"""
UserSession — торговий контур ОДНОГО користувача.

Усе, що залежить від рахунку, живе тут і не перетинається з іншими
користувачами: власний BingXClient (ключі користувача, REST + user-data
stream), власна шина подій, налаштування, RiskManager, SimpleTrader,
TrailingStopManager, ExcursionTracker і Telegram-сповіщення в його чат.

Ринкові дані й сигнали стратегій спільні (MarketEngine): SessionManager
пересилає в шину сесії лише сигнали (якщо стратегія ввімкнена в користувача)
і тіки цін символів, по яких у нього відкриті позиції.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..analytics import ExcursionTracker
from ..database import Database
from ..events import Event, EventBus
from ..exchange import BingXClient
from ..risk import RiskManager, TrailingStopManager
from ..state import SettingsManager
from ..trading import SimpleTrader

logger = logging.getLogger(__name__)


class UserSession:
    def __init__(
        self,
        user: dict,
        api_key: str,
        api_secret: str,
        mongo_client,
        config,
        strategy_settings,
        telegram_gateway=None,
        market_context_provider=None,
    ):
        self.user = user
        self.user_id = str(user['_id'])
        self.testnet = bool(user.get('testnet', True))
        self.config = config
        self.started = False

        self.db = Database(testnet=self.testnet, user_id=self.user_id, client=mongo_client)
        self.event_bus = EventBus()
        self.exchange = BingXClient(
            api_key=api_key, api_secret=api_secret, testnet=self.testnet, event_bus=self.event_bus,
        )
        self.settings_manager = SettingsManager(self.db, self.event_bus)
        self.risk_manager = RiskManager(
            self.db, self.event_bus, self.exchange, config.get('trading.risk'),
            settings_manager=self.settings_manager,
        )
        self.trader = SimpleTrader(
            exchange=self.exchange,
            event_bus=self.event_bus,
            db=self.db,
            risk_manager=self.risk_manager,
            settings_manager=self.settings_manager,
            strategy_settings=strategy_settings,
        )
        self.trader.market_context_provider = market_context_provider
        self.trailing = TrailingStopManager(
            event_bus=self.event_bus,
            exchange=self.exchange,
            db=self.db,
            trader=self.trader,
            config=config.get('trading.trailing_stop', {}),
        )
        self.excursion = ExcursionTracker(self.event_bus, self.db, self.trader, config.get('analytics', {}))

        self.notifier = None
        if telegram_gateway is not None:
            from ..telegram import TelegramBot
            self.notifier = TelegramBot(
                chat_id=self.user_id,
                event_bus=self.event_bus,
                gateway=telegram_gateway,
                exchange_client=self.exchange,
            )

    # ---------- життєвий цикл ----------

    async def start(self) -> None:
        await self.event_bus.start()
        if self.db.get_setting('trading.enabled') is None:
            await self.settings_manager.set_trading_enabled(True)
        await self.exchange.start_user_data_stream()
        self.started = True
        logger.info(
            f"[SESSION {self.user_id}] started ({'testnet' if self.testnet else 'LIVE'}, "
            f"{len(self.trader.open_positions)} open positions restored)"
        )

    async def stop(self) -> None:
        try:
            await self.exchange.stop_websocket()
        except Exception as e:
            logger.warning(f"[SESSION {self.user_id}] stop_websocket failed: {e}")
        try:
            await self.exchange.close()
        except Exception as e:
            logger.warning(f"[SESSION {self.user_id}] exchange close failed: {e}")
        await self.event_bus.stop()
        try:
            await self.excursion.flush_all()
        except Exception as e:
            logger.warning(f"[SESSION {self.user_id}] excursion flush failed: {e}")
        self.started = False
        logger.info(f"[SESSION {self.user_id}] stopped")

    # ---------- маршрутизація зі спільної шини ----------

    def held_symbols(self) -> set:
        return {p.get('symbol') for p in self.trader.open_positions.values() if p.get('symbol')}

    async def deliver(self, event: Event) -> None:
        await self.event_bus.publish(event)

    async def emergency_stop(self, close_positions: bool) -> int:
        """Telegram /emergency: вимкнути нові входи; за потреби закрити всі позиції."""
        await self.settings_manager.set_trading_enabled(False)
        await self.settings_manager.activate_emergency_stop(close_positions=close_positions)
        closed = 0
        if close_positions:
            for pos in list(self.trader.open_positions.values()):
                try:
                    if await self.trader.close_position(pos['symbol'], pos['side'], 'emergency'):
                        closed += 1
                except Exception as e:
                    logger.error(f"[SESSION {self.user_id}] emergency close {pos.get('symbol')} failed: {e}")
        return closed
