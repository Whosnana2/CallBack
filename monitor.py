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

import asyncpg
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from telethon import TelegramClient, events
from twilio.rest import Client as TwilioClient

load_dotenv(".env", override=True)

# ---------- Налаштування ----------

# База даних (PostgreSQL на Railway — DATABASE_URL підставляється автоматично,
# якщо в тому ж проєкті додано сервіс Postgres і прив'язано змінну)
DATABASE_URL = os.getenv("DATABASE_URL")

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

# ---------- Стан (частково в пам'яті, події і лічильник — у Postgres) ----------

class State:
    active: bool = True  # моніторинг активний за замовчуванням (перезаписується з БД при старті)
    last_call_ts: float = 0.0

state = State()

db_pool: asyncpg.Pool | None = None


async def init_db():
    """Створює пул з'єднань і таблиці, якщо їх ще немає."""
    global db_pool
    if not DATABASE_URL:
        log.warning("DATABASE_URL не задано — журнал подій і лічильник НЕ будуть зберігатись між рестартами.")
        return
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMPTZ NOT NULL DEFAULT now(),
                kind TEXT NOT NULL,
                channel TEXT,
                keyword TEXT,
                snippet TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'active'")
        if row is not None:
            state.active = row["value"] == "true"
    log.info("Підключено до Postgres, стан 'active' завантажено: %s", state.active)


async def set_active_in_db(active: bool):
    state.active = active
    if db_pool is None:
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('active', $1) "
            "ON CONFLICT (key) DO UPDATE SET value = $1",
            "true" if active else "false",
        )


async def add_event(kind: str, channel: str, keyword: str, snippet: str = ""):
    """Записує подію в Postgres (для показу на сайті через /logs)."""
    if db_pool is None:
        log.debug("Подія не збережена (немає БД): %s / %s / %s", kind, channel, keyword)
        return
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO events (kind, channel, keyword, snippet) VALUES ($1, $2, $3, $4)",
            kind, channel, keyword, snippet,
        )


async def get_recent_events(limit: int = 100):
    if db_pool is None:
        return []
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT ts, kind, channel, keyword, snippet FROM events ORDER BY ts DESC LIMIT $1",
            limit,
        )
    return [
        {
            "time": r["ts"].strftime("%Y-%m-%d %H:%M:%S"),
            "kind": r["kind"],
            "channel": r["channel"] or "",
            "keyword": r["keyword"] or "",
            "snippet": r["snippet"] or "",
        }
        for r in rows
    ]


async def get_calls_today() -> int:
    if db_pool is None:
        return 0
    async with db_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM events WHERE kind = 'call' AND ts::date = (now() AT TIME ZONE 'Europe/Kyiv')::date"
        )
    return count or 0


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

async def make_calls(channel: str = "", keyword: str = ""):
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
            await add_event("call", channel, keyword, f"дзвінок на {number}")
        except Exception as e:
            log.error("Помилка при дзвінку на %s: %s", number, e)
            await add_event("call_error", channel, keyword, f"{number}: {e}")


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
        await add_event("push", channel, keyword, snippet)
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
        await add_event("excluded", "", matched, f"стоп-фраза: {excluded}")
        return

    chat = await event.get_chat()
    chat_name = str(getattr(chat, "username", None) or getattr(chat, "title", "?"))
    log.info("[%s] Знайдено '%s' у: %s", chat_name, matched, text[:200])
    await add_event("keyword", chat_name, matched, text[:200])

    if matched in PUSH_ONLY_KEYWORDS:
        await send_push(chat_name, matched, text[:200])
    else:
        await make_calls(channel=chat_name, keyword=matched)


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
    calls_today = await get_calls_today()
    return {
        "active": state.active,
        "channels": CHANNELS,
        "keywords": KEYWORDS,
        "calls_today": calls_today,
    }


@app.get("/logs")
async def logs(x_api_key: str | None = Header(default=None)):
    check_secret(x_api_key)
    return {"events": await get_recent_events()}


@app.post("/start")
async def start(x_api_key: str | None = Header(default=None)):
    check_secret(x_api_key)
    await set_active_in_db(True)
    log.info("Моніторинг УВІМКНЕНО через API")
    return {"active": True}


@app.post("/stop")
async def stop(x_api_key: str | None = Header(default=None)):
    check_secret(x_api_key)
    await set_active_in_db(False)
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

    await init_db()

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