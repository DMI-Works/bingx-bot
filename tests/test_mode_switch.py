"""
Юніт-тести перемикання режиму testnet <-> live з Mini App:
  - чисті хелпери mongo_config (resolve_testnet / db_name_for / mode_str);
  - Database(testnet=...) обирає правильну базу і trade_mode (без реального
    коннекту — MongoClient мокнутий);
  - ендпоінт /api/settings/mode: guard від зміни режиму з відкритими позиціями
    (409), no-op у той самий режим, успішний шлях ставить restart_event.

Запуск: python -m unittest tests.test_mode_switch -v
MONGO_URI потрібен лише для імпорту core.* (див. tests/test_pnl_accounting.py).
"""
import asyncio
import os
import unittest
from unittest import mock

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
# webapp.backend.auth читає токен на рівні модуля при імпорті (як і в проді)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

from fastapi import HTTPException  # noqa: E402

from core.database import mongo_config  # noqa: E402
from core.database.mongo_config import (  # noqa: E402
    base_db_name,
    db_name_for,
    mode_str,
    resolve_testnet,
)


class TestPureHelpers(unittest.TestCase):
    def test_resolve_testnet_override_wins(self):
        # override (з БД) важливіший за дефолт із config.yaml
        self.assertTrue(resolve_testnet(False, True))
        self.assertFalse(resolve_testnet(True, False))

    def test_resolve_testnet_none_falls_back_to_default(self):
        self.assertTrue(resolve_testnet(True, None))
        self.assertFalse(resolve_testnet(False, None))

    def test_db_name_for(self):
        self.assertEqual(db_name_for(False), base_db_name())
        self.assertEqual(db_name_for(True), f"{base_db_name()}_testnet")

    def test_mode_str(self):
        self.assertEqual(mode_str(True), "testnet")
        self.assertEqual(mode_str(False), "live")


class TestDatabaseMode(unittest.TestCase):
    def _make_db(self, testnet):
        # MongoClient мокнутий: __init__ викликає _init_database, але мережі нема.
        with mock.patch("core.database.database.MongoClient") as mc:
            mc.return_value.__getitem__.return_value = mock.MagicMock()
            from core.database.database import Database
            return Database(testnet=testnet)

    def test_testnet_true(self):
        db = self._make_db(True)
        self.assertTrue(db.testnet)
        self.assertEqual(db.db_name, f"{base_db_name()}_testnet")
        self.assertEqual(db.trade_mode, "testnet")

    def test_testnet_false(self):
        db = self._make_db(False)
        self.assertFalse(db.testnet)
        self.assertEqual(db.db_name, base_db_name())
        self.assertEqual(db.trade_mode, "live")


class _State:
    pass


class _Request:
    """Мінімальний фейк Request: ендпоінтам потрібен лише request.app.state."""
    def __init__(self, **state):
        self.app = _State()
        self.app.state = _State()
        for k, v in state.items():
            setattr(self.app.state, k, v)


class TestModeEndpoint(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from webapp.backend.routes import settings as settings_route
        self.route = settings_route

    async def test_blocked_when_open_positions(self):
        trader = _State(); trader.open_positions = {"BTC-USDT_LONG": object()}
        req = _Request(testnet=True, trader=trader,
                       restart_event=asyncio.Event(), db=_State())
        with self.assertRaises(HTTPException) as ctx:
            await self.route.set_mode(req, {"testnet": False}, user={})
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["open_positions"], 1)
        self.assertFalse(req.app.state.restart_event.is_set())

    async def test_noop_same_mode(self):
        req = _Request(testnet=True)
        res = await self.route.set_mode(req, {"testnet": True}, user={})
        self.assertFalse(res["restarting"])

    async def test_success_sets_restart_event(self):
        trader = _State(); trader.open_positions = {}
        db = _State(); db.client = mock.MagicMock()
        ev = asyncio.Event()
        req = _Request(testnet=True, trader=trader, restart_event=ev, db=db)
        res = await self.route.set_mode(req, {"testnet": False}, user={})
        self.assertTrue(res["restarting"])
        self.assertFalse(res["testnet"])
        self.assertTrue(ev.is_set())
        db.client.__getitem__.assert_called()  # save_mode_override звернувся до БД

    async def test_force_bypasses_open_positions(self):
        trader = _State(); trader.open_positions = {"X_LONG": object()}
        db = _State(); db.client = mock.MagicMock()
        ev = asyncio.Event()
        req = _Request(testnet=False, trader=trader, restart_event=ev, db=db)
        res = await self.route.set_mode(req, {"testnet": True, "force": True}, user={})
        self.assertTrue(res["restarting"])
        self.assertTrue(ev.is_set())


if __name__ == "__main__":
    unittest.main()
