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
    """Режим у кожного свій: перемикання перезапускає лише сесію цього користувача."""

    def setUp(self):
        from webapp.backend.routes import settings as settings_route
        self.route = settings_route
        self.user = {"_id": "1", "testnet": True}
        self.users = mock.MagicMock()
        self.users.has_keys.return_value = True
        self.sessions = mock.MagicMock()
        self.sessions.start_user = mock.AsyncMock(return_value=object())
        self.session = _State()
        self.session.trader = _State()
        self.session.trader.open_positions = {}
        self.sessions.get.return_value = self.session
        self.req = _Request(users=self.users, sessions=self.sessions)

    async def test_blocked_when_open_positions(self):
        self.session.trader.open_positions = {"BTC-USDT_LONG": object()}
        with self.assertRaises(HTTPException) as ctx:
            await self.route.set_mode(self.req, {"testnet": False}, user=self.user)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["open_positions"], 1)
        self.sessions.start_user.assert_not_awaited()

    async def test_noop_same_mode(self):
        res = await self.route.set_mode(self.req, {"testnet": True}, user=self.user)
        self.assertFalse(res["restarting"])
        self.sessions.start_user.assert_not_awaited()

    async def test_no_keys_for_target_mode(self):
        self.users.has_keys.return_value = False
        with self.assertRaises(HTTPException) as ctx:
            await self.route.set_mode(self.req, {"testnet": False}, user=self.user)
        self.assertEqual(ctx.exception.detail["code"], "no_keys")

    async def test_success_restarts_only_this_user(self):
        res = await self.route.set_mode(self.req, {"testnet": False}, user=self.user)
        self.assertTrue(res["restarting"])
        self.users.set_testnet.assert_called_once_with("1", False)
        self.sessions.start_user.assert_awaited_once_with("1")

    async def test_failed_start_rolls_back_mode(self):
        self.sessions.start_user = mock.AsyncMock(side_effect=[None, object()])
        with self.assertRaises(HTTPException):
            await self.route.set_mode(self.req, {"testnet": False}, user=self.user)
        self.users.set_testnet.assert_called_with("1", True)

    async def test_force_bypasses_open_positions(self):
        self.session.trader.open_positions = {"X_LONG": object()}
        res = await self.route.set_mode(self.req, {"testnet": False, "force": True}, user=self.user)
        self.assertTrue(res["restarting"])


if __name__ == "__main__":
    unittest.main()
