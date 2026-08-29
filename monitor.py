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

state = State()

# ---------- Twilio ----------

def make_calls():
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
        except Exception as e:
            log.error("Помилка при дзвінку на %s: %s", number, e)


def text_matches_keywords(text: str) -> str | None:
    if not text:
        return None
    lowered = text.lower()
    for kw in KEYWORDS:
        if kw in lowered:
            return kw
    return None


# ---------- Telegram ----------

tg_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


@tg_client.on(events.NewMessage(chats=CHANNELS))
async def handler(event):
    if not state.active:
        log.debug("Моніторинг вимкнено (пауза) — повідомлення пропущено.")
        return

    text = event.raw_text or ""
    matched = text_matches_keywords(text)
    if matched:
        chat = await event.get_chat()
        chat_name = getattr(chat, "username", None) or getattr(chat, "title", "?")
        log.info("[%s] Знайдено '%s' у: %s", chat_name, matched, text[:200])
        make_calls()
    else:
        log.debug("Повідомлення без збігів: %s", text[:80])


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
    return {"active": state.active, "channels": CHANNELS, "keywords": KEYWORDS}


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