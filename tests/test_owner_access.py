"""Доступ до Mini App і Telegram-панелі — лише власнику бота."""

import hashlib
import hmac
import json
import os
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")

from core.common.owner_access import is_owner, owner_ids  # noqa: E402
from webapp.backend import auth  # noqa: E402


def signed_init_data(user_id, auth_date=None, token=None):
    """initData, підписаний так само, як це робить Telegram."""
    fields = {
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "user": json.dumps({"id": user_id, "username": f"u{user_id}"}),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", (token or auth.BOT_TOKEN).encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class TestOwnerIds(unittest.TestCase):
    def test_admin_ids_and_private_chat(self):
        with mock.patch.dict(os.environ, {"ADMIN_TELEGRAM_ID": "111, 222", "TELEGRAM_CHAT_ID": "333"}):
            self.assertEqual(owner_ids(), {"111", "222", "333"})
            self.assertTrue(is_owner(222))
            self.assertFalse(is_owner(444))

    def test_group_chat_gives_no_access(self):
        with mock.patch.dict(os.environ, {"ADMIN_TELEGRAM_ID": "", "TELEGRAM_CHAT_ID": "-1001234"}):
            self.assertEqual(owner_ids(), set())
            self.assertFalse(is_owner(1234))
            self.assertFalse(is_owner(None))


class TestValidateInitData(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(auth.validate_init_data(signed_init_data(42))["id"], 42)

    def test_garbage_is_rejected_not_raised(self):
        for bad in ("abc", "hash=1&auth_date=x", "user=%7B&hash=x", "a=1&&b"):
            self.assertIsNone(auth.validate_init_data(bad), bad)

    def test_wrong_token(self):
        self.assertIsNone(auth.validate_init_data(signed_init_data(42, token="999:OTHER")))

    def test_expired_and_future(self):
        self.assertIsNone(auth.validate_init_data(signed_init_data(42, auth_date=time.time() - 7200)))
        self.assertIsNone(auth.validate_init_data(signed_init_data(42, auth_date=time.time() + 3600)))


class TestApiRequiresOwner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from webapp.backend.api import app
        cls.client = TestClient(app)

    def _get(self, path, init_data=None):
        headers = {"X-Telegram-Init-Data": init_data} if init_data is not None else {}
        return self.client.get(path, headers=headers)

    @staticmethod
    def _api_routes():
        # маршрути беремо з роутерів модулів: app.routes у нових FastAPI
        # містить обгортки _IncludedRouter замість самих маршрутів
        from webapp.backend.routes import analytics, profile, settings, symbols, trades
        for module in (trades, analytics, profile, settings, symbols):
            for route in module.router.routes:
                if getattr(route, "path", "").startswith("/api"):
                    yield route

    def test_every_api_route_is_protected(self):
        routes = list(self._api_routes())
        self.assertGreater(len(routes), 10)
        with mock.patch.dict(os.environ, {"ADMIN_TELEGRAM_ID": "111", "TELEGRAM_CHAT_ID": ""}):
            for route in routes:
                url = route.path.replace("{name}", "X").replace("{symbol}", "X").replace("{order_id}", "X")
                for method in route.methods:
                    resp = self.client.request(method, url, headers={"X-Telegram-Init-Data": signed_init_data(222)})
                    self.assertEqual(resp.status_code, 403, f"{method} {route.path}")
                    resp = self.client.request(method, url)
                    self.assertEqual(resp.status_code, 401, f"{method} {route.path}")

    def test_owner_passes_auth(self):
        with mock.patch.dict(os.environ, {"ADMIN_TELEGRAM_ID": "111", "TELEGRAM_CHAT_ID": ""}):
            # бот не піднятий (app.state порожній) — 503, але НЕ 401/403
            self.assertEqual(self._get("/api/stats", signed_init_data(111)).status_code, 503)

    def test_no_owner_configured_denies_all(self):
        with mock.patch.dict(os.environ, {"ADMIN_TELEGRAM_ID": "", "TELEGRAM_CHAT_ID": "-100"}):
            self.assertEqual(self._get("/api/stats", signed_init_data(111)).status_code, 403)


if __name__ == "__main__":
    unittest.main()
