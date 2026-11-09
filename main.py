import os
import sys
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # ВАЖНО: до импорта webapp.backend.api — auth.py читает
                # TELEGRAM_BOT_TOKEN из окружения на уровне модуля, при импорте

import certifi
import uvicorn
from pymongo import MongoClient

from config import ConfigLoader
from core.database import Database, StrategySettingsStore
from core.database.mongo_config import MONGO_URI
from core.engine import SessionManager
from core.events import EventBus
from core.exchange import BingXClient
from core.exchange import SymbolSelector
from core.state import SettingsManager
from core.strategies import StrategyManager, SignalActivityTracker
from core.strategies.orderbook_analyzer import OrderBookAnalyzer
from core.telegram import TelegramGateway
from core.users import UsersRepository, admin_telegram_id
from core.users.migration import GLOBAL_SCOPE, migrate_legacy
from webapp.backend.api import app as webapp_app


def setup_logging(config: ConfigLoader) -> None:
    log_level = config.get('logging.level', 'INFO')
    log_file = config.get('logging.file', 'logs/trading_bot.log')
    max_bytes = config.get('logging.max_bytes', 10485760)
    backup_count = config.get('logging.backup_count', 5)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # File handler with UTF-8 encoding, rotated by logging.max_bytes / backup_count
    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8'
    )
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

    # Console handler with UTF-8 encoding
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

    # Force UTF-8 for console on Windows
    if sys.platform == 'win32':
        import codecs
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')

    logging.basicConfig(
        level=getattr(logging, log_level),
        handlers=[file_handler, console_handler]
    )

    # httpx на INFO пише URL кожного запиту, а в URL Telegram Bot API —
    # токен бота (api.telegram.org/bot<TOKEN>/getUpdates раз на 10 с)
    logging.getLogger('httpx').setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info("Logging initialized")


async def main():
    config = ConfigLoader()
    setup_logging(config)
    try:
        await run_bot(config)
    except KeyboardInterrupt:
        pass


