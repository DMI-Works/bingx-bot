"""
SettingsManager: значення переживають "рестарт" (новий інстанс з тією ж БД)
без зміни типу. Регресія: bool писався як str(False) -> "False", а після
рестарту читався як непорожня строка, тобто вимкнений kill-switch
trading.enabled вважався увімкненим.
"""
import os
import unittest

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")

from core.events import EventBus  # noqa: E402
from core.state import SettingsManager  # noqa: E402


class FakeSettingsDb:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get_setting(self, key):
        return self.data.get(key)

    def save_setting(self, key, value):
        self.data[key] = value


class TestSettingsRoundTrip(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_trading_survives_restart(self):
        db = FakeSettingsDb()
        await SettingsManager(db, EventBus()).set_trading_enabled(False)
        restarted = SettingsManager(db, EventBus())
        self.assertIs(restarted.get_trading_enabled(), False)

    async def test_values_keep_their_types(self):
        db = FakeSettingsDb()
        manager = SettingsManager(db, EventBus())
        await manager.set('a.bool', True)
        await manager.set('a.int', 5)
        await manager.set('a.list', ['BTC-USDT'])
        await manager.set('a.dict', {'streak': [1.0], 'blocked_until': 0.0})
        restarted = SettingsManager(db, EventBus())
        self.assertIs(restarted.get('a.bool'), True)
        self.assertEqual(restarted.get('a.int'), 5)
        self.assertEqual(restarted.get('a.list'), ['BTC-USDT'])
        self.assertEqual(restarted.get('a.dict'), {'streak': [1.0], 'blocked_until': 0.0})

    def test_legacy_python_bool_strings_are_read_as_bools(self):
        manager = SettingsManager(FakeSettingsDb({'trading.enabled': 'False'}), EventBus())
        self.assertIs(manager.get_trading_enabled(), False)
        manager = SettingsManager(FakeSettingsDb({'trading.enabled': 'True'}), EventBus())
        self.assertIs(manager.get_trading_enabled(), True)

    async def test_blacklist_add_remove_is_idempotent(self):
        db = FakeSettingsDb()
        manager = SettingsManager(db, EventBus())
        await manager.add_blacklist_symbol('BTC-USDT')
        await manager.add_blacklist_symbol('BTC-USDT')
        self.assertEqual(SettingsManager(db, EventBus()).get_blacklist_symbols(), ['BTC-USDT'])
        await manager.remove_blacklist_symbol('BTC-USDT')
        await manager.remove_blacklist_symbol('BTC-USDT')
        self.assertEqual(SettingsManager(db, EventBus()).get_blacklist_symbols(), [])


if __name__ == "__main__":
    unittest.main()
