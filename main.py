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
from core.execution import OrderManager, ExecutionEngine
from core.state import PositionManager, RecoveryEngine, SettingsManager
from core.risk import RiskManager
from core.strategies import SimpleMovingAverageStrategy
from core.telegram import TelegramBot


def setup_logging(config: ConfigLoader) -> None:
    log_level = config.get('logging.level', 'INFO')
    log_file = config.get('logging.file', 'logs/trading_bot.log')
    max_bytes = config.get('logging.max_bytes', 10485760)
    backup_count = config.get('logging.backup_count', 5)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
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
    logger.info("✓ Database initialized")

    event_bus = EventBus()
    await event_bus.start()
    logger.info("✓ Event Bus started")

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
    logger.info("✓ Exchange client initialized")

    settings_manager = SettingsManager(db, event_bus)
    logger.info("✓ Settings Manager initialized")

    order_manager = OrderManager(db, event_bus)
    logger.info("✓ Order Manager initialized")

    position_manager = PositionManager(db, event_bus)
    logger.info("✓ Position Manager initialized")

    recovery_engine = RecoveryEngine(
        exchange=exchange,
        order_manager=order_manager,
        position_manager=position_manager,
        db=db,
        event_bus=event_bus
    )
    logger.info("✓ Recovery Engine initialized")

    risk_config = config.get('trading.risk')
    risk_manager = RiskManager(db, event_bus, risk_config)
    logger.info("✓ Risk Manager initialized")

    execution_engine = ExecutionEngine(
        exchange=exchange,
        order_manager=order_manager,
        position_manager=position_manager,
        event_bus=event_bus,
        db=db
    )
    logger.info("✓ Execution Engine initialized")

    logger.info("Starting recovery process...")
    recovery_success = await recovery_engine.recover()

    if not recovery_success:
        logger.error("Recovery failed! Check logs for details.")

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
                position_manager=position_manager,
                order_manager=order_manager,
                settings_manager=settings_manager
            )
            await telegram_bot.start()
            logger.info("✓ Telegram Bot started")
        else:
            logger.warning("Telegram credentials not found, bot disabled")

    ws_enabled = config.get('exchange.websocket.enabled', True)
    if ws_enabled:
        await exchange.start_websocket()
        await exchange.subscribe_account()
        await exchange.subscribe_orders()
        logger.info("✓ WebSocket connected")

        whitelist_symbols = config.get('trading.filters.whitelist_symbols', [])
        for symbol in whitelist_symbols:
            await exchange.subscribe_trades(symbol)
            logger.info(f"✓ Subscribed to {symbol}")

    strategy_config = {
        'sma_period': 20,
        'position_size': config.get('trading.position_size.value', 100),
        'stop_loss_percent': config.get('trading.stop_loss.value', 2.0),
        'take_profit_percent': config.get('trading.take_profit.levels.0.percent', 3.0)
    }

    strategy = SimpleMovingAverageStrategy(event_bus, strategy_config)

    enabled_strategies = config.get('strategies.enabled', [])
    if 'SimpleMovingAverageStrategy' in enabled_strategies:
        strategy.enable()
        logger.info("✓ SimpleMovingAverageStrategy enabled")

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
        logger.info("✓ Telegram Bot stopped")

    await exchange.stop_websocket()
    logger.info("✓ WebSocket stopped")

    await exchange.close()
    logger.info("✓ Exchange client closed")

    await event_bus.stop()
    logger.info("✓ Event Bus stopped")

    db.close()
    logger.info("✓ Database closed")

    logger.info("Ruflo Trading Bot stopped successfully")


if __name__ == "__main__":
    asyncio.run(main())
