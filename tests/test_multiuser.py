"""
Багатокористувацький режим: ізоляція даних, шифрування ключів, маршрутизація
сигналів і цін між сесіями, права адміна. Мережа й реальна БД не потрібні.

Запуск: python -m unittest tests.test_multiuser -v
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

from cryptography.fernet import Fernet  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from core.database.scoped import ScopedCollection, ScopedDatabase  # noqa: E402
from core.events import Event, EventBus, EventType  # noqa: E402
from core.users.access import can_open_new  # noqa: E402
from core.users.crypto import decrypt_secret, encrypt_secret  # noqa: E402
from core.users.repository import public_view  # noqa: E402


class TestScopedCollection(unittest.TestCase):
    def setUp(self):
        self.raw = mock.MagicMock()
        self.c = ScopedCollection(self.raw, "u1")

    def test_filters_get_user_id(self):
        self.c.find({"status": "OPEN"})
        self.assertEqual(self.raw.find.call_args.args[0], {"status": "OPEN", "user_id": "u1"})
        self.c.find_one({"order_id": "x"})
        self.assertEqual(self.raw.find_one.call_args.args[0], {"order_id": "x", "user_id": "u1"})
        self.c.count_documents({})
        self.assertEqual(self.raw.count_documents.call_args.args[0], {"user_id": "u1"})

    def test_caller_cannot_override_user_id_in_filter(self):
        self.c.find({"user_id": "victim"})
        self.assertEqual(self.raw.find.call_args.args[0]["user_id"], "u1")

    def test_insert_stamps_user_id(self):
        self.c.insert_one({"a": 1, "user_id": "victim"})
        self.assertEqual(self.raw.insert_one.call_args.args[0], {"a": 1, "user_id": "u1"})

    def test_aggregate_starts_with_match(self):
        self.c.aggregate([{"$group": {"_id": "$symbol"}}])
        self.assertEqual(self.raw.aggregate.call_args.args[0][0], {"$match": {"user_id": "u1"}})

    def test_update_and_delete_scoped(self):
        self.c.update_one({"key": "k"}, {"$set": {"v": 1}}, upsert=True)
        self.assertEqual(self.raw.update_one.call_args.args[0], {"key": "k", "user_id": "u1"})
        self.c.delete_one({"order_id": "x"})
        self.assertEqual(self.raw.delete_one.call_args.args[0], {"order_id": "x", "user_id": "u1"})

    def test_cannot_move_row_to_another_user(self):
        with self.assertRaises(ValueError):
            self.c.update_one({"key": "k"}, {"$set": {"user_id": "victim"}})

    def test_index_prefixed_with_user(self):
        self.c.create_index("key", unique=True)
        self.assertEqual(self.raw.create_index.call_args.args[0], [("user_id", 1), ("key", 1)])

    def test_unsupported_method_does_not_bypass_scope(self):
        with self.assertRaises(AttributeError):
            self.c.drop()

    def test_database_wrapper(self):
        raw_db = mock.MagicMock()
        self.assertIsInstance(ScopedDatabase(raw_db, "u1").positions, ScopedCollection)


class TestCrypto(unittest.TestCase):
    def test_roundtrip_and_not_plaintext(self):
        key = Fernet.generate_key().decode()
        token = encrypt_secret("my-secret", key)
        self.assertNotIn("my-secret", token)
        self.assertEqual(decrypt_secret(token, key), "my-secret")

    def test_wrong_key_fails(self):
        token = encrypt_secret("s", Fernet.generate_key().decode())
        with self.assertRaises(ValueError):
            decrypt_secret(token, Fernet.generate_key().decode())

    def test_public_view_hides_secrets(self):
        user = {"_id": "1", "keys": {"live": {
            "api_key_enc": "ENC1", "api_secret_enc": "ENC2", "key_suffix": "abcd",
        }}}
        view = public_view(user)
        self.assertTrue(view["keys"]["live"]["connected"])
        self.assertEqual(view["keys"]["live"]["key_suffix"], "abcd")
        self.assertNotIn("ENC1", str(view))
        self.assertNotIn("ENC2", str(view))


class TestAccess(unittest.TestCase):
    def test_active_and_blocked(self):
        self.assertTrue(can_open_new({"status": "active"})[0])
        self.assertFalse(can_open_new({"status": "blocked"})[0])
        self.assertFalse(can_open_new(None)[0])


def _session(user_id, held=(), status="active"):
    session = mock.MagicMock()
    session.user_id = user_id
    session.user = {"_id": user_id, "status": status}
    session.held_symbols.return_value = set(held)
    session.deliver = mock.AsyncMock()
    return session


class TestRouting(unittest.IsolatedAsyncioTestCase):
    def _manager(self, *sessions):
        from core.engine.session_manager import SessionManager
        manager = SessionManager(
            users=mock.MagicMock(), mongo_client=mock.MagicMock(), config=mock.MagicMock(),
            shared_bus=EventBus(), strategy_settings=mock.MagicMock(),
        )
        manager.sessions = {s.user_id: s for s in sessions}
        return manager

    def _signal(self, **extra):
        return Event(type=EventType.SIGNAL_GENERATED, data={
            "action": "OPEN", "symbol": "BTC-USDT", "strategy": "S", **extra,
        })

    async def test_signal_reaches_each_enabled_user(self):
        a, b = _session("a"), _session("b")
        await self._manager(a, b)._on_signal(self._signal())
        a.deliver.assert_awaited_once()
        b.deliver.assert_awaited_once()

    async def test_blocked_user_gets_no_new_entries(self):
        a = _session("a", status="blocked")
        await self._manager(a)._on_signal(self._signal())
        a.deliver.assert_not_awaited()

    async def test_target_still_delivered_but_no_open_when_not_allowed(self):
        # закриття по розвороту має працювати і без права на нові входи
        a = _session("a", status="blocked")
        b = _session("b")
        target = Event(type=EventType.SIGNAL_GENERATED, data={
            "action": "TARGET", "symbol": "BTC-USDT", "side": "LONG", "strategy": "S",
        })
        await self._manager(a, b)._on_signal(target)
        self.assertTrue(a.deliver.call_args.args[0].data["no_open"])
        self.assertNotIn("no_open", b.deliver.call_args.args[0].data)

    async def test_failure_of_one_user_does_not_stop_others(self):
        a, b = _session("a"), _session("b")
        a.deliver = mock.AsyncMock(side_effect=RuntimeError("boom"))
        await self._manager(a, b)._on_signal(self._signal())
        b.deliver.assert_awaited_once()

    async def test_price_only_to_holders(self):
        a, b = _session("a", held=["BTC-USDT"]), _session("b", held=["ETH-USDT"])
        await self._manager(a, b)._on_price(Event(
            type=EventType.PRICE_UPDATED, data={"s": "BTC-USDT", "p": "100"},
        ))
        a.deliver.assert_awaited_once()
        b.deliver.assert_not_awaited()

    def test_held_symbols_union(self):
        manager = self._manager(_session("a", held=["BTC-USDT"]), _session("b", held=["ETH-USDT"]))
        self.assertEqual(manager.held_symbols(), {"BTC-USDT", "ETH-USDT"})


class TestReentryPerUser(unittest.IsolatedAsyncioTestCase):
    async def test_cooldown_is_per_trader(self):
        import core.trading.simple_trader as st
        trader = st.SimpleTrader.__new__(st.SimpleTrader)
        trader.settings_manager = None
        trader._last_close_at = {}
        trader._loss_close_at = {}
        trader.open_position = mock.AsyncMock(return_value=True)
        signal = {
            "action": "OPEN", "symbol": "KAIA-USDT", "side": "LONG", "strategy": "W", "quantity": 1,
            "reentry_cooldown_seconds": 900,
        }
        await trader._remember_close(Event(
            type=EventType.POSITION_CLOSED, data={"symbol": "KAIA-USDT", "strategy": "W"},
        ))
        await trader._handle_signal(Event(type=EventType.SIGNAL_GENERATED, data=signal))
        trader.open_position.assert_not_awaited()

        trader._last_close_at[("KAIA-USDT", "W")] -= 901
        await trader._handle_signal(Event(type=EventType.SIGNAL_GENERATED, data=signal))
        trader.open_position.assert_awaited_once()


class TestDeps(unittest.TestCase):
    def test_non_admin_rejected(self):
        from webapp.backend.deps import require_admin
        with self.assertRaises(HTTPException) as ctx:
            require_admin({"_id": "1", "role": "user"})
        self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_passes(self):
        from webapp.backend.deps import require_admin
        self.assertEqual(require_admin({"_id": "1", "role": "admin"})["_id"], "1")

    def test_invalid_init_data_rejected(self):
        from webapp.backend.deps import require_user
        with self.assertRaises(HTTPException) as ctx:
            require_user(mock.MagicMock(), x_telegram_init_data="")
        self.assertEqual(ctx.exception.status_code, 401)


class TestUserSessionWiring(unittest.IsolatedAsyncioTestCase):
    """Контур користувача збирається і працює з реальними класами (Mongo/біржа — підміна)."""

    async def test_two_sessions_start_route_and_stop(self):
        from core.engine.session_manager import SessionManager
        from core.exchange.bingx_client import BingXClient

        mongo = mock.MagicMock()
        mongo.__getitem__.return_value.__getitem__.return_value.find_one.return_value = None
        mongo.__getitem__.return_value.__getitem__.return_value.find.return_value.sort.return_value = []
        config = mock.MagicMock()
        config.get.side_effect = lambda key, default=None: {
            "trading.risk": {"max_open_positions": 3},
            "trading.trailing_stop": {"enabled": True},
            "analytics": {},
        }.get(key, default)

        users = mock.MagicMock()
        docs = {str(i): {"_id": str(i), "status": "active", "testnet": True} for i in (1, 2)}
        users.get.side_effect = lambda user_id: docs.get(str(user_id))
        users.list_with_keys.return_value = list(docs.values())
        users.decrypted_keys.return_value = {"api_key": "k", "api_secret": "s"}

        shared = EventBus()
        manager = SessionManager(users, mongo, config, shared, strategy_settings=mock.MagicMock())
        with mock.patch.object(BingXClient, "start_user_data_stream", mock.AsyncMock()), \
                mock.patch.object(BingXClient, "stop_websocket", mock.AsyncMock()), \
                mock.patch.object(BingXClient, "close", mock.AsyncMock()):
            await manager.start_all()
            self.assertEqual(set(manager.sessions), {"1", "2"})
            one, two = manager.sessions["1"], manager.sessions["2"]
            self.assertIsNot(one.event_bus, two.event_bus)
            self.assertIsNot(one.exchange, two.exchange)
            self.assertEqual(one.db.user_id, "1")
            self.assertEqual(two.db.user_id, "2")

            one.trader.open_position = mock.AsyncMock(return_value=True)
            two.trader.open_position = mock.AsyncMock(return_value=True)
            manager.sessions["2"].user["status"] = "blocked"
            await manager._on_signal(Event(type=EventType.SIGNAL_GENERATED, data={
                "action": "OPEN", "symbol": "BTC-USDT", "side": "LONG", "strategy": "S", "quantity": 1,
            }))
            for _ in range(50):
                if one.trader.open_position.await_count:
                    break
                await __import__("asyncio").sleep(0.02)
            one.trader.open_position.assert_awaited_once()
            two.trader.open_position.assert_not_awaited()
            await manager.stop_all()
        self.assertEqual(manager.sessions, {})


class TestEveryEndpointRequiresAuth(unittest.TestCase):
    """Раніше читання (статистика, позиції) було відкрите будь-кому, хто знає URL."""

    def test_all_api_routes_reject_missing_init_data(self):
        from fastapi.testclient import TestClient
        from webapp.backend.api import app
        client = TestClient(app, raise_server_exceptions=True)
        checked = 0
        for route in app.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/api"):
                continue
            url = path.replace("{name}", "X").replace("{symbol}", "X").replace("{order_id}", "X")
            url = url.replace("{user_id}", "X").replace("{invoice_id}", "X")
            for method in route.methods - {"HEAD", "OPTIONS"}:
                query = "?testnet=true" if path == "/api/account/keys" and method == "DELETE" else ""
                body = {"json": {}} if method == "POST" else {}
                response = client.request(method, url + query, **body)
                self.assertEqual(response.status_code, 401, f"{method} {path} -> {response.status_code}")
                checked += 1
        self.assertGreater(checked, 15)


class TestStrategiesAdminOnly(unittest.TestCase):
    def _settings_for(self, role):
        from webapp.backend.routes import settings as routes

        session = mock.MagicMock()
        session.user = {"role": role}
        store = mock.MagicMock()
        store.is_modified.return_value = False
        store.list_strategies.return_value = [
            {"strategy_name": "S", "enabled": False, "updated_at": None, "params": {"leverage": 10}},
        ]
        with mock.patch.object(routes, "require_strategy_settings", return_value=store):
            return routes.get_settings(mock.MagicMock(), session)

    def test_user_gets_no_strategies(self):
        data = self._settings_for("user")
        self.assertFalse(data["is_admin"])
        self.assertEqual(data["strategies"], [])

    def test_admin_gets_one_shared_flag(self):
        data = self._settings_for("admin")
        self.assertTrue(data["is_admin"])
        strategy = data["strategies"][0]
        self.assertFalse(strategy["enabled"])
        self.assertNotIn("global_enabled", strategy)
        self.assertEqual(strategy["params"][0]["key"], "leverage")

    def test_enabled_endpoint_requires_admin(self):
        from webapp.backend.deps import require_admin
        from webapp.backend.routes.settings import set_strategy_enabled
        import inspect

        defaults = [p.default for p in inspect.signature(set_strategy_enabled).parameters.values()]
        self.assertTrue(any(getattr(d, "dependency", None) is require_admin for d in defaults))


class TestCoinsReadOnlyForUsers(unittest.TestCase):
    def test_every_coin_change_requires_admin(self):
        from webapp.backend.deps import require_admin
        from webapp.backend.routes.symbols import router

        changing = [r for r in router.routes if r.methods & {"POST", "PUT", "PATCH", "DELETE"}]
        self.assertEqual(len(changing), 4)
        for route in changing:
            calls = [d.call for d in route.dependant.dependencies]
            self.assertIn(require_admin, calls, route.path)


class TestAdminId(unittest.TestCase):
    def test_admin_only_from_admin_telegram_id(self):
        from core.users.repository import admin_telegram_id

        with mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "-100"}, clear=False):
            os.environ.pop("ADMIN_TELEGRAM_ID", None)
            self.assertIsNone(admin_telegram_id())
            os.environ["ADMIN_TELEGRAM_ID"] = " 42 "
            self.assertEqual(admin_telegram_id(), "42")


if __name__ == "__main__":
    unittest.main()
