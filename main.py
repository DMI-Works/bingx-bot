import os
import sys
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv

from config import ConfigLoader
from core.database import Database, StrategySettingsStore
from core.events import EventBus
from core.exchange import BingXClient
from core.exchange import SymbolSelector
from core.state import SettingsManager
from core.risk import RiskManager, TrailingStopManager
from core.trading import SimpleTrader
from core.strategies import StrategyManager
from core.telegram import TelegramBot


def setup_logging(config: ConfigLoader) -> None:
    log_level = config.get('logging.level', 'INFO')
    log_file = config.get('logging.file', 'logs/trading_bot.log')
    max_bytes = config.get('logging.max_bytes', 10485760)
    backup_count = config.get('logging.backup_count', 5)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # File handler with UTF-8 encoding
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
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

    logger = logging.getLogger(__name__)
    logger.info("Logging initialized")


async def main():
    load_dotenv()

    config = ConfigLoader()
    setup_logging(config)

    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("Starting Ruflo Trading Bot")
    logger.info("=" * 60)

    db = Database(config.get('database.path'))
    logger.info("[OK] Database initialized")

    event_bus = EventBus()
    await event_bus.start()
    logger.info("[OK] Event Bus started")

    api_key = os.getenv('BINGX_API_KEY')
    api_secret = os.getenv('BINGX_API_SECRET')
    testnet = config.get('exchange.testnet', True)

    if not api_key or not api_secret:
        logger.error("BingX API credentials not found in environment variables")
        return

    exchange = BingXClient(
        api_key=api_key,
        api_secret=api_secret,
        testnet=testnet,
        event_bus=event_bus
    )
    logger.info("[OK] Exchange client initialized")

    settings_manager = SettingsManager(db, event_bus)
    logger.info("[OK] Settings Manager initialized")

    risk_config = config.get('trading.risk')
    risk_manager = RiskManager(db, event_bus, exchange, risk_config)
    logger.info("[OK] Risk Manager initialized")

    trader = SimpleTrader(
        exchange=exchange,
        event_bus=event_bus,
        db=db,
        risk_manager=risk_manager,
    )
    logger.info("[OK] Simple Trader initialized")

    trailing_stop_config = config.get('trading.trailing_stop', {})
    trailing_stop_manager = TrailingStopManager(
        event_bus=event_bus,
        exchange=exchange,
        db=db,
        trader=trader,
        config=trailing_stop_config,
    )
    logger.info("[OK] Trailing Stop Manager initialized")

    filters_config = config.get('trading.filters', {})
    refresh_interval = config.get('trading.filters.refresh_interval_seconds', 3600)
    symbol_selector = SymbolSelector(exchange, filters_config)
    logger.info("[OK] Symbol Selector initialized")

    # --- Strategy settings + live strategy manager створюються ДО
    # TelegramBot, бо SettingsMenu всередині нього має отримати вже готовий
    # strategy_manager (щоб тумблер enabled/зміна параметра/reset у меню
    # застосовувались одразу, без рестарту бота) ---
    strategy_settings = StrategySettingsStore(db)
    logger.info("[OK] Strategy Settings Store initialized")

    strategy_manager = StrategyManager(event_bus, config, logger, strategy_settings, bingx_client=exchange)
    strategies = strategy_manager.setup()
    logger.info(f"[OK] Strategy Manager initialized ({len(strategies)} strategies)")

    telegram_enabled = config.get('telegram.enabled', False)
    telegram_bot = None

    if telegram_enabled:
        telegram_token = os.getenv('TELEGRAM_BOT_TOKEN')
        telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')

        if telegram_token and telegram_chat_id:
            telegram_bot = TelegramBot(
                token=telegram_token,
                chat_id=telegram_chat_id,
                event_bus=event_bus,
                db=db,
                settings_manager=settings_manager,
                exchange_client=exchange,
                symbol_selector=symbol_selector,
                strategy_settings=strategy_settings,
                strategy_manager=strategy_manager,
            )
            await telegram_bot.start()
            logger.info("[OK] Telegram Bot started")
        else:
            logger.warning("Telegram credentials not found, bot disabled")

    ws_enabled = config.get('exchange.websocket.enabled', True)
    selected_symbols = set()

    if ws_enabled:
        await exchange.start_websocket()
        await exchange.start_user_data_stream()
        logger.info("[OK] WebSocket connected")

        selected_symbols = await symbol_selector.apply()
        logger.info(f"[OK] Initial symbol selection: {sorted(selected_symbols)}")

        await symbol_selector.start_refresh_loop(refresh_interval)

    # --- Стартове повідомлення в Telegram — ЛИШЕ після того, як усе
    # реально готове (біржа, стратегії, символи), щоб цифри в ньому
    # відповідали дійсності. Якщо Telegram вимкнено — просто пропускаємо. ---
    if telegram_bot:
        try:
            await telegram_bot.notify_startup(
                testnet=testnet,
                strategies=strategies
            )
            logger.info("[OK] Startup notification sent to Telegram")
        except Exception as e:
            logger.error(f"Failed to send startup notification: {e}", exc_info=True)

    logger.info("=" * 60)
    logger.info("Ruflo Trading Bot is running")
    logger.info("Press Ctrl+C to stop")
    logger.info("=" * 60)

    try:
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        logger.info("Shutdown signal received")

    logger.info("Shutting down...")

    if telegram_bot:
        await telegram_bot.stop()
        logger.info("[OK] Telegram Bot stopped")

    await symbol_selector.stop_refresh_loop()
    logger.info("[OK] Symbol selector stopped")

    await exchange.stop_websocket()
    logger.info("[OK] WebSocket stopped")

    await exchange.close()
    logger.info("[OK] Exchange client closed")

    await event_bus.stop()
    logger.info("[OK] Event Bus stopped")

    db.close()
    logger.info("[OK] Database closed")

    logger.info("Ruflo Trading Bot stopped successfully")


if __name__ == "__main__":
    asyncio.run(main())