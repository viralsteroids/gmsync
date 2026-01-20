# -*- coding: utf-8 -*-
"""
Telegram-бот ежедневного чеклиста под Google App Engine (webhook + cron).

- /tasks/daily_checklist  — дергается cron'ом раз в день и шлёт чеклист в чат.
- /telegram/webhook       — webhook от Telegram для команд и нажатий по кнопкам.
- /telegram/set_webhook   — разово вызываем в браузере, чтобы зарегистрировать webhook.
"""

import os
import json
from datetime import datetime
from typing import Dict, List

import requests
from flask import Flask, request

# ===== Настройки из env (app.yaml) =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")        # Токен бота
CHAT_ID = int(os.environ.get("CHAT_ID", "0"))  # Целевой чат для ежедневного чеклиста
TZ_NAME = os.environ.get("TZ_NAME", "Europe/Tallinn")
APP_BASE_URL = os.environ.get("APP_BASE_URL")  # https://checklistbot-dot-...appspot.com

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в env_variables.")
if not CHAT_ID:
    raise RuntimeError("Не задан CHAT_ID в env_variables.")
if not APP_BASE_URL:
    raise RuntimeError("Не задан APP_BASE_URL в env_variables.")

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

try:
    from zoneinfo import ZoneInfo  # для Python 3.9+
except ModuleNotFoundError:
    from backports.zoneinfo import ZoneInfo  # для локального Python 3.8


TZ = ZoneInfo(TZ_NAME)

# ===== Данные чеклиста =====

CHECKLIST_TEMPLATE = [
    "Подъём ≤ 07:00",
    "Стакан ГКВ (горячей кипяченой воды) натощак",
    "БАДы",
    "Зарядка",
    "Завтрак",
    "ГКВ между завтраком и обедом",
    "Обед",
    "Тренировка или прогулка",
    "ГКВ между обедом и ужином",
    "Ужин ≤ 18:00",
    "Общий объём жидкости ≥ 2 л/сут",
    
    "Вечерняя практика (растяжка, дыхание, медитация)",
    "Ирригатор",
    "Сауна/горячая ванна (2 раза в неделю)",
    "Отбой ≤ 23:00",
]

# Пункты, которые НЕ проверяются при scheduled check (не ежедневные)
SKIP_ON_SCHEDULED_CHECK = {
    "Сауна/горячая ванна (2 раза в неделю)",
}

# message_id -> список состояний пунктов чеклиста
CHECKLIST_STATE: Dict[int, List[bool]] = {}

# message_id последнего отправленного чеклиста (для проверки прогресса)
LAST_CHECKLIST_MSG_ID: int | None = None

# Время последней отправки чеклиста (для защиты от повторных отправок)
LAST_CHECKLIST_SENT: datetime | None = None

PIN_MESSAGE = True

# ===== Flask-приложение =====

app = Flask(__name__)


# ===== Хелперы Telegram =====

def render_checklist_text(states: List[bool], premium: bool = True) -> str:
    """Рендерит текст чеклиста с премиум-форматированием."""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    
    if premium:
        # Премиум-формат с HTML
        header = f"✨ <b>Чеклист на {today}</b> ✨"
        lines = [header, ""]
        
        completed = sum(states)
        total = len(states)
        progress = f"📊 Прогресс: {completed}/{total} ({int(completed/total*100)}%)"
        lines.append(progress)
        lines.append("")
        
        for done, title in zip(states, CHECKLIST_TEMPLATE):
            if done:
                prefix = "✅"
                title_formatted = f"<s>{title}</s>"
            else:
                prefix = "⬜"
                title_formatted = title
            lines.append(f"{prefix} {title_formatted}")
        
        return "\n".join(lines)
    else:
        # Обычный формат
        lines = [f"✅ Чеклист на {today}"]
        for done, title in zip(states, CHECKLIST_TEMPLATE):
            prefix = "☑️" if done else "⬜️"
            lines.append(f"{prefix} {title}")
        return "\n".join(lines)


