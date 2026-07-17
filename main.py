import os
import sys
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv

from config import ConfigLoader
from core.database import Database
from core.events import EventBus
from core.exchange import BingXClient
from core.exchange import SymbolSelector
from core.state import SettingsManager
from core.risk import RiskManager
from core.strategies import SimpleMovingAverageStrategy
from core.telegram import TelegramBot
from core.trading import SimpleTrader


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
    risk_manager = RiskManager(db, event_bus, risk_config)
    logger.info("[OK] Risk Manager initialized")

    trader = SimpleTrader(
        exchange=exchange,
        event_bus=event_bus,
        db=db,
        risk_manager=risk_manager,
    )

    risk_manager.sync_from_positions(trader.open_positions)
    logger.info(f"[OK] Simple Trader initialized")

    filters_config = config.get('trading.filters', {})
    refresh_interval = config.get('trading.filters.refresh_interval_seconds', 3600)
    symbol_selector = SymbolSelector(exchange, filters_config)
    logger.info("[OK] Symbol Selector initialized")

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
                symbol_selector=symbol_selector
            )
            await telegram_bot.start()
            logger.info("[OK] Telegram Bot started")
        else:
            logger.warning("Telegram credentials not found, bot disabled")

    ws_enabled = config.get('exchange.websocket.enabled', True)
    if ws_enabled:
        await exchange.start_websocket()
        await exchange.start_user_data_stream()
        logger.info("[OK] WebSocket connected")

        selected_symbols = await symbol_selector.apply()
        logger.info(f"[OK] Initial symbol selection: {sorted(selected_symbols)}")

        await symbol_selector.start_refresh_loop(refresh_interval)

    tp_levels_config = config.get('trading.take_profit.levels', [])
    first_tp_percent = tp_levels_config[0]['percent'] if tp_levels_config else 3.0
    use_atr_risk = config.get('trading.stop_loss.mode', 'fixed_percent') == 'atr'

    strategy_config = {
        'timeframe_seconds': 60,
        'sma_period': config.get('trading.sma_period', 20),
        'threshold_percent': config.get('trading.threshold_percent', 0.3),
        'confirmation_candles': config.get('trading.confirmation_candles', 2),
        'cooldown_seconds': config.get('trading.cooldown_seconds', 300),
        'position_size': config.get('trading.position_size.value', 100),
        'leverage': config.get('trading.leverage', 10),

        # ATR risk
        'use_atr_risk': use_atr_risk,
        'atr_period': config.get('trading.stop_loss.atr.period', 14),
        'atr_stop_multiplier': config.get('trading.stop_loss.atr.multiplier', 1.5),
        'atr_tp_multipliers': config.get('trading.take_profit.atr.multipliers', [2.0, 3.5]),
        'tp_close_percents': config.get('trading.take_profit.atr.close_percents', [50, 50]),

        # Fallback: фиксированные проценты, если ATR выключен
        'stop_loss_percent': config.get('trading.stop_loss.value', 2.0),
        'take_profit_levels': config.get('trading.take_profit.levels', [{'percent': 3.0, 'close_percent': 100}]),
    }

    strategy = SimpleMovingAverageStrategy(event_bus, strategy_config)

    enabled_strategies = config.get('strategies.enabled', [])
    if 'SimpleMovingAverageStrategy' in enabled_strategies:
        strategy.enable()
        logger.info("[OK] SimpleMovingAverageStrategy enabled")

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
