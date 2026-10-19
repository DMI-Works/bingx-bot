import logging
import json
from typing import Any

from ..database import Database
from ..events import EventBus, Event, EventType


logger = logging.getLogger(__name__)

TRADING_ENABLED_KEY = 'trading.enabled'
WHITELIST_KEY = 'trading.whitelist_symbols'
BLACKLIST_KEY = 'trading.blacklist_symbols'
EMERGENCY_KEY = 'emergency'

_LEGACY_BOOLS = {'True': True, 'False': False}


class SettingsManager:
    """Runtime-налаштування в колекції settings (значення — JSON-рядки) з
    кешем у пам'яті. Кожна зміна публікує SETTINGS_CHANGED."""

    def __init__(self, db: Database, event_bus: EventBus):
        self.db = db
        self.event_bus = event_bus
        self.cache = {}
        logger.info("SettingsManager initialized")

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.cache:
            return self.cache[key]

        value = self.db.get_setting(key)
        if value is None:
            return default

        parsed_value = self._decode(value)
        self.cache[key] = parsed_value
        return parsed_value

    @staticmethod
    def _decode(raw: str) -> Any:
        # Раніше bool писався як str(value) -> "True"/"False", а json.loads їх
        # не розуміє: після рестарту поверталась НЕпорожня строка "False",
        # тобто вимкнений kill-switch читався як увімкнений. Старі записи
        # мапимо явно; нові пишуться через json.dumps ("true"/"false").
        if raw in _LEGACY_BOOLS:
            return _LEGACY_BOOLS[raw]
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    async def set(self, key: str, value: Any) -> None:
        logger.info(f"Setting updated: {key} = {value}")

        try:
            value_str = json.dumps(value)
        except (TypeError, ValueError):
            value_str = str(value)
        self.db.save_setting(key, value_str)
        self.cache[key] = value

        await self.event_bus.publish(Event(
            type=EventType.SETTINGS_CHANGED,
            data={'key': key, 'value': value},
            source="SettingsManager"
        ))

    # ---------- глобальний kill-switch ----------

    def get_trading_enabled(self) -> bool:
        return self.get(TRADING_ENABLED_KEY, False)

    async def set_trading_enabled(self, enabled: bool) -> None:
        await self.set(TRADING_ENABLED_KEY, enabled)

    # ---------- whitelist / blacklist монет (мініапп «Монети») ----------

    async def _add_to_list(self, key: str, symbol: str) -> None:
        symbols = list(self.get(key, []))
        if symbol not in symbols:
            await self.set(key, symbols + [symbol])

    async def _remove_from_list(self, key: str, symbol: str) -> None:
        symbols = list(self.get(key, []))
        if symbol in symbols:
            symbols.remove(symbol)
            await self.set(key, symbols)

    def get_whitelist_symbols(self) -> list:
        return self.get(WHITELIST_KEY, [])

    async def add_whitelist_symbol(self, symbol: str) -> None:
        await self._add_to_list(WHITELIST_KEY, symbol)

    async def remove_whitelist_symbol(self, symbol: str) -> None:
        await self._remove_from_list(WHITELIST_KEY, symbol)

    def get_blacklist_symbols(self) -> list:
        return self.get(BLACKLIST_KEY, [])

    async def add_blacklist_symbol(self, symbol: str) -> None:
        await self._add_to_list(BLACKLIST_KEY, symbol)

    async def remove_blacklist_symbol(self, symbol: str) -> None:
        await self._remove_from_list(BLACKLIST_KEY, symbol)

    # ---------- аварійна зупинка (Telegram /emergency) ----------

    def get_emergency_stop_status(self) -> dict:
        return self.get(EMERGENCY_KEY, {'active': False, 'close_positions': False})

    async def activate_emergency_stop(self, close_positions: bool = False) -> None:
        await self.set(EMERGENCY_KEY, {'active': True, 'close_positions': close_positions})

        await self.event_bus.publish(Event(
            type=EventType.EMERGENCY_STOP_ACTIVATED,
            data={'close_positions': close_positions},
            source="SettingsManager"
        ))

    async def deactivate_emergency_stop(self) -> None:
        await self.set(EMERGENCY_KEY, {'active': False, 'close_positions': False})

        await self.event_bus.publish(Event(
            type=EventType.EMERGENCY_STOP_DEACTIVATED,
            data={},
            source="SettingsManager"
        ))
