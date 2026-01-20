"""
App Engine entrypoint for Exchange -> Gmail one-way sync.

Использует логику из sync.py как библиотеку и даёт HTTP-эндпоинт /tasks/sync,
который делает ОДИН проход синка (Inbox + Sent).
"""

import os
import json
import time

from flask import Flask

# Импортируем всё нужное из твоего исходного sync.py
import sync as syncmod

# -----------------------------------------------------------------------------
# Flask app
# -----------------------------------------------------------------------------

app = Flask(__name__)

# -----------------------------------------------------------------------------
# Runtime-файлы состояния в /tmp (разрешённая директория на App Engine)
# -----------------------------------------------------------------------------

RUNTIME_DIR = os.environ.get("RUNTIME_DIR", "/tmp")
os.makedirs(RUNTIME_DIR, exist_ok=True)

STATE_FILE = os.path.join(RUNTIME_DIR, "state.json")
SEEN_FILE = os.path.join(RUNTIME_DIR, "seen.json")
DUPLICATES_FILE = os.path.join(RUNTIME_DIR, "duplicates.json")

# Можно при желании переопределить тестовые режимы
TEST_LIMIT = None       # например, 50 для теста
TEST_DRY_RUN = False    # True = только лог, без записи в Gmail
ITEMS_PER_RUN = int(os.environ.get("ITEMS_PER_RUN", "200"))


# -----------------------------------------------------------------------------
# Один проход синка (Inbox + Sent) - быстрый режим
# -----------------------------------------------------------------------------

def _load_runtime_state():
    """Загружает state/seen/duplicates из файлов в RUNTIME_DIR."""
    state = {"inbox": None, "sent": None}
    seen: set[str] = set()
    duplicates_db: dict = {}

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state.update(json.load(f))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"⚠️  Warning: Invalid JSON in {STATE_FILE}: {e}. Using default state.")
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE) as f:
                seen = set(json.load(f))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"⚠️  Warning: Invalid JSON in {SEEN_FILE}: {e}. Using empty set.")
    if os.path.exists(DUPLICATES_FILE):
        try:
            with open(DUPLICATES_FILE) as f:
                duplicates_db.update(json.load(f))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"⚠️  Warning: Invalid JSON in {DUPLICATES_FILE}: {e}. Using empty dict.")

    return state, seen, duplicates_db


