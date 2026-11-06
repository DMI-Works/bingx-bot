"""
Користувачі бота (колекція `users` у БАЗОВІЙ, mode-незалежній базі).

Документ:
  _id          — Telegram user id (рядок), він же user_id у всіх даних
  username, first_name
  role         — 'admin' | 'user'
  status       — 'active' | 'blocked'
  testnet      — режим рахунку користувача (у кожного свій)
  keys.live / keys.testnet — {api_key_enc, api_secret_enc, key_suffix,
                 can_withdraw, verified_at} (секрети зашифровані, див. crypto.py)
  created_at, trial_until
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from pymongo import MongoClient

from ..database.mongo_config import base_db_name
from .crypto import decrypt_secret, encrypt_secret, mask_key

TRIAL_DAYS = 7


def admin_telegram_id() -> Optional[str]:
    """Адмін — ADMIN_TELEGRAM_ID, а якщо не задано — TELEGRAM_CHAT_ID (власник,
    якому бот і раніше слав усі повідомлення)."""
    value = os.getenv("ADMIN_TELEGRAM_ID") or os.getenv("TELEGRAM_CHAT_ID")
    return str(value).strip() if value else None


def _now() -> datetime:
    return datetime.utcnow()


def _mode_key(testnet: bool) -> str:
    return "testnet" if testnet else "live"


class UsersRepository:
    def __init__(self, client: MongoClient):
        self.collection = client[base_db_name()].users
        self._lock = threading.Lock()

    # ---------- читання ----------

    def get(self, user_id: str) -> Optional[dict]:
        with self._lock:
            return self.collection.find_one({"_id": str(user_id)})

    def list_all(self) -> List[dict]:
        with self._lock:
            return list(self.collection.find().sort("created_at", 1))

    def list_active_with_keys(self) -> List[dict]:
        """Активні користувачі, у яких є ключі для поточного режиму їхнього рахунку."""
        result = []
        for user in self.list_all():
            if user.get("status") != "active":
                continue
            if self.has_keys(user, bool(user.get("testnet", False))):
                result.append(user)
        return result

    @staticmethod
    def has_keys(user: dict, testnet: bool) -> bool:
        keys = (user.get("keys") or {}).get(_mode_key(testnet)) or {}
        return bool(keys.get("api_key_enc") and keys.get("api_secret_enc"))

    @staticmethod
    def decrypted_keys(user: dict, testnet: bool) -> Optional[Dict[str, str]]:
        keys = (user.get("keys") or {}).get(_mode_key(testnet)) or {}
        if not keys.get("api_key_enc"):
            return None
        return {
            "api_key": decrypt_secret(keys["api_key_enc"]),
            "api_secret": decrypt_secret(keys["api_secret_enc"]),
        }

    # ---------- запис ----------

    def ensure_user(self, tg_user: dict) -> dict:
        """Створює користувача при першому вході в Mini App (з пробним
        періодом) або оновлює ім'я. Адмін (ADMIN_TELEGRAM_ID) отримує роль admin."""
        user_id = str(tg_user["id"])
        now = _now()
        role = "admin" if user_id == admin_telegram_id() else "user"
        with self._lock:
            self.collection.update_one(
                {"_id": user_id},
                {
                    "$set": {
                        "username": tg_user.get("username"),
                        "first_name": tg_user.get("first_name"),
                        "last_seen_at": now,
                    },
                    "$setOnInsert": {
                        "role": role,
                        "status": "active",
                        "testnet": True,
                        "created_at": now,
                        "trial_until": now + timedelta(days=TRIAL_DAYS),
                    },
                },
                upsert=True,
            )
            return self.collection.find_one({"_id": user_id})

    def save_keys(self, user_id: str, testnet: bool, api_key: str, api_secret: str, can_withdraw: bool) -> None:
        mode = _mode_key(testnet)
        with self._lock:
            self.collection.update_one(
                {"_id": str(user_id)},
                {"$set": {
                    f"keys.{mode}": {
                        "api_key_enc": encrypt_secret(api_key),
                        "api_secret_enc": encrypt_secret(api_secret),
                        "key_suffix": mask_key(api_key),
                        "can_withdraw": bool(can_withdraw),
                        "verified_at": _now(),
                    }
                }},
            )

    def delete_keys(self, user_id: str, testnet: bool) -> None:
        with self._lock:
            self.collection.update_one({"_id": str(user_id)}, {"$unset": {f"keys.{_mode_key(testnet)}": ""}})

    def set_testnet(self, user_id: str, testnet: bool) -> None:
        with self._lock:
            self.collection.update_one({"_id": str(user_id)}, {"$set": {"testnet": bool(testnet)}})

    def set_status(self, user_id: str, status: str) -> None:
        if status not in ("active", "blocked"):
            raise ValueError(f"unknown status {status!r}")
        with self._lock:
            self.collection.update_one({"_id": str(user_id)}, {"$set": {"status": status}})

    def upsert_raw(self, user_id: str, fields: dict) -> None:
        """Для міграції/скриптів."""
        with self._lock:
            self.collection.update_one({"_id": str(user_id)}, {"$set": fields}, upsert=True)


def public_view(user: dict) -> dict:
    """Що можна віддати в Mini App (без зашифрованих секретів)."""
    keys = user.get("keys") or {}
    return {
        "id": user["_id"],
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "role": user.get("role", "user"),
        "status": user.get("status", "active"),
        "testnet": bool(user.get("testnet", True)),
        "trial_until": user.get("trial_until"),
        "created_at": user.get("created_at"),
        "keys": {
            mode: {
                "connected": bool((keys.get(mode) or {}).get("api_key_enc")),
                "key_suffix": (keys.get(mode) or {}).get("key_suffix"),
                "can_withdraw": bool((keys.get(mode) or {}).get("can_withdraw")),
                "verified_at": (keys.get(mode) or {}).get("verified_at"),
            }
            for mode in ("live", "testnet")
        },
    }
