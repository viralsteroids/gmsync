"""
One-way sync: Exchange -> Gmail
- Inbox -> Gmail Inbox
- Sent Items -> Gmail label "Exchange/Sent"
- Enhanced duplicate detection to avoid conflicts with Google Workspace Migration

Adapted for Google App Engine Standard:
- HTTP handler /tasks/sync (Flask)
- Single sync pass per request (no infinite loop)
- Gmail auth via env var GMAIL_TOKEN_JSON (authorized_user JSON)
- State stored in /tmp/*.json (ephemeral, per-instance)
"""

import os
import json
import time
import base64
from datetime import datetime, timezone, timedelta

from flask import Flask

from exchangelib import (
    Account,
    Credentials,
    Configuration,
    DELEGATE,
    NTLM,
    EWSDateTime,
    UTC,
)

from google.oauth2.credentials import Credentials as GCreds  # type: ignore
from googleapiclient.discovery import build  # type: ignore
from google.auth.transport.requests import Request  # type: ignore

# -----------------------------------------------------------------------------
# Flask app (entrypoint for App Engine)
# -----------------------------------------------------------------------------

app = Flask(__name__)

# -----------------------------------------------------------------------------
# CONFIG (for GAE)
# -----------------------------------------------------------------------------

EXCHANGE_EMAIL    = "alexander.abolmasov@slsoft.ru"   # your mailbox SMTP
EXCHANGE_USERNAME = r"softline\abolmasovag"           # Domain\Username (note the r'' to keep the backslash)
EXCHANGE_PASSWORD = "&PdSq6UCBa$zg^3J"  # Используйте переменную окружения для безопасности

# Working EWS server configuration
EWS_SERVER = "mail.softline.com"  # Working EWS endpoint



# Директория для runtime-состояния (на GAE Standard можно писать только в /tmp)
RUNTIME_DIR = os.environ.get("RUNTIME_DIR", "/tmp")
os.makedirs(RUNTIME_DIR, exist_ok=True)

STATE_FILE = os.path.join(RUNTIME_DIR, "state.json")        # tracks EWS sync state
SEEN_FILE = os.path.join(RUNTIME_DIR, "seen.json")          # tracks synced Message-IDs
DUPLICATES_FILE = os.path.join(RUNTIME_DIR, "duplicates.json")  # duplicate detection

# Gmail scopes
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# --- Test mode ---
TEST_LIMIT = None          # set to int for limited test
TEST_DRY_RUN = False       # True: print-only, no import

# Duplicate detection settings
CHECK_DUPLICATES = True  # Enable/disable duplicate checking

# Import settings (configurable via env)
IMPORT_LAST_DAYS = int(os.environ.get("IMPORT_LAST_DAYS", "2"))
DUPLICATE_CHECK_DAYS = int(os.environ.get("DUPLICATE_CHECK_DAYS", "2"))

# Time sync safety
SYNC_GRACE_MINUTES = int(os.environ.get("SYNC_GRACE_MINUTES", "180"))


# -----------------------------------------------------------------------------
# Gmail helpers (for GAE)
# -----------------------------------------------------------------------------

