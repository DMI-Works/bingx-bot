"""
Ruflo Mini App backend.

Своєї БД і свого exchange-клієнта не відкриває: використовує ті самі живі
об'єкти бота (Database, BingXClient, SettingsManager, ...), які main.py
кладе в app.state при старті (див. секцію "Mini App" в main.py).
Ендпоінти — у routes/, залежності від app.state — у deps.py.
"""

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from .deps import require_telegram_user
from .security import install_security_headers
from .routes import analytics, profile, settings, symbols, trades

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Ruflo Mini App API",
    # публічна карта API не потрібна: Mini App знає свої ендпоінти
    docs_url=None, redoc_url=None, openapi_url=None,
)

# Опційні: якщо main.py їх не проставить (webapp.enabled=False, тести),
# відповідні ендпоінти відповідатимуть 503, решта API працює.
app.state.db = None
app.state.exchange_client = None
app.state.settings_manager = None
app.state.strategy_settings = None
app.state.strategy_manager = None
app.state.symbol_selector = None
app.state.signal_tracker = None

# Заголовки безпеки; CORS вимкнено навмисно — див. security.py
install_security_headers(app)

# Авторизація на рівні роутерів: жоден /api/* (навіть GET з балансом і
# позиціями) не віддається без підпису Telegram від власника бота.
for module in (trades, analytics, profile, settings, symbols):
    app.include_router(module.router, dependencies=[Depends(require_telegram_user)])

# Зібраний фронт (npm run build кладе файли в static/) — монтується
# останнім, щоб "/" не перехоплював /api/*
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