def build_keyboard(states: List[bool]) -> dict:
    rows = []
    for idx, (done, title) in enumerate(zip(states, CHECKLIST_TEMPLATE)):
        box = "☑️" if done else "☐"
        rows.append([{"text": f"{box} {title}", "callback_data": f"t:{idx}"}])
    return {"inline_keyboard": rows}


def tg_request(method: str, payload: dict) -> dict:
    url = f"{BASE_URL}/{method}"
    resp = requests.post(url, json=payload, timeout=10)
    try:
        data = resp.json()
    except Exception:
        print(f"Telegram API error, status={resp.status_code}, text={resp.text}")
        return {}
    if not data.get("ok", False):
        print(f"Telegram API returned error for {method}: {data}")
    return data


def send_message(chat_id: int, text: str, reply_markup: dict | None = None, parse_mode: str = "HTML") -> int | None:
    """Отправляет сообщение. По умолчанию использует HTML для премиум-форматирования."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = tg_request("sendMessage", payload)
    msg = data.get("result") or {}
    return msg.get("message_id")


def pin_message(chat_id: int, message_id: int) -> bool:
    """Пытается закрепить сообщение. Возвращает True если успешно."""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "disable_notification": True,
    }
    result = tg_request("pinChatMessage", payload)
    return result.get("ok", False)


def edit_message(chat_id: int, message_id: int, text: str, reply_markup: dict, parse_mode: str = "HTML") -> None:
    """Редактирует сообщение. По умолчанию использует HTML для премиум-форматирования."""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
        "reply_markup": reply_markup,
    }
    tg_request("editMessageText", payload)


def answer_callback_query(callback_query_id: str) -> None:
    tg_request("answerCallbackQuery", {"callback_query_id": callback_query_id})


# ===== Логика чеклиста =====

def create_and_send_checklist(chat_id: int, use_premium: bool = True) -> None:
    """Создаёт и отправляет новый чеклист в чат с премиум-форматированием."""
    global LAST_CHECKLIST_MSG_ID

    states = [False] * len(CHECKLIST_TEMPLATE)
    text = render_checklist_text(states, premium=use_premium)
    keyboard = build_keyboard(states)

    msg_id = send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML" if use_premium else "Markdown")
    if msg_id is None:
        print(f"⚠️ Не удалось отправить чеклист в чат {chat_id}. Проверьте, что:")
        print(f"   1. Бот добавлен в группу/чат с ID {chat_id}")
        print(f"   2. Бот имеет права на отправку сообщений")
        print(f"   3. CHAT_ID в app.yaml указан правильно")
        return

    CHECKLIST_STATE[msg_id] = states
    LAST_CHECKLIST_MSG_ID = msg_id

    if PIN_MESSAGE:
        pin_result = tg_request("pinChatMessage", {
            "chat_id": chat_id,
            "message_id": msg_id,
            "disable_notification": True,
        })
        if not pin_result.get("ok", False):
            error_desc = pin_result.get("description", "unknown error")
            if "not enough rights" in error_desc.lower():
                print(f"ℹ️ Не удалось закрепить сообщение: у бота нет прав администратора в группе. Чеклист отправлен.")
            else:
                print(f"⚠️ Не удалось закрепить сообщение: {error_desc}")


def check_and_remind_progress(chat_id: int) -> None:
    """Проверяет прогресс по чеклисту и отправляет напоминание о невыполненных пунктах."""
    global LAST_CHECKLIST_MSG_ID

    if LAST_CHECKLIST_MSG_ID is None:
        print("⚠️ Нет сохранённого ID чеклиста для проверки прогресса")
        return

    states = CHECKLIST_STATE.get(LAST_CHECKLIST_MSG_ID)
    if states is None:
        print(f"⚠️ Состояние чеклиста {LAST_CHECKLIST_MSG_ID} не найдено")
        return

    # Собираем невыполненные пункты (кроме тех, что в SKIP_ON_SCHEDULED_CHECK)
    uncompleted = []
    for done, title in zip(states, CHECKLIST_TEMPLATE):
        if not done and title not in SKIP_ON_SCHEDULED_CHECK:
            uncompleted.append(title)

    if not uncompleted:
        print("✅ Все ежедневные пункты выполнены!")
        return

    # Формируем напоминание
    now = datetime.now(TZ)
    time_str = now.strftime("%H:%M")

    lines = [f"⏰ <b>Напоминание ({time_str})</b>", ""]
    lines.append(f"Осталось выполнить ({len(uncompleted)}):")
    for title in uncompleted:
        lines.append(f"⬜ {title}")

    text = "\n".join(lines)
    send_message(chat_id, text, parse_mode="HTML")


# ===== Обработка апдейтов Telegram =====

def handle_update(update: dict) -> None:
    """Обрабатываем входящий update от Telegram (команды и нажатия кнопок)."""
    if "message" in update:
        msg = update["message"]
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        text = msg.get("text", "") or ""

        if not chat_id or not text:
            return

        if text.startswith("/start"):
            send_message(
                chat_id,
                "Привет! Я публикую ежедневный чеклист в этой группе.\n"
                "Команды:\n"
                "• /getchatid — показать ID текущего чата\n"
                "• /now — отправить чеклист прямо сейчас",
            )
        elif text.startswith("/getchatid"):
            send_message(chat_id, f"Chat ID: `{chat_id}`")
        elif text.startswith("/now"):
            create_and_send_checklist(chat_id)

    elif "callback_query" in update:
        cq = update["callback_query"]
        cq_id = cq.get("id")
        msg = cq.get("message") or {}
        data = cq.get("data") or ""

        if cq_id:
            # ответим, чтобы Telegram не показывал "часики"
            answer_callback_query(cq_id)

        if not data.startswith("t:"):
            return

        try:
            idx = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            return

        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        msg_id = msg.get("message_id")

        if chat_id is None or msg_id is None:
            return

        states = CHECKLIST_STATE.get(msg_id)
        if states is None:
            states = [False] * len(CHECKLIST_TEMPLATE)
            CHECKLIST_STATE[msg_id] = states

        if 0 <= idx < len(states):
            states[idx] = not states[idx]
            new_text = render_checklist_text(states, premium=True)
            new_kb = build_keyboard(states)
            try:
                edit_message(chat_id, msg_id, new_text, new_kb)
            except Exception as e:
                print(f"⚠️ Не удалось отредактировать сообщение: {e}")


# ===== Flask-эндпоинты =====

@app.post("/telegram/webhook")
def telegram_webhook():
    """Webhook, на который Telegram шлёт апдейты."""
    update = request.get_json(silent=True, force=True) or {}
    try:
        handle_update(update)
    except Exception as e:
        print(f"❌ Error in handle_update: {e}")
    return "ok", 200


@app.get("/telegram/set_webhook")
def set_webhook():
    """Разово вызвать в браузере, чтобы зарегистрировать webhook у Telegram."""
    url = APP_BASE_URL.rstrip("/") + "/telegram/webhook"
    r = requests.get(
        f"{BASE_URL}/setWebhook",
        params={"url": url},
        timeout=10,
    )
    return f"setWebhook -> {r.status_code}: {r.text}", 200


@app.get("/tasks/daily_checklist")
def daily_checklist():
    """Эндпоинт для cron: один раз отправляет чеклист в заданный CHAT_ID."""
    global LAST_CHECKLIST_SENT
    
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    
    # Защита от повторных отправок: проверяем, был ли чеклист отправлен сегодня
    # и не менее 1 часа назад (на случай повторных вызовов cron)
    if LAST_CHECKLIST_SENT is not None:
        last_sent_date = LAST_CHECKLIST_SENT.strftime("%Y-%m-%d")
        time_diff = (now - LAST_CHECKLIST_SENT).total_seconds() / 3600  # разница в часах
        
        if last_sent_date == today and time_diff < 1:
            print(f"⚠️ Чеклист уже был отправлен сегодня ({today}) {int(time_diff*60)} минут назад. Пропускаем.")
            return f"Already sent today ({today}, {int(time_diff*60)} min ago)", 200
    
    print(f"=== DAILY CHECKLIST START ({today}) ===")
    try:
        create_and_send_checklist(CHAT_ID)
        LAST_CHECKLIST_SENT = now
        print(f"=== DAILY CHECKLIST END ({today}) ===")
        return "ok", 200
    except Exception as e:
        print(f"❌ Error sending checklist: {e}")
        return f"Error: {e}", 500


@app.get("/tasks/check_progress")
def check_progress():
    """Эндпоинт для cron: проверяет прогресс и отправляет напоминание."""
    now = datetime.now(TZ)
    time_str = now.strftime("%H:%M")

    print(f"=== CHECK PROGRESS ({time_str}) ===")
    try:
        check_and_remind_progress(CHAT_ID)
        print(f"=== CHECK PROGRESS END ({time_str}) ===")
        return "ok", 200
    except Exception as e:
        print(f"❌ Error checking progress: {e}")
        return f"Error: {e}", 500


@app.get("/")
def index():
    return "checklistbot is running. Try /telegram/set_webhook or wait for cron.", 200


@app.get("/telegram/bot_info")
def bot_info():
    """Получить информацию о боте (username, id и т.д.)."""
    data = tg_request("getMe", {})
    if data.get("ok"):
        bot_data = data.get("result", {})
        username = bot_data.get("username")
        
        result = {
            "bot_info": bot_data,
            "has_username": bool(username),
            "invite_link": f"https://t.me/{username}" if username else None,
        }
        
        if not username:
            result["instructions"] = [
                "У бота нет username. Чтобы добавить бота в группу:",
                "1. Откройте @BotFather в Telegram",
                "2. Отправьте /mybots",
                "3. Выберите вашего бота",
                "4. Выберите 'Edit Bot' -> 'Edit Username'",
                "5. Установите username (например: checklistbot_bot)",
                "6. После этого можно добавить бота в группу по имени или использовать ссылку"
            ]
        else:
            result["instructions"] = [
                f"Чтобы добавить бота в группу:",
                f"1. Откройте группу в Telegram",
                f"2. Нажмите 'Добавить участников'",
                f"3. Найдите бота по имени: @{username}",
                f"Или используйте ссылку: https://t.me/{username}?startgroup=start"
            ]
        
        return result, 200
    else:
        return {"error": "Failed to get bot info", "details": data}, 500


@app.get("/health")
def health():
    """Проверка конфигурации и статуса бота."""
    info = {
        "status": "ok",
        "bot_token_set": bool(BOT_TOKEN),
        "chat_id": CHAT_ID,
        "chat_id_valid": CHAT_ID != 0 and CHAT_ID != -1001234567890,
        "app_base_url": APP_BASE_URL,
        "timezone": TZ_NAME,
    }
    
    if not BOT_TOKEN:
        info["status"] = "error"
        info["error"] = "BOT_TOKEN not set"
    elif not CHAT_ID or CHAT_ID == 0 or CHAT_ID == -1001234567890:
        info["status"] = "warning"
        info["warning"] = "CHAT_ID is not set or is example value. Use /getchatid command in Telegram to get your chat ID"
        info["instructions"] = [
            "1. Add bot to your Telegram group/channel",
            "2. Send /getchatid command in that group/channel",
            "3. Update CHAT_ID in checklistbot/app.yaml",
            "4. Redeploy: gcloud app deploy checklistbot/app.yaml"
        ]
    elif not APP_BASE_URL:
        info["status"] = "error"
        info["error"] = "APP_BASE_URL not set"
    
    return info, 200
