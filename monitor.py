"""
Моніторинг кількох Telegram-каналів за ключовими словами + дзвінок на кілька
телефонів через Twilio. Має вбудований HTTP API (/start, /stop, /status),
через яке зовнішній сайт (наприклад на Netlify) може вмикати/вимикати
моніторинг без перезапуску процесу.

Встановлення залежностей:
    pip install telethon twilio python-dotenv fastapi uvicorn --break-system-packages

Перед першим запуском заповніть .env (див. .env.example), потім:
    python monitor.py
При першому запуску Telethon попросить номер телефону та код підтвердження
(вхід у ваш Telegram-акаунт), сесія збережеться у файл .session — тримайте
його в секреті, він = доступ до акаунта.
"""

import asyncio
import logging
import os
import time

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from telethon import TelegramClient, events
from twilio.rest import Client as TwilioClient

load_dotenv(".env", override=True)

# ---------- Налаштування ----------

# Telegram
API_ID = int(os.getenv("TG_API_ID"))
API_HASH = os.getenv("TG_API_HASH")
SESSION_NAME = os.getenv("TG_SESSION_NAME", "monitor_session")

# Канали, які слухаємо. @username або id, через кому.
CHANNELS = [c.strip() for c in os.getenv("TG_CHANNELS", "").split(",") if c.strip()]

# Ключові слова (регістр не враховується)
KEYWORDS = [k.strip().lower() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]

# Слова, для яких НЕ дзвонимо, а лише надсилаємо push-повідомлення в Telegram
# (Saved Messages акаунта, під яким залогінений бот)
PUSH_ONLY_KEYWORDS = [k.strip().lower() for k in os.getenv("PUSH_ONLY_KEYWORDS", "").split(",") if k.strip()]

# Стоп-фрази: якщо повідомлення містить будь-яку з них, воно повністю
# ігнорується, навіть якщо збігається ключове слово
EXCLUDE_KEYWORDS = [k.strip().lower() for k in os.getenv("EXCLUDE_KEYWORDS", "").split(",") if k.strip()]

# Twilio
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
# Номери, на які дзвонити, через кому: +380..., +380...
MY_PHONE_NUMBERS = [p.strip() for p in os.getenv("MY_PHONE_NUMBERS", "").split(",") if p.strip()]
TWIML_BIN_URL = os.getenv("TWIML_BIN_URL")

COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "300"))

# API-контроль (start/stop з сайту)
CONTROL_SECRET = os.getenv("CONTROL_SECRET")  # обов'язково задайте у .env
# Railway сам призначає порт через змінну PORT — якщо вона є, використовуємо її
CONTROL_PORT = int(os.getenv("PORT", os.getenv("CONTROL_PORT", "8080")))
# Домени, яким дозволено смикати API (ваш Netlify-сайт). "*" — дозволити всім.
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")

# ---------- Логування ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("monitor")

# ---------- Стан ----------

class State:
    active: bool = True  # моніторинг активний за замовчуванням
    last_call_ts: float = 0.0
    events: list = []  # журнал знайдених ключових слів / дзвінків (найновіші перші)
    calls_today: int = 0
    calls_today_date: str = ""

state = State()

MAX_LOG_ENTRIES = 100


def bump_call_counter():
    today = time.strftime("%Y-%m-%d")
    if state.calls_today_date != today:
        state.calls_today_date = today
        state.calls_today = 0
    state.calls_today += 1


def add_event(kind: str, channel: str, keyword: str, snippet: str = ""):
    """Додає запис у журнал подій (для показу на сайті через /logs)."""
    state.events.insert(0, {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,          # "keyword" (знайдено слово) або "call" (здійснено дзвінок)
        "channel": channel,
        "keyword": keyword,
        "snippet": snippet,
    })
    del state.events[MAX_LOG_ENTRIES:]

# ---------- Twilio ----------

def make_calls(channel: str = "", keyword: str = ""):
    """Дзвонить на всі номери зі списку MY_PHONE_NUMBERS."""
    now = time.time()
    if now - state.last_call_ts < COOLDOWN_SECONDS:
        log.info("Пропускаю дзвінок — ще діє cooldown (%s сек).", COOLDOWN_SECONDS)
        return
    state.last_call_ts = now

    client = TwilioClient(TWILIO_SID, TWILIO_AUTH_TOKEN)
    for number in MY_PHONE_NUMBERS:
        try:
            call = client.calls.create(
                url=TWIML_BIN_URL,
                to=number,
                from_=TWILIO_FROM_NUMBER,
            )
            log.info("Дзвінок на %s ініційовано, SID: %s", number, call.sid)
            add_event("call", channel, keyword, f"дзвінок на {number}")
            bump_call_counter()
        except Exception as e:
            log.error("Помилка при дзвінку на %s: %s", number, e)
            add_event("call_error", channel, keyword, f"{number}: {e}")


