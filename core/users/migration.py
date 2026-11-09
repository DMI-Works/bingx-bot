"""
Перехід з однокористувацької схеми на багатокористувацьку (ідемпотентно,
викликається при кожному старті main.py):

1. Рядки positions / trade_analytics / settings без user_id (дані власника)
   отримують user_id адміна — в обох базах (live і testnet).
2. Спільні налаштування пулу монет (whitelist/blacklist) копіюються в
   глобальну область user_id="__global__" (їх читають SymbolSelector'и).
3. Старі ГЛОБАЛЬНО-унікальні індекси (order_id_1, key_1) видаляються: інакше
   другий користувач не зміг би записати той самий ключ налаштувань.
4. Адмін створюється в users; його ключі з .env (BINGX_API_KEY/_SECRET,
   *_TESTNET) шифруються в документ, режим — зі старого control.mode.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from ..database.mongo_config import base_db_name, db_name_for, read_mode_override
from .repository import UsersRepository

logger = logging.getLogger(__name__)

GLOBAL_SCOPE = "__global__"
GLOBAL_SETTING_KEYS = ("trading.whitelist_symbols", "trading.blacklist_symbols")
_LEGACY_UNIQUE_INDEXES = {
    "positions": "order_id_1",
    "trade_analytics": "order_id_1",
    "settings": "key_1",
}


def _drop_legacy_indexes(db) -> None:
    for coll, index in _LEGACY_UNIQUE_INDEXES.items():
        try:
            if index in db[coll].index_information():
                db[coll].drop_index(index)
                logger.info(f"[MIGRATION] {db.name}.{coll}: dropped legacy index {index}")
        except Exception as e:
            logger.warning(f"[MIGRATION] {db.name}.{coll}: cannot drop {index}: {e}")


def migrate_legacy(client, admin_id: str, default_testnet: bool) -> None:
    admin_id = str(admin_id)
    for testnet in (False, True):
        db = client[db_name_for(testnet)]
        _drop_legacy_indexes(db)

        for key in GLOBAL_SETTING_KEYS:
            legacy = db.settings.find_one({"key": key, "user_id": {"$exists": False}})
            if legacy and not db.settings.find_one({"key": key, "user_id": GLOBAL_SCOPE}):
                db.settings.insert_one({
                    "key": key, "value": legacy["value"], "user_id": GLOBAL_SCOPE,
                    "updated_at": datetime.utcnow(),
                })

        for coll in ("positions", "trade_analytics", "settings"):
            result = db[coll].update_many({"user_id": {"$exists": False}}, {"$set": {"user_id": admin_id}})
            if result.modified_count:
                logger.info(f"[MIGRATION] {db.name}.{coll}: {result.modified_count} rows -> user {admin_id}")

    users = UsersRepository(client)
    if users.get(admin_id) is None:
        override = read_mode_override(client)
        testnet = default_testnet if override is None else bool(override)
        now = datetime.utcnow()
        users.upsert_raw(admin_id, {
            "role": "admin", "status": "active", "testnet": testnet,
            "created_at": now, "trial_until": now + timedelta(days=36500),
        })
        logger.info(f"[MIGRATION] admin user {admin_id} created (testnet={testnet})")

    admin = users.get(admin_id)
    if admin.get("role") != "admin":
        users.upsert_raw(admin_id, {"role": "admin"})
    for testnet, key_env, secret_env in (
        (False, "BINGX_API_KEY", "BINGX_API_SECRET"),
        (True, "BINGX_API_KEY_TESTNET", "BINGX_API_SECRET_TESTNET"),
    ):
        api_key, api_secret = os.getenv(key_env), os.getenv(secret_env)
        if api_key and api_secret and not users.has_keys(admin, testnet):
            users.save_keys(admin_id, testnet, api_key, api_secret, can_withdraw=False)
            logger.info(f"[MIGRATION] admin {'testnet' if testnet else 'live'} keys imported from env")


__all__ = ["migrate_legacy", "GLOBAL_SCOPE", "base_db_name"]
