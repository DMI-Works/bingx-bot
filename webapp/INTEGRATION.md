# Как вкрутить в bingx-bot

## 1. Куда класть файлы

Скопируйте папку `webapp/` целиком в корень репозитория `bingx-bot`, рядом с `core/` и `main.py`:

```
bingx-bot/
├── core/
├── config/
├── webapp/              ← сюда
│   ├── frontend/         (React, Vite)
│   └── backend/          (FastAPI: api.py, auth.py)
├── main.py
└── requirements.txt
```

## 2. Установка

```bash
# фронт
cd webapp/frontend
npm install

# бэкенд — добавьте в корневой requirements.txt
pip install fastapi uvicorn
```

## 3. Локальная разработка

Два терминала:

```bash
# терминал 1 — бэкенд
uvicorn webapp.backend.api:app --reload --port 8000

# терминал 2 — фронт с горячей перезагрузкой
cd webapp/frontend && npm run dev
```

Откройте `http://localhost:5173` в обычном браузере — Telegram WebApp SDK
подгрузится, но без реального Telegram просто ничего не подставит в
`window.Telegram.WebApp`, поэтому UI отрисуется с моками. Этого достаточно,
чтобы верстать и проверять логику.

## 4. Проверка внутри самого Telegram (нужен HTTPS)

Telegram Mini App обязательно открывается по `https://`, localhost не
подходит. Для теста — быстрый туннель:

```bash
# любой из вариантов
cloudflared tunnel --url http://localhost:5173
ngrok http 5173
```

Полученный `https://...` URL укажите в BotFather:

```
/mybots → выбрать бота → Bot Settings → Menu Button → Edit Menu Button URL
```

или как кнопку прямо в сообщении (см. пункт 5).

## 5. Кнопка запуска мини-аппа из бота

Пример для **aiogram** — добавьте в обработчик `/start` или `/status`
(`core/telegram/`):

```python
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

WEBAPP_URL = "https://your-domain.com"  # прод-домен или туннель для теста

kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📊 Статистика", web_app=WebAppInfo(url=WEBAPP_URL))]
])

await message.answer("Открыть статистику:", reply_markup=kb)
```

Если используете **pyTelegramBotAPI**, аналог:

```python
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

markup = InlineKeyboardMarkup()
markup.add(InlineKeyboardButton("📊 Статистика", web_app=WebAppInfo(url=WEBAPP_URL)))
bot.send_message(chat_id, "Открыть статистику:", reply_markup=markup)
```

## 6. Продакшен: один процесс, один деплой

Собираете фронт (он ляжет прямо в `webapp/backend/static`, см.
`vite.config.js`), а FastAPI отдаёт и API, и статику фронта с одного порта —
второй домен/сервис не нужен:

```bash
cd webapp/frontend
npm run build
```

В `main.py` бота запустите бота и веб-сервер как две задачи одного
event loop:

```python
import asyncio
import uvicorn
from webapp.backend.api import app as webapp_app

async def run_webapp():
    config = uvicorn.Config(webapp_app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    await asyncio.gather(
        run_bot(),       # ваша существующая функция запуска бота
        run_webapp(),
    )

if __name__ == "__main__":
    asyncio.run(main())
```

Перед портом 8000 нужен обычный reverse-proxy с HTTPS (nginx / Caddy) и
домен — Telegram не откроет мини-апп без валидного сертификата.

## 7. Переменные окружения

Добавьте в `.env`:

```
TELEGRAM_BOT_TOKEN=тот же токен, что уже используется ботом
```

`webapp/backend/auth.py` использует его для проверки `initData` — без этого
любой человек с прямой ссылкой на API увидит вашу статистику в обход
Telegram-авторизации.

## 8. Что доделать перед первым запуском

- В `webapp/backend/api.py` названия таблиц/колонок (`balance_history`,
  `positions`, `trades`) — заглушка. Сверьте с реальной схемой в
  `core/database/` и поправьте SQL-запросы.
- Включите `Depends(require_telegram_user)` в эндпоинтах `api.py` (сейчас
  закомментировано как `user=None`, чтобы можно было тестировать без
  Telegram) — для прода обязательно раскомментировать.
- В `App.jsx` заменить мок-константы (`EQUITY_SERIES`, `OPEN_POSITIONS`,
  `TRADE_HISTORY`) на вызовы `apiGet(...)`, которые уже определены в файле.
