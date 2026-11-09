"""
Ruflo Mini App backend.

Своєї БД і свого exchange-клієнта не відкриває: спільні об'єкти (users,
sessions, пул монет, параметри стратегій) main.py кладе в app.state, а дані
рахунку беруться із сесії авторизованого користувача (див. deps.py).
Ендпоінти — у routes/, залежності від app.state — у deps.py.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .routes import account, admin, analytics, profile, settings, symbols, trades

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Ruflo Mini App API")

# Опційні: якщо main.py їх не проставить (webapp.enabled=False, тести),
# відповідні ендпоінти відповідатимуть 503, решта API працює.
app.state.users = None
app.state.mongo_client = None
app.state.billing = None
app.state.sessions = None
app.state.market_exchange = None
app.state.global_settings = None
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

for module in (account, admin, trades, analytics, profile, settings, symbols):
    app.include_router(module.router)

# Зібраний фронт (npm run build кладе файли в static/) — монтується
# останнім, щоб "/" не перехоплював /api/*
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