def text_matches_keywords(text: str) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    for kw in KEYWORDS:
        if kw in lowered:
            return kw
    return None


def text_has_exclusion(text: str) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    for kw in EXCLUDE_KEYWORDS:
        if kw in lowered:
            return kw
    return None


async def send_push(channel: str, keyword: str, snippet: str):
    """Надсилає push-повідомлення в Saved Messages акаунта (без дзвінка)."""
    try:
        text = f"⚠️ Знайдено '{keyword}' у [{channel}]:\n{snippet}"
        await tg_client.send_message("me", text)
        log.info("Push-повідомлення надіслано ('%s', %s)", keyword, channel)
        add_event("push", channel, keyword, snippet)
    except Exception as e:
        log.error("Помилка надсилання push: %s", e)


# ---------- Telegram ----------

tg_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


@tg_client.on(events.NewMessage(chats=CHANNELS))
async def handler(event):
    if not state.active:
        log.debug("Моніторинг вимкнено (пауза) — повідомлення пропущено.")
        return

    text = event.raw_text or ""
    matched = text_matches_keywords(text)
    if not matched:
        log.debug("Повідомлення без збігів: %s", text[:80])
        return

    excluded = text_has_exclusion(text)
    if excluded:
        log.info("Пропущено — містить стоп-фразу '%s'", excluded)
        add_event("excluded", "", matched, f"стоп-фраза: {excluded}")
        return

    chat = await event.get_chat()
    chat_name = str(getattr(chat, "username", None) or getattr(chat, "title", "?"))
    log.info("[%s] Знайдено '%s' у: %s", chat_name, matched, text[:200])
    add_event("keyword", chat_name, matched, text[:200])

    if matched in PUSH_ONLY_KEYWORDS:
        await send_push(chat_name, matched, text[:200])
    else:
        make_calls(channel=chat_name, keyword=matched)


# ---------- HTTP API (керування з сайту) ----------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN] if ALLOWED_ORIGIN != "*" else ["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def check_secret(x_api_key: str | None):
    if not CONTROL_SECRET or x_api_key != CONTROL_SECRET:
        raise HTTPException(status_code=401, detail="Невірний або відсутній ключ")


@app.get("/status")
async def status(x_api_key: str | None = Header(default=None)):
    check_secret(x_api_key)
    today = time.strftime("%Y-%m-%d")
    calls_today = state.calls_today if state.calls_today_date == today else 0
    return {
        "active": state.active,
        "channels": CHANNELS,
        "keywords": KEYWORDS,
        "calls_today": calls_today,
    }


@app.get("/logs")
async def logs(x_api_key: str | None = Header(default=None)):
    check_secret(x_api_key)
    return {"events": state.events}


@app.post("/start")
async def start(x_api_key: str | None = Header(default=None)):
    check_secret(x_api_key)
    state.active = True
    log.info("Моніторинг УВІМКНЕНО через API")
    return {"active": True}


@app.post("/stop")
async def stop(x_api_key: str | None = Header(default=None)):
    check_secret(x_api_key)
    state.active = False
    log.info("Моніторинг ВИМКНЕНО через API")
    return {"active": False}


@app.post("/restart")
async def restart(x_api_key: str | None = Header(default=None)):
    check_secret(x_api_key)
    log.info("Перезапуск ініційовано через API")

    async def _delayed_exit():
        await asyncio.sleep(0.5)  # дати час відповіді дійти до сайту
        os._exit(1)  # ненульовий код виходу -> Railway перезапустить процес

    asyncio.create_task(_delayed_exit())
    return {"restarting": True}


# ---------- Запуск ----------

async def main():
    if not KEYWORDS:
        log.warning("Список ключових слів порожній.")
    if not CHANNELS:
        raise SystemExit("Не вказано жодного каналу в TG_CHANNELS (.env)")
    if not MY_PHONE_NUMBERS:
        raise SystemExit("Не вказано жодного номера в MY_PHONE_NUMBERS (.env)")
    if not CONTROL_SECRET:
        raise SystemExit("Задайте CONTROL_SECRET у .env — це пароль для керування з сайту")

    await tg_client.start()
    log.info("Telegram підключено. Канали: %s", CHANNELS)
    log.info("Ключові слова: %s", KEYWORDS)
    log.info("Push-only слова (без дзвінка): %s", PUSH_ONLY_KEYWORDS)
    log.info("Стоп-фрази (виключення): %s", EXCLUDE_KEYWORDS)
    log.info("Номери для дзвінків: %s", MY_PHONE_NUMBERS)

    config = uvicorn.Config(app, host="0.0.0.0", port=CONTROL_PORT, log_level="info")
    server = uvicorn.Server(config)

    # Telegram-клієнт і HTTP API працюють одночасно в одному event loop
    await asyncio.gather(
        tg_client.run_until_disconnected(),
        server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())
