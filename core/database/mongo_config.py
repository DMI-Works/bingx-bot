"""
Параметри підключення до MongoDB. MONGO_URI перевіряється вже при імпорті:
усе, що імпортує core.database (а транзитивно core.risk / core.trading),
потребує цієї змінної оточення.

Режим testnet/live:
  - `exchange.testnet` у config.yaml — лише ДЕФОЛТ (запечений в образ на Railway).
  - Реальний режим рантайму може бути перевизначений із Mini App: він пишеться в
    колекцію `control` (документ _id="mode") БАЗОВОЇ бази (без суфікса `_testnet`),
    бо ця база не залежить від режиму й переживає рестарт контейнера на Railway.
    main.py читає override ДО вибору робочої бази (див. resolve_testnet).
Константи IS_TESTNET / MONGO_DB_NAME / TRADE_MODE лишаються ДЕФОЛТАМИ з config.yaml
(їх читають тести та scripts/*); робочий інстанс Database отримує режим явно.
"""

import logging
import os
from typing import Optional

from config import ConfigLoader

logger = logging.getLogger(__name__)

# Колекція і ключ для mode-незалежного override режиму в базовій БД.
MODE_CONTROL_COLLECTION = "control"
MODE_CONTROL_ID = "mode"


def _load_testnet_flag() -> bool:
    try:
        return bool(ConfigLoader().get('exchange.testnet', True))
    except Exception as e:
        logger.warning(
            f"Не вдалось прочитати exchange.testnet через ConfigLoader ({e}) — "
            f"вважаю testnet=True для вибору назви БД"
        )
        return True


def base_db_name() -> str:
    """Імя базової (mode-незалежної) бази — без суфікса `_testnet`."""
    return os.getenv("MONGO_DB_NAME") or "trading_bot"


def db_name_for(testnet: bool) -> str:
    """Робоча база для режиму: базова +`_testnet` для тестнету."""
    base = base_db_name()
    return f"{base}_testnet" if testnet else base


def mode_str(testnet: bool) -> str:
    """Рядок режиму для поля trade_analytics.mode."""
    return "testnet" if testnet else "live"


def resolve_testnet(config_default: bool, override: Optional[bool]) -> bool:
    """Пріоритет режиму: override з БД важливіший за дефолт із config.yaml.

    Чиста функція (без БД) — легко тестується.
    """
    return config_default if override is None else bool(override)


def read_mode_override(client) -> Optional[bool]:
    """Читає `control.mode.testnet` із БАЗОВОЇ бази. None, якщо документа нема.

    client — відкритий pymongo.MongoClient. Помилки не ковтаємо нишком, лише
    логуємо й повертаємо None (щоб бот піднявся на дефолті з config.yaml).
    """
    try:
        doc = client[base_db_name()][MODE_CONTROL_COLLECTION].find_one(
            {"_id": MODE_CONTROL_ID}
        )
    except Exception as e:
        logger.warning(f"Не вдалось прочитати override режиму з БД ({e}) — дефолт із config")
        return None
    if not doc or "testnet" not in doc:
        return None
    return bool(doc["testnet"])


def save_mode_override(client, testnet: bool) -> None:
    """Upsert `control.mode` у БАЗОВУ базу (джерело істини режиму рантайму)."""
    from datetime import datetime
    client[base_db_name()][MODE_CONTROL_COLLECTION].update_one(
        {"_id": MODE_CONTROL_ID},
        {"$set": {"testnet": bool(testnet), "updated_at": datetime.utcnow()}},
        upsert=True,
    )


IS_TESTNET = _load_testnet_flag()

# Режим, у якому зараз працює бот — пишеться в кожен запис trade_analytics.
# testnet і live і так лежать у РІЗНИХ базах (MONGO_DB_NAME vs
# MONGO_DB_NAME_testnet); поле — додатковий запобіжник на випадок, якщо дані
# з обох баз колись опиняться поряд (напр. спільний експорт у CSV).
TRADE_MODE = mode_str(IS_TESTNET)

MONGO_URI = os.getenv("MONGO_URI")

MONGO_DB_NAME = db_name_for(IS_TESTNET)

if not MONGO_URI:
    raise RuntimeError(
        "MONGO_URI не задано. Додай у .env рядок:\n"
        "MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?appName=<app>"
    )
