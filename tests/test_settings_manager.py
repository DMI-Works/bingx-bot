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
    async def test_max_open_positions_survives_restart(self):
        db = FakeSettingsDb()
        manager = SettingsManager(db, EventBus())
        self.assertIsNone(manager.get_max_open_positions())
        await manager.set_max_open_positions(5)
        self.assertEqual(SettingsManager(db, EventBus()).get_max_open_positions(), 5)

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


class TestSymbolPauseAfterLosses(unittest.IsolatedAsyncioTestCase):
    def _risk_manager(self, settings):
        from unittest import mock
        from core.risk import RiskManager
        bus = EventBus()
        bus.publish = mock.AsyncMock()
        rm = RiskManager(db=None, event_bus=bus, exchange=mock.MagicMock(),
                         config={'max_consecutive_losses': 2, 'symbol_pause_hours': 24},
                         settings_manager=settings)
        rm._get_real_open_positions = mock.AsyncMock(return_value=[])
        return rm

    def setUp(self):
        self.settings = SettingsManager(FakeSettingsDb(), EventBus())

    async def _streak(self, rm):
        await rm.position_closed(-1.0, 'BTC-USDT')
        await rm.position_closed(-1.0, 'BTC-USDT')

    async def test_streak_pauses_coin_without_blacklisting(self):
        from core.events import EventType
        rm = self._risk_manager(self.settings)
        await self._streak(rm)
        allowed, reason = await rm.can_open_position('BTC-USDT')
        self.assertFalse(allowed)
        self.assertIn('paused', reason)
        self.assertEqual(self.settings.get_blacklist_symbols(), [])
        self.assertTrue((await rm.can_open_position('ETH-USDT'))[0])
        event = rm.event_bus.publish.await_args.args[0]
        self.assertEqual(event.type, EventType.SYMBOL_PAUSED)
        self.assertEqual(event.data['pause_hours'], 24)

    async def test_entries_resume_after_pause(self):
        from unittest import mock
        import time
        rm = self._risk_manager(self.settings)
        await self._streak(rm)
        with mock.patch('core.risk.risk_manager.time.time', return_value=time.time() + 24 * 3600 + 1):
            self.assertTrue((await rm.can_open_position('BTC-USDT'))[0])
        self.assertEqual(rm.consecutive_losses, {})

    async def test_pause_survives_restart(self):
        rm = self._risk_manager(self.settings)
        await self._streak(rm)
        restarted = self._risk_manager(self.settings)
        self.assertFalse((await restarted.can_open_position('BTC-USDT'))[0])

    async def test_loss_during_pause_not_counted(self):
        rm = self._risk_manager(self.settings)
        await self._streak(rm)
        await rm.position_closed(-1.0, 'BTC-USDT')
        self.assertEqual(rm.consecutive_losses, {})


class TestMaxOpenPositionsOverride(unittest.IsolatedAsyncioTestCase):
    async def _risk_manager(self, open_count):
        from unittest import mock
        from core.risk import RiskManager
        settings = SettingsManager(FakeSettingsDb(), EventBus())
        rm = RiskManager(db=None, event_bus=EventBus(), exchange=mock.MagicMock(),
                         config={'max_open_positions': 3, 'max_consecutive_losses': 0},
                         settings_manager=settings)
        rm._get_real_open_positions = mock.AsyncMock(
            return_value=[{'symbol': f'C{i}-USDT'} for i in range(open_count)]
        )
        return rm, settings

    async def test_yaml_value_without_override(self):
        rm, _ = await self._risk_manager(3)
        self.assertEqual(rm.effective_max_open_positions(), 3)
        self.assertFalse((await rm.can_open_position('BTC-USDT'))[0])

    async def test_raised_limit_allows_entry(self):
        rm, settings = await self._risk_manager(3)
        await settings.set_max_open_positions(5)
        self.assertTrue((await rm.can_open_position('BTC-USDT'))[0])

    async def test_lowered_limit_blocks_entry(self):
        rm, settings = await self._risk_manager(1)
        await settings.set_max_open_positions(1)
        ok, reason = await rm.can_open_position('BTC-USDT')
        self.assertFalse(ok)
        self.assertIn('1', reason)


if __name__ == "__main__":
    unittest.main()
