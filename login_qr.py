"""
Одноразовий скрипт для входу в Telegram через QR-код (без SMS-коду).

Запуск:
    python login_qr.py

Скрипт покаже QR-код прямо в терміналі (ASCII-графіка).
Відкрийте Telegram на телефоні -> Налаштування -> Пристрої ->
"Підключити пристрій" (Link Desktop Device) -> наведіть камеру на QR-код у терміналі.

Після успішного сканування створиться файл сесії (той самий TG_SESSION_NAME з .env),
яким потім користуватиметься monitor.py — повторний вхід більше не знадобиться.
"""

import asyncio
import os

import qrcode
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

load_dotenv()

API_ID = int(os.getenv("TG_API_ID"))
API_HASH = os.getenv("TG_API_HASH")
SESSION_NAME = os.getenv("TG_SESSION_NAME", "monitor_session")


def print_qr_ascii(url: str):
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make()
    qr.print_ascii(invert=True)


async def main():
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        print("Ви вже авторизовані, сесія існує. Нічого робити не треба.")
        await client.disconnect()
        return

    qr_login = await client.qr_login()
    print("\nВідкрийте Telegram на телефоні:")
    print("Налаштування -> Пристрої -> Підключити пристрій (Link Desktop Device)")
    print("І відскануйте QR-код нижче:\n")
    print_qr_ascii(qr_login.url)

    try:
        await qr_login.wait()
        print("\n✅ Успішно авторизовано! Сесія збережена, тепер можна запускати monitor.py")
    except SessionPasswordNeededError:
        pw = input("Введіть пароль двофакторної автентифікації: ")
        await client.sign_in(password=pw)
        print("\n✅ Успішно авторизовано з паролем 2FA!")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
