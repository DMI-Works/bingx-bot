"""
Параметри підключення до MongoDB. MONGO_URI перевіряється вже при імпорті:
усе, що імпортує core.database (а транзитивно core.risk / core.trading),
потребує цієї змінної оточення.
"""

import logging
import os

from config import ConfigLoader

logger = logging.getLogger(__name__)


def _load_testnet_flag() -> bool:
    try:
        return bool(ConfigLoader().get('exchange.testnet', True))
    except Exception as e:
        logger.warning(
            f"Не вдалось прочитати exchange.testnet через ConfigLoader ({e}) — "
            f"вважаю testnet=True для вибору назви БД"
        )
        return True


IS_TESTNET = _load_testnet_flag()

# Режим, у якому зараз працює бот — пишеться в кожен запис trade_analytics.
# testnet і live і так лежать у РІЗНИХ базах (MONGO_DB_NAME vs
# MONGO_DB_NAME_testnet); поле — додатковий запобіжник на випадок, якщо дані
# з обох баз колись опиняться поряд (напр. спільний експорт у CSV).
TRADE_MODE = "testnet" if IS_TESTNET else "live"

MONGO_URI = os.getenv("MONGO_URI")

_base_db_name = os.getenv("MONGO_DB_NAME") or "trading_bot"

MONGO_DB_NAME = f"{_base_db_name}_testnet" if IS_TESTNET else _base_db_name

if not MONGO_URI:
    raise RuntimeError(
        "MONGO_URI не задано. Додай у .env рядок:\n"
        "MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?appName=<app>"
    )
