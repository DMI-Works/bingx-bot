import logging
import json
from typing import Any, Optional

from ..database import Database
from ..events import EventBus, Event, EventType


logger = logging.getLogger(__name__)


class SettingsManager:
    def __init__(self, db: Database, event_bus: EventBus):
        self.db = db
        self.event_bus = event_bus
        self.cache = {}
        self._load_settings()
        logger.info("SettingsManager initialized")

    def _load_settings(self) -> None:
        logger.info("Loading settings from database")

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.cache:
            return self.cache[key]

        value = self.db.get_setting(key)

        if value is None:
            return default

        try:
            parsed_value = json.loads(value)
            self.cache[key] = parsed_value
            return parsed_value
        except json.JSONDecodeError:
            self.cache[key] = value
            return value

    async def set(self, key: str, value: Any) -> None:
        logger.info(f"Setting updated: {key} = {value}")

        if isinstance(value, (dict, list)):
            value_str = json.dumps(value)
        else:
            value_str = str(value)

        self.db.save_setting(key, value_str)
        self.cache[key] = value

        await self.event_bus.publish(Event(
            type=EventType.SETTINGS_CHANGED,
            data={'key': key, 'value': value},
            source="SettingsManager"
        ))

    def get_trading_enabled(self) -> bool:
        return self.get('trading.enabled', False)

    async def set_trading_enabled(self, enabled: bool) -> None:
        await self.set('trading.enabled', enabled)

    def get_max_open_positions(self) -> int:
        return self.get('risk.max_open_positions', 3)

    async def set_max_open_positions(self, value: int) -> None:
        await self.set('risk.max_open_positions', value)

    def get_whitelist_symbols(self) -> list:
        return self.get('trading.whitelist_symbols', [])

    async def set_whitelist_symbols(self, symbols: list) -> None:
        await self.set('trading.whitelist_symbols', symbols)

    async def add_whitelist_symbol(self, symbol: str) -> None:
        symbols = self.get_whitelist_symbols()
        if symbol not in symbols:
            symbols.append(symbol)
            await self.set_whitelist_symbols(symbols)

    async def remove_whitelist_symbol(self, symbol: str) -> None:
        symbols = self.get_whitelist_symbols()
        if symbol in symbols:
            symbols.remove(symbol)
            await self.set_whitelist_symbols(symbols)

    def get_blacklist_symbols(self) -> list:
        return self.get('trading.blacklist_symbols', [])

    async def set_blacklist_symbols(self, symbols: list) -> None:
        await self.set('trading.blacklist_symbols', symbols)

    def get_position_size_config(self) -> dict:
        return self.get('trading.position_size', {'mode': 'fixed_usd', 'value': 100})

    async def set_position_size_config(self, config: dict) -> None:
        await self.set('trading.position_size', config)

    def get_stop_loss_config(self) -> dict:
        return self.get('trading.stop_loss', {'mode': 'fixed_percent', 'value': 2.0})

    async def set_stop_loss_config(self, config: dict) -> None:
        await self.set('trading.stop_loss', config)

    def get_take_profit_config(self) -> dict:
        return self.get('trading.take_profit', {
            'enabled': True,
            'levels': [
                {'percent': 3.0, 'close_percent': 50},
                {'percent': 5.0, 'close_percent': 50}
            ]
        })

    async def set_take_profit_config(self, config: dict) -> None:
        await self.set('trading.take_profit', config)

    def get_leverage(self, symbol: str) -> int:
        leverage_config = self.get('trading.leverage_by_symbol', {})
        return leverage_config.get(symbol, self.get('trading.default_leverage', 10))

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        leverage_config = self.get('trading.leverage_by_symbol', {})
        leverage_config[symbol] = leverage
        await self.set('trading.leverage_by_symbol', leverage_config)

    def get_emergency_stop_status(self) -> dict:
        return self.get('emergency', {'active': False, 'close_positions': False})

    async def activate_emergency_stop(self, close_positions: bool = False) -> None:
        await self.set('emergency', {'active': True, 'close_positions': close_positions})

        await self.event_bus.publish(Event(
            type=EventType.EMERGENCY_STOP_ACTIVATED,
            data={'close_positions': close_positions},
            source="SettingsManager"
        ))

    async def deactivate_emergency_stop(self) -> None:
        await self.set('emergency', {'active': False, 'close_positions': False})

        await self.event_bus.publish(Event(
            type=EventType.EMERGENCY_STOP_DEACTIVATED,
            data={},
            source="SettingsManager"
        ))

    def reload(self) -> None:
        self.cache.clear()
        self._load_settings()
        logger.info("Settings reloaded")
