from datetime import datetime
from typing import Any, List, Optional

from pymongo import DESCENDING


class PositionsRepository:
    """Колекція positions — "оперативний" стан угод (останній стан позиції;
    повна історія — у trade_analytics). Частина Database: використовує
    self.db і self._lock."""

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
            # commission_usdt = commission_open + commission_close (funding окремо —
            # це не комісія біржі). Сумарне поле лишається заради всього, що вже
            # його читає (webapp /api/stats, risk_manager).
            "commission_usdt": None,
            "commission_open": None,
            "commission_close": None,
            "funding_fee": None,
            "net_pnl": None,
            # net_pnl ДО фіксу формули знаку — пише лише скрипт міграції
            # історичних даних; для нових позицій лишається None.
            "net_pnl_legacy": None,
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
        commission_open: Optional[float] = None,
        commission_close: Optional[float] = None,
        funding_fee: Optional[float] = None,
        net_pnl: Optional[float] = None
    ) -> None:
        """Оновлює статус позиції. Метрики, передані як None, не чіпаються
        ($set лише по переданих полях, щоб не затерти вже записані значення)."""
        optional_fields = {
            "closed_at": closed_at,
            "close_price": close_price,
            "realized_pnl": realized_pnl,
            "roe_percent": roe_percent,
            "margin_usdt": margin_usdt,
            "commission_usdt": commission_usdt,
            "commission_open": commission_open,
            "commission_close": commission_close,
            "funding_fee": funding_fee,
            "net_pnl": net_pnl,
        }
        update_fields = {"status": status, **{k: v for k, v in optional_fields.items() if v is not None}}

        with self._lock:
            self.db.positions.update_one({"order_id": order_id}, {"$set": update_fields})

    def update_position_metadata(self, order_id: str, metadata: str) -> None:
        with self._lock:
            self.db.positions.update_one({"order_id": order_id}, {"$set": {"metadata": metadata}})

    def get_active_positions(self) -> List[dict]:
        with self._lock:
            return list(self.db.positions.find({"status": "OPEN"}).sort("created_at", DESCENDING))

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
            return list(self.db.positions.find({"status": "CLOSED"}).sort("closed_at", DESCENDING))

    def get_closed_positions_count(self) -> int:
        with self._lock:
            return self.db.positions.count_documents({"status": "CLOSED"})

    def get_last_position_time_by_symbol(self) -> dict:
        """Для кожного символу — час останньої угоди (відкриття), OPEN або CLOSED."""
        pipeline = [{"$group": {"_id": "$symbol", "last_at": {"$max": "$created_at"}}}]
        with self._lock:
            rows = list(self.db.positions.aggregate(pipeline))
        return {row["_id"]: row["last_at"] for row in rows}
