import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

import certifi
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.database import Database as MongoDatabase


logger = logging.getLogger(__name__)


MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME") or "trading_bot"

if not MONGO_URI:
    raise RuntimeError(
        "MONGO_URI не задано. Додай у .env рядок:\n"
        "MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?appName=<app>"
    )


class Database:
    def __init__(self):
        self.uri = MONGO_URI
        self.db_name = MONGO_DB_NAME
        self.client: Optional[MongoClient] = None
        self.db: Optional[MongoDatabase] = None

        self._lock = threading.Lock()
        self._init_database()

    def _init_database(self) -> None:
        self.client = MongoClient(self.uri, tlsCAFile=certifi.where())
        self.db = self.client[self.db_name]
        self._create_indexes()
        logger.info(f"Database initialized: db={self.db_name}")

    def _create_indexes(self) -> None:
        self.db.balance.create_index([("asset", ASCENDING), ("timestamp", DESCENDING)])

        # order_id унікальний, але sparse — щоб не заважати документам,
        # де його немає (на випадок ручних записів без order_id).
        self.db.positions.create_index("order_id", unique=True, sparse=True)
        self.db.positions.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
        self.db.positions.create_index([("status", ASCENDING), ("closed_at", DESCENDING)])
        self.db.positions.create_index([("symbol", ASCENDING), ("side", ASCENDING), ("status", ASCENDING)])

        self.db.settings.create_index("key", unique=True)

        logger.info("Database indexes created/verified")

    # ------------------------------------------------------------------
    # balance
    # ------------------------------------------------------------------

    def insert_balance(self, asset: str, free: float, locked: float) -> None:
        with self._lock:
            self.db.balance.insert_one({
                "asset": asset,
                "free": free,
                "locked": locked,
                "total": free + locked,
                "timestamp": datetime.utcnow(),
            })

    def get_latest_balance(self, asset: str) -> Optional[dict]:
        with self._lock:
            return self.db.balance.find_one(
                {"asset": asset},
                sort=[("timestamp", DESCENDING)],
            )

    # ------------------------------------------------------------------
    # positions
    # ------------------------------------------------------------------

    def insert_position(
        self,
        order_id: str,
        symbol: str,
        side: str,
        status: str,
        metadata: str = None
    ) -> Any:
        """Зберігає мінімальні дані про відкриту позицію. Повертає _id вставленого документа."""
        doc = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "status": status,
            "created_at": datetime.utcnow(),
            "closed_at": None,
            "close_price": None,
            "realized_pnl": None,
            "roe_percent": None,
            "margin_usdt": None,
            "commission_usdt": None,
            "net_pnl": None,
            "metadata": metadata,
        }
        with self._lock:
            result = self.db.positions.insert_one(doc)
        return result.inserted_id

    def update_position_status(
        self,
        order_id: str,
        status: str,
        closed_at: datetime = None,
        close_price: Optional[float] = None,
        realized_pnl: Optional[float] = None,
        roe_percent: Optional[float] = None,
        margin_usdt: Optional[float] = None,
        commission_usdt: Optional[float] = None,
        net_pnl: Optional[float] = None
    ) -> None:
        """
        Оновлює статус позиції. Усі метрики — опціональні: якщо не передані,
        відповідні поля не чіпаються (тільки $set по переданих полях, щоб
        проміжний виклик не затер вже записані значення None-ом).
        """
        update_fields: Dict[str, Any] = {"status": status}

        if closed_at is not None:
            update_fields["closed_at"] = closed_at
        if close_price is not None:
            update_fields["close_price"] = close_price
        if realized_pnl is not None:
            update_fields["realized_pnl"] = realized_pnl
        if roe_percent is not None:
            update_fields["roe_percent"] = roe_percent
        if margin_usdt is not None:
            update_fields["margin_usdt"] = margin_usdt
        if commission_usdt is not None:
            update_fields["commission_usdt"] = commission_usdt
        if net_pnl is not None:
            update_fields["net_pnl"] = net_pnl

        with self._lock:
            self.db.positions.update_one(
                {"order_id": order_id},
                {"$set": update_fields},
            )

    def get_active_positions(self) -> List[dict]:
        """Повертає всі активні позиції"""
        with self._lock:
            return list(
                self.db.positions.find({"status": "OPEN"}).sort("created_at", DESCENDING)
            )

    def update_position_metadata(self, order_id: str, metadata: str) -> None:
        with self._lock:
            self.db.positions.update_one(
                {"order_id": order_id},
                {"$set": {"metadata": metadata}},
            )

    def get_open_position_by_symbol_side(self, symbol: str, side: str) -> Optional[dict]:
        with self._lock:
            return self.db.positions.find_one({
                "symbol": symbol,
                "side": side,
                "status": "OPEN",
            })

    def get_closed_positions(self, limit: int = 5, offset: int = 0) -> List[dict]:
        with self._lock:
            return list(
                self.db.positions.find({"status": "CLOSED"})
                .sort("closed_at", DESCENDING)
                .skip(offset)
                .limit(limit)
            )

    def get_all_closed_positions(self) -> List[dict]:
        with self._lock:
            return list(
                self.db.positions.find({"status": "CLOSED"}).sort("closed_at", DESCENDING)
            )

    def get_closed_positions_count(self) -> int:
        with self._lock:
            return self.db.positions.count_documents({"status": "CLOSED"})

    def get_stats_summary(self) -> dict:
        """
        Агрегована статистика по закритих позиціях в доларах: скільки всього
        вкладено (маржа), скільки заробили/втратили чисто (net_pnl),
        скільки пішло на комісію.
        """
        pipeline = [
            {"$match": {"status": "CLOSED"}},
            {"$group": {
                "_id": None,
                "total_trades": {"$sum": 1},
                "total_margin_usdt": {"$sum": "$margin_usdt"},
                "total_realized_pnl": {"$sum": "$realized_pnl"},
                "total_commission_usdt": {"$sum": "$commission_usdt"},
                "total_net_pnl": {"$sum": "$net_pnl"},
                "winning_trades": {
                    "$sum": {"$cond": [{"$gt": ["$net_pnl", 0]}, 1, 0]}
                },
                "losing_trades": {
                    "$sum": {"$cond": [{"$lt": ["$net_pnl", 0]}, 1, 0]}
                },
            }},
        ]

        with self._lock:
            result = list(self.db.positions.aggregate(pipeline))

        if not result:
            return {}

        row = result[0]
        row.pop("_id", None)
        return row

    def get_last_position_time_by_symbol(self) -> dict:
        """Для кожного символу — час останньої угоди (відкриття), OPEN або CLOSED."""
        pipeline = [
            {"$group": {"_id": "$symbol", "last_at": {"$max": "$created_at"}}},
        ]
        with self._lock:
            rows = list(self.db.positions.aggregate(pipeline))
        return {row["_id"]: row["last_at"] for row in rows}

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