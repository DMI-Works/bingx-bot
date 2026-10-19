"""
Ruflo Mini App backend.

Своєї БД і свого exchange-клієнта не відкриває: використовує ті самі живі
об'єкти бота (Database, BingXClient, SettingsManager, ...), які main.py
кладе в app.state при старті (див. секцію "Mini App" в main.py).
Ендпоінти — у routes/, залежності від app.state — у deps.py.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .routes import analytics, profile, settings, symbols, trades

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Ruflo Mini App API")

# Опційні: якщо main.py їх не проставить (webapp.enabled=False, тести),
# відповідні ендпоінти відповідатимуть 503, решта API працює.
app.state.db = None
app.state.exchange_client = None
app.state.settings_manager = None
app.state.strategy_settings = None
app.state.strategy_manager = None
app.state.symbol_selector = None
app.state.signal_tracker = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

for module in (trades, analytics, profile, settings, symbols):
    app.include_router(module.router)

# Зібраний фронт (npm run build кладе файли в static/) — монтується
# останнім, щоб "/" не перехоплював /api/*
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