def gmail_service():
    """
    Create Gmail service using JSON from env GMAIL_TOKEN_JSON.

    GMAIL_TOKEN_JSON must contain the content of a token.json that you
    generated locally via OAuth (authorized_user credentials).
    """
    token_json = os.environ.get("GMAIL_TOKEN_JSON")
    if not token_json:
        raise RuntimeError(
            "GMAIL_TOKEN_JSON environment variable is not set. "
            "Generate token.json locally via OAuth and put its content into env."
        )

    creds_info = json.loads(token_json)
    creds = GCreds.from_authorized_user_info(creds_info, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        print("Refreshing Gmail token via refresh_token...")
        creds.refresh(Request())
        # На GAE мы не сохраняем обратно обновлённый токен — остаёмся stateless.

    return build("gmail", "v1", credentials=creds)


def ensure_gmail_valid(service):
    try:
        service.users().labels().list(userId="me").execute()
        return service
    except Exception as e:
        print(f"Gmail service invalid: {e}. Rebuilding...")
        return gmail_service()


def load_labels_map(service):
    """Load a name -> id dict for all labels (once per sync pass)."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    return {l["name"]: l["id"] for l in labels}


def print_gmail_label_counts(service, labels_map=None):
    try:
        inbox_info = service.users().labels().get(userId="me", id="INBOX").execute()
        inbox_total = inbox_info.get("messagesTotal", 0)
        print(f"📧 Total emails in Gmail Inbox: {inbox_total}")

        backup_label_id = None
        if labels_map and "backup.pst/Входящие" in labels_map:
            backup_label_id = labels_map["backup.pst/Входящие"]
        else:
            # One-time fallback if cache not provided
            labels = service.users().labels().list(userId="me").execute().get("labels", [])
            for label in labels:
                if label.get("name") == "backup.pst/Входящие":
                    backup_label_id = label["id"]
                    break

        if backup_label_id:
            backup_info = service.users().labels().get(
                userId="me", id=backup_label_id
            ).execute()
            backup_total = backup_info.get("messagesTotal", 0)
            print(f"📁 Total emails in backup.pst/Входящие: {backup_total}")
        else:
            print("⚠️  Label 'backup.pst/Входящие' not found")
    except Exception as e:
        print(f"❌ Error checking Gmail label counts: {e}")


def ensure_label(service, name, labels_map=None):
    try:
        # Use cache if available
        if labels_map is not None and name in labels_map:
            return labels_map[name]

        # Fallback: fetch labels once if cache not provided
        labels = service.users().labels().list(userId="me").execute().get("labels", [])
        for L in labels:
            if L["name"] == name:
                if labels_map is not None:
                    labels_map[name] = L["id"]
                return L["id"]

        lbl = service.users().labels().create(
            userId="me", body={"name": name}
        ).execute()
        if labels_map is not None:
            labels_map[name] = lbl["id"]
        return lbl["id"]
    except Exception as e:
        print(f"❌ Error in ensure_label for '{name}': {e}")
        if name == "Exchange/Sent":
            # Fallback – use built-in SENT
            return "SENT"
        return None


def import_raw(service, raw_bytes, label_ids=None):
    body = {"raw": base64.urlsafe_b64encode(raw_bytes).decode("ascii")}
    if label_ids:
        body["labelIds"] = label_ids

    result = service.users().messages().import_(
        userId="me", body=body, internalDateSource="dateHeader"
    ).execute()

    message_id = result.get("id")
    if message_id:
        try:
            service.users().messages().modify(
                userId="me",
                id=message_id,
                body={"addLabelIds": ["UNREAD"]},
            ).execute()
            print(f"✅ Email marked as unread: {message_id}")
        except Exception as e:
            print(f"⚠️  Warning: Could not mark email as unread: {e}")

    return result


# -----------------------------------------------------------------------------
# Duplicate detection (optimized via Message-ID)
# -----------------------------------------------------------------------------

def search_gmail_for_duplicate(service, message_id):
    """
    Fast duplicate search by RFC822 Message-ID via:
        rfc822msgid:"<message-id>"
    Returns True if a duplicate is found.
    """
    if not CHECK_DUPLICATES or not message_id:
        return False

    query = f'rfc822msgid:"{message_id}"'
    print(f"🔍 Duplicate check by Message-ID: {query}")

    try:
        res = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=1,
        ).execute()
        if res.get("messages"):
            print("✅ Duplicate found by Message-ID")
            return True
        print("❌ No duplicates by Message-ID")
        return False
    except Exception as e:
        print(f"❌ Error in Message-ID duplicate search: {e}")
        return False


def is_duplicate_email(service, item, seen, duplicates_db):
    """
    Duplicate detection for an email item:
    - first check local 'seen' set
    - then check Gmail via rfc822msgid
    """
    if not CHECK_DUPLICATES:
        return False

    mid = getattr(item, "message_id", None)

    # Local fast check
    if mid and mid in seen:
        print(f"Message-ID already seen in this runtime: {mid[:80]}...")
        return True

    if not mid:
        # Без Message-ID нормальный дедуп невозможен — считаем, что не дубликат
        print("⚠️  No Message-ID on item, skipping duplicate check.")
        return False

    print(f"🔍 Checking for duplicates by Message-ID: {mid[:80]}...")

    is_dup = search_gmail_for_duplicate(service, mid)

    if is_dup:
        dt_val = getattr(item, "datetime_received", None) or getattr(
            item, "datetime_sent", None
        )
        duplicates_db[mid] = {
            "message_id": mid,
            "subject": getattr(item, "subject", ""),
            "sender": str(getattr(item, "sender", "")),
            "date": dt_val.isoformat() if dt_val else None,
            "detected_at": time.time(),
        }
        print("✅ DUPLICATE FOUND, skipping import.")
    else:
        print("❌ No duplicate found by Message-ID.")

    return is_dup


def cleanup_old_duplicates(duplicates_db, max_age_days=30):
    current_time = time.time()
    max_age_seconds = max_age_days * 24 * 3600

    keys_to_remove = []
    for key, record in list(duplicates_db.items()):
        if record.get("detected_at", 0) < (current_time - max_age_seconds):
            keys_to_remove.append(key)

    for key in keys_to_remove:
        del duplicates_db[key]

    if keys_to_remove:
        print(f"Cleaned up {len(keys_to_remove)} old duplicate records")

    return len(keys_to_remove)


def print_sync_stats(state, seen, duplicates_db):
    print("\n" + "=" * 50)
    print("SYNC STATISTICS")
    print("=" * 50)

    inbox_dt = state.get("inbox_dt") or state.get("inbox")
    sent_dt = state.get("sent_dt") or state.get("sent")

    if inbox_dt:
        print(f"Inbox last sync: {inbox_dt}")
    if sent_dt:
        print(f"Sent last sync:  {sent_dt}")

    print(f"Total messages processed this runtime: {len(seen)}")

    if duplicates_db:
        print(f"Duplicate patterns detected: {len(duplicates_db)}")
        recent_duplicates = 0

        for record in duplicates_db.values():
            date_str = record.get("date")
            if not date_str:
                continue
            try:
                rec_date = datetime.fromisoformat(date_str).date()
            except Exception:
                continue
            if (datetime.utcnow().date() - rec_date).days <= 7:
                recent_duplicates += 1

        print(f"Recent duplicates (last 7 days): {recent_duplicates}")

    print("=" * 50 + "\n")


# -----------------------------------------------------------------------------
# Exchange helpers
# -----------------------------------------------------------------------------

def ews_account():
    creds = Credentials(username=EXCHANGE_USERNAME, password=EXCHANGE_PASSWORD)

    try:
        print(f"Connecting to Exchange server: {EWS_SERVER}")

        cfg = Configuration(server=EWS_SERVER, credentials=creds, auth_type=NTLM)

        account = Account(
            primary_smtp_address=EXCHANGE_EMAIL,
            credentials=creds,
            autodiscover=False,
            config=cfg,
            access_type=DELEGATE,
        )

        # Test connection
        _ = account.root
        print(f"✓ Successfully connected to Exchange: {EWS_SERVER}")
        return account

    except Exception as e:
        print(f"✗ Error connecting to Exchange: {str(e)}")
        raise Exception(f"Failed to connect to Exchange server {EWS_SERVER}: {str(e)}")


# -----------------------------------------------------------------------------
# Time helpers
# -----------------------------------------------------------------------------

def to_utc_iso(dt):
    if dt is None:
        return None
    if isinstance(dt, EWSDateTime):
        return dt.astimezone(UTC).isoformat()
    if getattr(dt, "tzinfo", None) is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def from_utc_iso(s):
    if not s:
        return None
    py_dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return EWSDateTime(
        py_dt.year,
        py_dt.month,
        py_dt.day,
        py_dt.hour,
        py_dt.minute,
        py_dt.second,
        py_dt.microsecond,
        tzinfo=UTC,
    )


# -----------------------------------------------------------------------------
# Core sync
# -----------------------------------------------------------------------------

def sync_folder_timebased(
    service,
    folder,
    state,
    seen,
    state_key,
    label_ids,
    dt_field,
    limit=None,
    print_progress=False,
    ignore_state=False,
    ignore_seen=False,
    dry_run=False,
    duplicates_db=None,
):
    last_dt_iso = state.get(state_key + "_dt") if not ignore_state else None
    last_dt = from_utc_iso(last_dt_iso) if last_dt_iso else None

    try:
        now_ews = EWSDateTime.now(tz=UTC)
        if last_dt and last_dt > now_ews:
            adjusted = now_ews - timedelta(minutes=1)
            if print_progress:
                print(
                    f"{state_key}: last_dt ({last_dt}) is in the future; "
                    f"clamping to {adjusted}"
                )
            last_dt = adjusted
    except Exception:
        pass

    qs = folder.all().only(
        "mime_content",
        "message_id",
        dt_field,
        "subject",
        "sender",
        "datetime_received",
        "datetime_sent",
    )

    try:
        now_ews = EWSDateTime.now(tz=UTC)
        cutoff_ews = now_ews - timedelta(days=IMPORT_LAST_DAYS) if IMPORT_LAST_DAYS else None

        effective_last_ews = None
        if last_dt and not ignore_state:
            grace = timedelta(minutes=SYNC_GRACE_MINUTES)
            effective_last_ews = last_dt - grace

        threshold = None
        if cutoff_ews and effective_last_ews:
            threshold = cutoff_ews if cutoff_ews > effective_last_ews else effective_last_ews
        else:
            threshold = effective_last_ews or cutoff_ews

        if threshold is not None:
            qs = qs.filter(**{f"{dt_field}__gt": threshold})
            if print_progress:
                print(
                    f"{state_key}: Filtering emails newer than "
                    f"{threshold.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC"
                )
    except Exception:
        if IMPORT_LAST_DAYS:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=IMPORT_LAST_DAYS)
            qs = qs.filter(**{f"{dt_field}__gt": cutoff_date})
            if print_progress:
                print(
                    f"{state_key}: Filtering emails newer than "
                    f"{cutoff_date.strftime('%Y-%m-%d %H:%M:%S')} UTC (fallback)"
                )

    qs = qs.order_by(dt_field)
    if limit:
        try:
            qs = qs[:limit]
        except Exception:
            pass

    count = 0
    duplicates_skipped = 0
    newest = last_dt

    for item in qs:
        mid = getattr(item, "message_id", None)

        if not ignore_seen and mid and mid in seen:
            dt_val = getattr(item, dt_field)
            if dt_val and (newest is None or dt_val > newest):
                newest = dt_val
            continue

        if duplicates_db is not None and is_duplicate_email(service, item, seen, duplicates_db):
            duplicates_skipped += 1
            if print_progress:
                subj = getattr(item, "subject", "(no subject)")
                print(f"{state_key} DUPLICATE SKIPPED: {subj}")
            dt_val = getattr(item, dt_field)
            if dt_val and (newest is None or dt_val > newest):
                newest = dt_val
            continue

        try:
            if not dry_run:
                import_raw(service, item.mime_content, label_ids=label_ids)
                if mid:
                    seen.add(mid)
            count += 1
            if print_progress:
                subj = getattr(item, "subject", "(no subject)")
                dt_val = getattr(item, dt_field)
                dt_str = to_utc_iso(dt_val) if dt_val else "N/A"
                lim_str = str(limit) if limit else "?"
                print(f"{state_key} {count}/{lim_str}: {subj} | {dt_str}")
            dt_val = getattr(item, dt_field)
            if dt_val and (newest is None or dt_val > newest):
                newest = dt_val
        except Exception as e:
            print(f"Error importing {mid}: {e}")

        if limit and count >= limit:
            break

    if newest and not ignore_state and not dry_run:
        try:
            now_ews = EWSDateTime.now(tz=UTC)
            if newest > now_ews:
                if print_progress:
                    print(f"{state_key}: newest ({newest}) is in the future; clamping")
                newest = now_ews
            state[state_key + "_dt"] = to_utc_iso(newest)
        except Exception:
            pass

    if print_progress:
        print(f"{state_key}: Imported {count} messages, skipped {duplicates_skipped} duplicates")

    return count