async def run_bot(config: ConfigLoader) -> None:
    """Багатокористувацький бот в одному event loop:

    MarketEngine (один): ринкові дані BingX (live), пули монет, OrderBookAnalyzer,
        стратегії — сигнали рахуються ОДИН раз і йдуть у спільну шину.
    SessionManager: торговий контур кожного користувача (власні ключі, режим
        testnet/live, трейдер, ризик, трейлінг, сповіщення) — див. core/engine.
    """
    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("Starting Ruflo Trading Bot (multi-user)")
    logger.info("=" * 60)

    mongo_client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    admin_id = admin_telegram_id()
    if admin_id:
        migrate_legacy(mongo_client, admin_id, default_testnet=config.get('exchange.testnet', True))
    else:
        logger.warning("ADMIN_TELEGRAM_ID/TELEGRAM_CHAT_ID не задано — адміна немає, міграцію пропущено")
    users = UsersRepository(mongo_client)

    # Спільні (адмінські) дані: параметри стратегій (strategy_settings, live-база)
    # і whitelist/blacklist пулу монет (settings з user_id="__global__").
    market_db = Database(testnet=False, user_id=GLOBAL_SCOPE, client=mongo_client)
    shared_bus = EventBus()
    await shared_bus.start()
    global_settings = SettingsManager(market_db, shared_bus)
    strategy_settings = StrategySettingsStore(market_db)

    # Ринкові дані — завжди live (публічні ендпоінти); ключі адміна з env
    # потрібні лише для _get_held_symbols без провайдера (тут він є).
    market = BingXClient(
        api_key=os.getenv('BINGX_API_KEY', ''),
        api_secret=os.getenv('BINGX_API_SECRET', ''),
        testnet=False,
        event_bus=shared_bus,
    )
    logger.info("[OK] Market data client initialized (live)")

    signal_activity_tracker = SignalActivityTracker(shared_bus)

    def make_selector(cfg, **kwargs):
        return SymbolSelector(
            market, cfg, signal_tracker=signal_activity_tracker, event_bus=shared_bus,
            settings_manager=global_settings, **kwargs,
        )

    filters_config = config.get('trading.filters', {})
    refresh_interval = config.get('trading.filters.rotation_interval_seconds', 1800)
    symbol_selector = make_selector(filters_config)
    trend_filters_config = config.get('trading.trend_filters', {}) or {}
    trend_symbol_selector = (
        make_selector(trend_filters_config, trade_only=True, pool_label="TREND-SYMBOLS")
        if trend_filters_config else None
    )
    tsmom_filters_config = config.get('trading.tsmom_filters', {}) or {}
    tsmom_symbol_selector = (
        make_selector(tsmom_filters_config, trade_only=True, pool_label="TSMOM-SYMBOLS")
        if tsmom_filters_config else None
    )

    def _others_union(*selectors):
        def provider():
            union = set()
            for sel in selectors:
                if sel is not None:
                    union |= set(sel.current_symbols)
            return union
        return provider

    symbol_selector.reserved_symbols_provider = _others_union(trend_symbol_selector, tsmom_symbol_selector)
    if trend_symbol_selector is not None:
        trend_symbol_selector.reserved_symbols_provider = _others_union(symbol_selector, tsmom_symbol_selector)
    if tsmom_symbol_selector is not None:
        tsmom_symbol_selector.reserved_symbols_provider = _others_union(symbol_selector, trend_symbol_selector)

    OrderBookAnalyzer(shared_bus, config.get('trading.orderbook', {}))
    strategy_manager = StrategyManager(shared_bus, config, logger, strategy_settings, bingx_client=market)
    strategies = strategy_manager.setup()
    logger.info(f"[OK] Strategy Manager initialized ({len(strategies)} strategies)")

    if trend_symbol_selector is not None:
        trend_strategy = strategy_manager.get('TrendSupertrendStrategy')
        if trend_strategy is not None and hasattr(trend_strategy, 'set_symbol_pool_provider'):
            trend_strategy.set_symbol_pool_provider(lambda: trend_symbol_selector.current_symbols)
    tsmom_strategy = strategy_manager.get('TsMomentumStrategy')
    if tsmom_symbol_selector is not None and tsmom_strategy is not None:
        tsmom_strategy.set_symbol_pool_provider(lambda: tsmom_symbol_selector.current_symbols)

    # --- Telegram: один бот на всіх, сповіщення — кожному в свій чат ---
    telegram_gateway = None
    telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
    sessions: SessionManager  # оголошено нижче; lookup викликається вже після старту

    if config.get('telegram.enabled', False) and telegram_token:
        telegram_gateway = TelegramGateway(
            token=telegram_token,
            session_lookup=lambda user_id: sessions.get(user_id),
            admin_chat_id=admin_id,
            shared_event_bus=shared_bus,
        )

    sessions = SessionManager(
        users=users,
        mongo_client=mongo_client,
        config=config,
        shared_bus=shared_bus,
        strategy_settings=strategy_settings,
        telegram_gateway=telegram_gateway,
        market_context_provider=symbol_selector.market_context,
        targets_provider=(tsmom_strategy.current_targets if tsmom_strategy is not None else None),
    )
    strategy_manager.subscribe_params(
        'WallBreakoutStrategy',
        lambda params: sessions.set_trail_levels(params.get('trail_levels_percent')),
    )
    for selector in (symbol_selector, trend_symbol_selector, tsmom_symbol_selector):
        if selector is not None:
            selector.held_symbols_provider = sessions.held_symbols

    if telegram_gateway is not None:
        await telegram_gateway.start()
        logger.info("[OK] Telegram gateway started")

    await sessions.start_all()

    if config.get('exchange.websocket.enabled', True):
        await market.start_websocket()
        logger.info("[OK] Market WebSocket connected")
        selected = await symbol_selector.apply()
        logger.info(f"[OK] Initial symbol selection: {sorted(selected)}")
        await symbol_selector.start_refresh_loop(refresh_interval)
        if trend_symbol_selector is not None:
            await trend_symbol_selector.apply()
            await trend_symbol_selector.start_refresh_loop(trend_filters_config.get('rotation_interval_seconds', 3600))
        if tsmom_symbol_selector is not None:
            await tsmom_symbol_selector.apply()
            await tsmom_symbol_selector.start_refresh_loop(tsmom_filters_config.get('rotation_interval_seconds', 86400))

    # --- Mini App ---
    webapp_server = None
    webapp_task = None
    if config.get('webapp.enabled', True):
        state = webapp_app.state
        state.users = users
        state.sessions = sessions
        state.mongo_client = mongo_client
        state.market_exchange = market
        state.global_settings = global_settings
        state.strategy_settings = strategy_settings
        state.strategy_manager = strategy_manager
        state.symbol_selector = symbol_selector
        state.signal_tracker = signal_activity_tracker

        port = int(os.getenv('PORT', config.get('webapp.port', 8000)))
        webapp_server = uvicorn.Server(uvicorn.Config(webapp_app, host='0.0.0.0', port=port, log_level='warning'))
        webapp_task = asyncio.create_task(webapp_server.serve())
        logger.info(f"[OK] Web App API started on port {port}")

    if telegram_gateway is not None and admin_id:
        await telegram_gateway.send_to(admin_id, (
            f"🚀 <b>Бот запущено</b>\n"
            f"Користувачів із активною сесією: {len(sessions.sessions)}\n"
            f"Стратегій увімкнено: {sum(1 for s in strategies if getattr(s, 'enabled', False))}/{len(strategies)}"
        ))

    logger.info("Ruflo Trading Bot is running")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutdown signal received")

    if webapp_server:
        webapp_server.should_exit = True
        await webapp_task
    for selector in (symbol_selector, trend_symbol_selector, tsmom_symbol_selector):
        if selector is not None:
            await selector.stop_refresh_loop()
    await sessions.stop_all()
    if telegram_gateway is not None:
        await telegram_gateway.stop()
    await market.stop_websocket()
    await market.close()
    await shared_bus.stop()
    mongo_client.close()
    logger.info("Ruflo Trading Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())