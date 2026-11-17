"""Заголовки безпеки, закриті /docs, ліміти частоти запитів."""

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

from fastapi.testclient import TestClient  # noqa: E402

from webapp.backend import auth  # noqa: E402
from webapp.backend.api import app  # noqa: E402
from webapp.backend.rate_limit import KEYS_LIMIT, SlidingWindowLimiter, limiter  # noqa: E402


def signed_init_data(user_id):
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", auth.BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class FakeUsers:
    def __init__(self, role="user"):
        self.role = role

    def ensure_user(self, tg_user):
        return {"_id": str(tg_user["id"]), "role": self.role, "username": "u"}


class TestSlidingWindow(unittest.TestCase):
    def test_limit_and_recovery(self):
        lim = SlidingWindowLimiter()
        with mock.patch("webapp.backend.rate_limit.time.monotonic", side_effect=[0, 1, 2, 3, 61.5]):
            self.assertEqual(lim.hit("b", "k", 3, 60), 0)
            self.assertEqual(lim.hit("b", "k", 3, 60), 0)
            self.assertEqual(lim.hit("b", "k", 3, 60), 0)
            self.assertGreater(lim.hit("b", "k", 3, 60), 0)   # 4-й за хвилину — чекати
            self.assertEqual(lim.hit("b", "k", 3, 60), 0)     # перший вийшов з вікна


class TestHttpSecurity(unittest.TestCase):
    def setUp(self):
        limiter.reset()
        self.client = TestClient(app)
        self._users = app.state.users

    def tearDown(self):
        app.state.users = self._users
        limiter.reset()

    def test_security_headers(self):
        resp = self.client.get("/api/me")
        for header in ("Content-Security-Policy", "Strict-Transport-Security",
                       "X-Content-Type-Options", "Referrer-Policy"):
            self.assertIn(header, resp.headers)
        self.assertIn("frame-ancestors", resp.headers["Content-Security-Policy"])
        self.assertEqual(resp.headers["Cache-Control"], "no-store")

    def test_no_cors_for_foreign_origin(self):
        resp = self.client.options("/api/me", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "GET",
        })
        self.assertNotIn("access-control-allow-origin", resp.headers)

    def test_docs_closed(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_garbage_init_data_is_401(self):
        self.assertEqual(self.client.get("/api/me", headers={"X-Telegram-Init-Data": "abc"}).status_code, 401)

    def test_auth_failures_limited_per_ip(self):
        codes = [self.client.get("/api/me", headers={"X-Forwarded-For": "1.2.3.4"}).status_code
                 for _ in range(35)]
        self.assertEqual(codes[:30], [401] * 30)
        self.assertEqual(codes[30], 429)
        # інша адреса не страждає
        self.assertEqual(self.client.get("/api/me", headers={"X-Forwarded-For": "5.6.7.8"}).status_code, 401)

    def test_keys_endpoint_limited_per_user(self):
        app.state.users = FakeUsers()
        headers = {"X-Telegram-Init-Data": signed_init_data(77)}
        codes = [self.client.post("/api/account/keys", json={}, headers=headers).status_code
                 for _ in range(KEYS_LIMIT[0] + 1)]
        # спроби в межах ліміту доходять до обробника (sessions немає — 503)
        self.assertNotIn(429, codes[:KEYS_LIMIT[0]])
        self.assertEqual(codes[-1], 429)
        self.assertIn("Retry-After", self.client.post("/api/account/keys", json={}, headers=headers).headers)

    def test_admin_route_logs_forbidden(self):
        app.state.users = FakeUsers(role="user")
        with self.assertLogs("webapp.backend.deps", level="WARNING") as logs:
            resp = self.client.get("/api/admin/users", headers={"X-Telegram-Init-Data": signed_init_data(78)})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(any("403 admin-only" in line and "/api/admin/users" in line for line in logs.output))


class TestKeyPermissions(unittest.TestCase):
    def setUp(self):
        from fastapi import HTTPException
        from webapp.backend.routes.account import check_key_permissions
        self.check, self.exc = check_key_permissions, HTTPException

    def test_trade_only_key_accepted(self):
        # реальна відповідь BingX для ключа «читання + спот + ф'ючерси»
        self.check({"permissions": [1, 2, 3], "enableFutures": True, "permitsUniversalTransfer": False})

    def test_withdraw_code_rejected(self):
        with self.assertRaises(self.exc) as ctx:
            self.check({"permissions": [1, 2, 3, 5]})
        self.assertIn("вывод", ctx.exception.detail.lower())

    def test_withdraw_flag_rejected(self):
        with self.assertRaises(self.exc):
            self.check({"permissions": [2, 3], "enableWithdrawals": True})

    def test_no_futures_rejected(self):
        with self.assertRaises(self.exc) as ctx:
            self.check({"permissions": [1, 2], "enableFutures": False})
        self.assertIn("фьючерс", ctx.exception.detail.lower())


if __name__ == "__main__":
    unittest.main()
