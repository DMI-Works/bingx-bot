import logging
import threading
from datetime import datetime
from typing import Optional

import certifi
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.database import Database as MongoDatabase

from .mongo_config import IS_TESTNET, MONGO_URI, db_name_for, mode_str
from .positions_repository import PositionsRepository
from .trade_analytics_repository import TradeAnalyticsRepository


logger = logging.getLogger(__name__)


class Database(PositionsRepository, TradeAnalyticsRepository):
    """Єдина точка доступу до MongoDB. Методи колекцій positions і
    trade_analytics — у відповідних репозиторіях; тут — підключення,
    індекси і key-value колекція settings.

    Режим (testnet/live) передається явно — від нього залежать і назва робочої
    бази, і поле trade_analytics.mode. Якщо не заданий — береться дефолт із
    config.yaml (IS_TESTNET), як було раніше."""

    def __init__(self, testnet: Optional[bool] = None):
        self.testnet = IS_TESTNET if testnet is None else bool(testnet)
        self.uri = MONGO_URI
        self.db_name = db_name_for(self.testnet)
        self.trade_mode = mode_str(self.testnet)
        self.client: Optional[MongoClient] = None
        self.db: Optional[MongoDatabase] = None

        self._lock = threading.Lock()
        self._init_database()

    def _init_database(self) -> None:
        self.client = MongoClient(self.uri, tlsCAFile=certifi.where())
        self.db = self.client[self.db_name]
        self._create_indexes()
        logger.info(f"Database initialized: db={self.db_name} (testnet={self.testnet})")

    def _create_indexes(self) -> None:
        self.db.positions.create_index("order_id", unique=True, sparse=True)
        self.db.positions.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
        self.db.positions.create_index([("status", ASCENDING), ("closed_at", DESCENDING)])
        self.db.positions.create_index([("symbol", ASCENDING), ("side", ASCENDING), ("status", ASCENDING)])

        self.db.settings.create_index("key", unique=True)

        self.db.trade_analytics.create_index("order_id", unique=True, sparse=True)
        self.db.trade_analytics.create_index([("status", ASCENDING), ("opened_at", DESCENDING)])
        self.db.trade_analytics.create_index([("status", ASCENDING), ("closed_at", DESCENDING)])
        self.db.trade_analytics.create_index(
            [("symbol", ASCENDING), ("strategy", ASCENDING), ("mode", ASCENDING), ("closed_at", DESCENDING)]
        )
        self.db.trade_analytics.create_index(
            [("strategy", ASCENDING), ("mode", ASCENDING), ("closed_at", DESCENDING)]
        )

        logger.info("Database indexes created/verified")

    # ------------------------------------------------------------------
    # settings (generic key-value)
    # ------------------------------------------------------------------

    def get_setting(self, key: str) -> Optional[str]:
        with self._lock:
            row = self.db.settings.find_one({"key": key})
        return row["value"] if row else None

    def save_setting(self, key: str, value: str) -> None:
        with self._lock:
            self.db.settings.update_one(
                {"key": key},
                {"$set": {"value": value, "updated_at": datetime.utcnow()}},
                upsert=True,
            )

    def close(self) -> None:
        if self.client:
            self.client.close()
            logger.info("Database connection closed")
