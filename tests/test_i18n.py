"""Мультимовність: каталог бекенду, мова відповідей API, вибір мови, тексти Telegram."""

import os
import re
import string
import unittest

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")

from fastapi.testclient import TestClient  # noqa: E402

from core.common.i18n import CATALOG, LANGS, normalize_lang, t  # noqa: E402
from core.telegram import messages  # noqa: E402
from webapp.backend.api import app  # noqa: E402
from webapp.backend.rate_limit import limiter  # noqa: E402

from test_security import signed_init_data  # noqa: E402


def placeholders(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


class TestCatalog(unittest.TestCase):
    def test_every_key_has_all_languages_with_same_placeholders(self):
        for key, entry in CATALOG.items():
            self.assertEqual(set(entry), set(LANGS), key)
            expected = placeholders(entry["en"])
            for lang in LANGS:
                self.assertTrue(entry[lang].strip(), (key, lang))
                self.assertEqual(placeholders(entry[lang]), expected, (key, lang))

    def test_normalize(self):
        self.assertEqual(normalize_lang(None), "en")
        self.assertEqual(normalize_lang("ru-RU"), "ru")
        self.assertEqual(normalize_lang("UK"), "uk")
        self.assertEqual(normalize_lang("ua"), "uk")
        self.assertEqual(normalize_lang("de"), "en")

    def test_unknown_key_and_fallback(self):
        self.assertEqual(t("ru", "no_such_key"), "no_such_key")
        self.assertEqual(t("de", "btn_cancel"), CATALOG["btn_cancel"]["en"])


class TestTelegramTexts(unittest.TestCase):
    def test_default_is_english(self):
        self.assertIn("How to start", messages.start_text())
        self.assertIn("Stop loss hit", messages.stop_loss_triggered({"symbol": "BTC-USDT", "price": 1}))

    def test_each_language(self):
        data = {"symbol": "ETH-USDT", "consecutive_losses": 3, "max_consecutive_losses": 3, "pause_hours": 6}
        self.assertIn("на паузе", messages.symbol_paused(data, "ru"))
        self.assertIn("на паузі", messages.symbol_paused(data, "uk"))
        self.assertIn("paused", messages.symbol_paused(data, "en"))
        moved = {"symbol": "BTC-USDT", "side": "LONG", "old_stop_price": 90.0, "new_stop_price": 101.0,
                 "entry_price": 100.0, "leverage": 10, "stage": "breakeven"}
        self.assertIn("безубыток", messages.stop_loss_moved(moved, "ru"))
        self.assertIn("Old stop", messages.stop_loss_moved(moved, "en"))
        self.assertEqual(messages.take_profit_summary([{}, {}], "uk"), "2 рівні")

    def test_no_cyrillic_in_english_texts(self):
        for key, entry in CATALOG.items():
            self.assertIsNone(re.search("[А-Яа-яЁёІіЇїЄє]", entry["en"]), key)


class FakeUsers:
    def __init__(self):
        self.docs = {}

    def ensure_user(self, tg_user):
        uid = str(tg_user["id"])
        self.docs.setdefault(uid, {"_id": uid, "role": "user", "status": "active", "testnet": True})
        return self.docs[uid]

    def get(self, user_id):
        return self.docs.get(str(user_id))

    def set_lang(self, user_id, lang):
        self.docs[str(user_id)]["lang"] = lang


class FakeSessions:
    def __init__(self):
        self.refreshed = []

    def get(self, user_id):
        return None

    def refresh_user(self, user_id):
        self.refreshed.append(user_id)


class TestApiLanguage(unittest.TestCase):
    def setUp(self):
        limiter.reset()
        self._state = {k: getattr(app.state, k, None) for k in ("users", "sessions")}
        app.state.users = FakeUsers()
        app.state.sessions = FakeSessions()
        self.client = TestClient(app)
        self.headers = {"X-Telegram-Init-Data": signed_init_data(501)}

    def tearDown(self):
        for k, v in self._state.items():
            setattr(app.state, k, v)
        limiter.reset()

    def test_lang_unset_then_saved(self):
        me = self.client.get("/api/me", headers=self.headers).json()
        self.assertIsNone(me["lang"])
        resp = self.client.post("/api/me/lang", json={"lang": "uk"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["lang"], "uk")
        self.assertEqual(app.state.sessions.refreshed, ["501"])

    def test_unknown_lang_rejected(self):
        resp = self.client.post("/api/me/lang", json={"lang": "de"}, headers=self.headers)
        self.assertEqual(resp.status_code, 422)

    def test_error_detail_follows_x_lang(self):
        # сесії немає → 409 no_session з текстом мовою запиту
        for lang, word in (("en", "Connect"), ("ru", "Подключите"), ("uk", "Підключіть")):
            resp = self.client.get("/api/settings", headers={**self.headers, "X-Lang": lang})
            self.assertEqual(resp.status_code, 409)
            self.assertIn(word, resp.json()["detail"]["message"])

    def test_can_open_reason_is_code(self):
        app.state.users.ensure_user({"id": 501})["status"] = "blocked"
        me = self.client.get("/api/me", headers=self.headers).json()
        self.assertFalse(me["can_open"])
        self.assertEqual(me["can_open_reason"], "blocked")


if __name__ == "__main__":
    unittest.main()
