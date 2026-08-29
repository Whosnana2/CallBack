# Telegram keyword monitor → Twilio call

Слідкує за ключовими словами в кількох Telegram-каналах, дзвонить на телефон
через Twilio при збігу. Має HTTP API (`/start`, `/stop`, `/status`) для
керування з зовнішнього сайту.

## Локальний запуск (потрібен один раз — щоб згенерувати сесію Telegram)

```
pip install -r requirements.txt --break-system-packages
```

Створи `.env` поруч з `monitor.py` (сам файл `.env` в репозиторій НЕ заливається —
див. `.gitignore`) зі змінними:

```
TG_API_ID=...
TG_API_HASH=...
TG_SESSION_NAME=monitor_session
TG_CHANNELS=@TEST_BALISTUKU,@kyiv_airdef
KEYWORDS=...
TWILIO_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=...
TWIML_BIN_URL=...
MY_PHONE_NUMBERS=+380970705298
COOLDOWN_SECONDS=300
CONTROL_SECRET=...
ALLOWED_ORIGIN=https://твій-сайт.netlify.app
```

Запусти:

```
python monitor.py
```

Введи номер телефону і код з Telegram. З'явиться файл `monitor_session.session`
— закоммить його разом з кодом (репозиторій має бути **приватним**, цей файл
= вхід у твій Telegram-акаунт).

## Деплой на Railway

1. Заведи приватний GitHub-репозиторій, заклади туди весь вміст цієї папки
   (включно з `monitor_session.session`, окрім `.env`)
2. railway.app → New Project → Deploy from GitHub repo
3. Project → Variables → додай усі змінні з переліку вище зі своїми значеннями
   (`PORT` не чіпай — Railway задає сам)
4. Settings → Networking → Generate Domain — отримаєш публічну адресу
5. Цю адресу + `CONTROL_SECRET` встав на сайті керування (Netlify)