def _save_runtime_state(state, seen, duplicates_db):
    """Сохраняет state/seen/duplicates в файлы в RUNTIME_DIR."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)
    with open(DUPLICATES_FILE, "w") as f:
        json.dump(duplicates_db, f)


def run_sync_once():
    print("=== run_sync_once: start ===")

    # ---- загрузка состояния ----
    state, seen, duplicates_db = _load_runtime_state()

    # ---- подключение к Exchange и Gmail ----
    acct = syncmod.ews_account()
    gsvc = syncmod.gmail_service()
    gsvc = syncmod.ensure_gmail_valid(gsvc)

    print("Initializing duplicate detection for Inbox and backup.pst/Входящие...")
    syncmod.print_gmail_label_counts(gsvc)
    sent_lbl_id = syncmod.ensure_label(gsvc, "Exchange/Sent")

    # ---- тестовый режим (ограниченное количество сообщений) ----
    if TEST_LIMIT is not None:
        print(
            f"Test run: processing up to {TEST_LIMIT} messages total "
            f"({ 'dry-run' if TEST_DRY_RUN else 'import' })"
        )
        remaining = TEST_LIMIT

        gsvc = syncmod.ensure_gmail_valid(gsvc)
        print("Syncing Inbox...")
        imported_inbox = syncmod.sync_folder_timebased(
            gsvc,
            acct.inbox,
            state,
            seen,
            "inbox",
            ["INBOX"],
            "datetime_received",
            limit=remaining,
            print_progress=True,
            ignore_state=True,
            ignore_seen=True,
            dry_run=TEST_DRY_RUN,
            duplicates_db=duplicates_db,
        )
        remaining = max(0, remaining - imported_inbox)

        if remaining > 0:
            print("Syncing Sent Items...")
            sent_lbl_id = syncmod.ensure_label(gsvc, "Exchange/Sent")
            imported_sent = syncmod.sync_folder_timebased(
                gsvc,
                acct.sent,
                state,
                seen,
                "sent",
                [sent_lbl_id] if sent_lbl_id else [],
                "datetime_sent",
                limit=remaining,
                print_progress=True,
                ignore_state=True,
                ignore_seen=True,
                dry_run=TEST_DRY_RUN,
                duplicates_db=duplicates_db,
            )
            remaining = max(0, remaining - imported_sent)

        print(f"Test completed. Processed {TEST_LIMIT - remaining} messages total.")
        syncmod.print_sync_stats(state, seen, duplicates_db)

        print("=== run_sync_once: end (test mode) ===")
        return

    # ---- боевой режим: один проход ----
    syncmod.cleanup_old_duplicates(duplicates_db)

    try:
        gsvc = syncmod.ensure_gmail_valid(gsvc)
        syncmod.print_gmail_label_counts(gsvc)

        print("Syncing Inbox...")
        syncmod.sync_folder_timebased(
            gsvc,
            acct.inbox,
            state,
            seen,
            "inbox",
            ["INBOX"],
            "datetime_received",
            duplicates_db=duplicates_db,
        )

        print("Syncing Sent Items...")
        sent_lbl_id = syncmod.ensure_label(gsvc, "Exchange/Sent")
        if sent_lbl_id:
            syncmod.sync_folder_timebased(
                gsvc,
                acct.sent,
                state,
                seen,
                "sent",
                [sent_lbl_id],
                "datetime_sent",
                duplicates_db=duplicates_db,
            )
        else:
            print("⚠️  Skipping Sent Items sync - no label available")

        # сохраняем состояние
        _save_runtime_state(state, seen, duplicates_db)

        syncmod.print_sync_stats(state, seen, duplicates_db)
        syncmod.cleanup_old_duplicates(duplicates_db)

    except Exception as e:
        print(f"❌ Error in sync pass: {e}")

    print("=== run_sync_once: end ===")


# -----------------------------------------------------------------------------
# Глубокий синк (Inbox + Sent) с длинным окном - для запуска реже (раз в час)
# -----------------------------------------------------------------------------

def run_sync_deep_once():
    print("=== run_sync_deep_once: start ===")

    state, seen, duplicates_db = _load_runtime_state()

    acct = syncmod.ews_account()
    gsvc = syncmod.gmail_service()
    gsvc = syncmod.ensure_gmail_valid(gsvc)

    print("Initializing duplicate detection for deep sync...")
    syncmod.print_gmail_label_counts(gsvc)

    # Сохраняем и временно увеличиваем окно синка
    old_import_last_days = getattr(syncmod, "IMPORT_LAST_DAYS", None)
    deep_days_env = os.environ.get("DEEP_IMPORT_LAST_DAYS")
    try:
        deep_days = int(deep_days_env) if deep_days_env else None
    except ValueError:
        deep_days = None

    if deep_days is not None and deep_days > 0:
        print(f"Deep sync: using DEEP_IMPORT_LAST_DAYS={deep_days} (was {old_import_last_days})")
        syncmod.IMPORT_LAST_DAYS = deep_days
    else:
        print(f"Deep sync: using existing IMPORT_LAST_DAYS={old_import_last_days}")

    try:
        # Inbox с игнорированием state, но с дедупликацией
        print("Deep sync: Inbox...")
        syncmod.sync_folder_timebased(
            gsvc,
            acct.inbox,
            state,
            seen,
            "inbox_deep",
            ["INBOX"],
            "datetime_received",
            print_progress=True,
            ignore_state=True,
            ignore_seen=False,
            dry_run=False,
            duplicates_db=duplicates_db,
        )

        # Sent Items
        print("Deep sync: Sent Items...")
        sent_lbl_id = syncmod.ensure_label(gsvc, "Exchange/Sent")
        if sent_lbl_id:
            syncmod.sync_folder_timebased(
                gsvc,
                acct.sent,
                state,
                seen,
                "sent_deep",
                [sent_lbl_id],
                "datetime_sent",
                print_progress=True,
                ignore_state=True,
                ignore_seen=False,
                dry_run=False,
                duplicates_db=duplicates_db,
            )
        else:
            print("⚠️  Deep sync: Skipping Sent Items - no label available")

        _save_runtime_state(state, seen, duplicates_db)
        syncmod.print_sync_stats(state, seen, duplicates_db)

    except Exception as e:
        print(f"❌ Error in deep sync pass: {e}")
    finally:
        # Восстанавливаем исходное окно
        if old_import_last_days is not None:
            syncmod.IMPORT_LAST_DAYS = old_import_last_days

    print("=== run_sync_deep_once: end ===")


# -----------------------------------------------------------------------------
# HTTP-эндпоинты для cron
# -----------------------------------------------------------------------------

@app.get("/tasks/sync")
def tasks_sync():
    """
    Вызывается cron-джобом App Engine и ручным заходом в браузер.
    Один HTTP-запрос = один проход синка (быстрый режим).
    """
    run_sync_once()
    return "ok", 200


@app.get("/tasks/sync_deep")
def tasks_sync_deep():
    """
    Вызывается cron-джобом раз в час.
    Делает глубокий синк с увеличенным окном по датам.
    """
    run_sync_deep_once()
    return "ok", 200

# -----------------------------------------------------------------------------
# Ежедневный процесс: печать (или отправка) подсказки по выбору проекта/сервиса
# -----------------------------------------------------------------------------


if __name__ == "__main__":
    run_sync_once()
