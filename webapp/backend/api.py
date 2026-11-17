"""
Velora Mini App backend.

Своєї БД і свого exchange-клієнта не відкриває: спільні об'єкти (users,
sessions, пул монет, параметри стратегій) main.py кладе в app.state, а дані
рахунку беруться із сесії авторизованого користувача (див. deps.py).
Ендпоінти — у routes/, залежності від app.state — у deps.py.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .i18n import install_request_lang
from .security import install_security_headers
from .routes import account, admin, analytics, profile, settings, symbols, trades

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Velora Mini App API",
    # публічна карта API не потрібна: Mini App знає свої ендпоінти
    docs_url=None, redoc_url=None, openapi_url=None,
)

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

# Заголовки безпеки; CORS вимкнено навмисно — див. security.py
install_security_headers(app)
install_request_lang(app)

for module in (account, admin, trades, analytics, profile, settings, symbols):
    app.include_router(module.router)

class MiniAppStaticFiles(StaticFiles):
    """index.html не кешується: інакше Telegram показував стару версію Mini App
    зі старими полями API. Файли в assets/ мають хеш у назві — їх кешуємо надовго."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.startswith("assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


# Зібраний фронт (npm run build кладе файли в static/) — монтується
# останнім, щоб "/" не перехоплював /api/*
if STATIC_DIR.exists():
    app.mount("/", MiniAppStaticFiles(directory=STATIC_DIR, html=True), name="static")
