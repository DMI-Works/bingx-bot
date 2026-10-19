from datetime import datetime
from typing import Any, Dict, List, Optional

from pymongo import DESCENDING

from .analytics_fields import entry_derived_fields, exit_derived_fields
from .mongo_config import TRADE_MODE


class TradeAnalyticsRepository:
    """
    Колекція trade_analytics — повна історія угоди: як переставлявся SL,
    якими частками спрацював TP, з якими налаштуваннями стратегії угода
    відкрита, у якому режимі (testnet/live). positions — "оперативна"
    таблиця (останній стан), trade_analytics — архів для розбору "чому саме
    ця угода пішла в мінус". Частина Database: використовує self.db і self._lock.
    """

    def insert_trade_analytics(
        self,
        order_id: str,
        symbol: str,
        side: str,
        opened_by: str,
        entry_price: float,
        quantity: float,
        leverage: Any = None,
        margin_usdt: Optional[float] = None,
        strategy: Optional[str] = None,
        strategy_params: Optional[dict] = None,
        stop_loss_price_initial: Optional[float] = None,
        take_profit_levels_initial: Optional[list] = None,
        trail_meta: Optional[dict] = None,
        risk_percent: Optional[float] = None,
        reference_price: Optional[float] = None,
        equity_at_entry: Optional[float] = None,
        concurrent_positions: Optional[int] = None,
        signal_context: Optional[dict] = None,
        market_context: Optional[dict] = None,
    ) -> Any:
        """
        Пишеться ОДИН раз при відкритті угоди (ботом або ручне відкриття,
        підхоплене через ACCOUNT_UPDATE). strategy_params — знімок активних
        параметрів стратегії на момент входу; на відміну від
        positions.metadata, він НІКОЛИ не перезаписується — щоб чесно
        порівнювати угоди до і після зміни налаштувань у мініаппі.

        equity_at_entry / concurrent_positions / signal_context — контекст
        входу (equity, скільки інших позицій було відкрито, знімок індикаторів
        стратегії на сигнальній свічці). market_context — 24h-об'єм, зміна,
        діапазон і спред монети на вході. Похідні метрики — entry_derived_fields.
        """
        doc = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "mode": TRADE_MODE,
            "status": "OPEN",
            "opened_by": opened_by,
            "strategy": strategy,
            "strategy_params": strategy_params,
            "risk_percent": risk_percent,
            "opened_at": datetime.utcnow(),
            "closed_at": None,
            "duration_seconds": None,
            "entry_price": entry_price,
            "reference_price": reference_price,
            "close_price": None,
            "quantity": quantity,
            "leverage": leverage,
            "margin_usdt": margin_usdt,
            "stop_loss_price_initial": stop_loss_price_initial,
            "take_profit_levels_initial": take_profit_levels_initial,
            "trail_meta": trail_meta,
            "realized_pnl": None,
            "commission_open": None,
            "commission_close": None,
            "commission_usdt": None,
            "funding_fee": None,
            "net_pnl": None,
            "roe_percent": None,
            "close_reason": None,
            "closed_by": None,
            "sl_moves": [],
            "tp_fills": [],
            "concurrent_positions": concurrent_positions,
            "signal_context": signal_context,
            "market_context": market_context,
            # MFE/MAE по ходу угоди пише ExcursionTracker (update_trade_excursion)
            "excursion": None,
        }
        doc.update(entry_derived_fields(
            side=side, entry_price=entry_price, quantity=quantity, leverage=leverage,
            margin_usdt=margin_usdt, stop_loss_price_initial=stop_loss_price_initial,
            reference_price=reference_price, equity_at_entry=equity_at_entry,
            opened_at=doc["opened_at"],
        ))
        with self._lock:
            result = self.db.trade_analytics.insert_one(doc)
        return result.inserted_id

    def append_sl_move(
        self,
        order_id: str,
        trigger: str,
        old_stop_price: Optional[float],
        new_stop_price: float,
        old_order_id: Optional[str] = None,
        new_order_id: Optional[str] = None,
        leverage: Any = None,
        roi_percent: Optional[float] = None,
        reason: str = "trail",
    ) -> None:
        """
        Рядок на КОЖНЕ успішне перенесення SL. trigger — що саме спрацювало
        (напр. "level_2pct_roi", "atr_3step_step_2", "fallback:last_positive");
        reason відрізняє штатний trail від аварійного fallback. Якщо запису
        trade_analytics немає (угода старша за колекцію) — update нічого не робить.
        """
        self._push(order_id, "sl_moves", {
            "at": datetime.utcnow(),
            "trigger": trigger,
            "reason": reason,
            "old_stop_price": old_stop_price,
            "new_stop_price": new_stop_price,
            "old_order_id": old_order_id,
            "new_order_id": new_order_id,
            "leverage": leverage,
            "roi_percent": roi_percent,
        })

    def append_tp_fill(
        self,
        order_id: str,
        tp_order_id: Optional[str],
        client_order_id: Optional[str],
        price: float,
        quantity: float,
        realized_pnl: float,
        commission: float,
        trade_id: Any = None,
    ) -> None:
        """Рядок на кожне (часткове чи фінальне) спрацювання TAKE_PROFIT_MARKET."""
        self._push(order_id, "tp_fills", {
            "at": datetime.utcnow(),
            "order_id": tp_order_id,
            "client_order_id": client_order_id,
            "price": price,
            "quantity": quantity,
            "realized_pnl": realized_pnl,
            "commission": commission,
            "trade_id": trade_id,
        })

    def _push(self, order_id: str, field: str, item: dict) -> None:
        with self._lock:
            self.db.trade_analytics.update_one({"order_id": order_id}, {"$push": {field: item}})

    def close_trade_analytics(
        self,
        order_id: str,
        close_price: Optional[float],
        realized_pnl: Optional[float],
        commission_open: Optional[float],
        commission_close: Optional[float],
        funding_fee: Optional[float],
        net_pnl: Optional[float],
        roe_percent: Optional[float],
        margin_usdt: Optional[float],
        closed_by: Optional[str],
        close_reason: Optional[str],
        quantity: Optional[float] = None,
    ) -> None:
        """Пишеться ОДИН раз, коли угода закрита ПОВНІСТЮ."""
        now = datetime.utcnow()
        commission_usdt = None
        if commission_open is not None or commission_close is not None:
            commission_usdt = (commission_open or 0.0) + (commission_close or 0.0)

        update_fields: Dict[str, Any] = {
            "status": "CLOSED",
            "closed_at": now,
            "close_price": close_price,
            "realized_pnl": realized_pnl,
            "commission_open": commission_open,
            "commission_close": commission_close,
            "commission_usdt": commission_usdt,
            "funding_fee": funding_fee,
            "net_pnl": net_pnl,
            "roe_percent": roe_percent,
            "closed_by": closed_by,
            "close_reason": close_reason,
        }
        if margin_usdt is not None:
            update_fields["margin_usdt"] = margin_usdt
        if quantity is not None:
            update_fields["quantity"] = quantity

        with self._lock:
            row = self.db.trade_analytics.find_one({"order_id": order_id})
            if row and row.get("opened_at"):
                update_fields["duration_seconds"] = (now - row["opened_at"]).total_seconds()
            update_fields.update(exit_derived_fields(
                row, close_price, net_pnl, commission_open, commission_close, close_reason,
            ))
            self.db.trade_analytics.update_one({"order_id": order_id}, {"$set": update_fields})

    def update_trade_excursion(self, order_id: str, excursion: dict) -> None:
        """Перезаписує піддокумент excursion (MFE/MAE) — його повністю
        перераховує ExcursionTracker, тож $set цілого піддокумента безпечний."""
        with self._lock:
            self.db.trade_analytics.update_one({"order_id": order_id}, {"$set": {"excursion": excursion}})

    def get_trade_analytics(self, order_id: str) -> Optional[dict]:
        with self._lock:
            return self.db.trade_analytics.find_one({"order_id": order_id})

    def get_trade_analytics_list(
        self,
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
        mode: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[dict]:
        filters = {"symbol": symbol, "strategy": strategy, "mode": mode, "status": status}
        query = {k: v for k, v in filters.items() if v}

        with self._lock:
            return list(
                self.db.trade_analytics.find(query)
                .sort([("opened_at", DESCENDING)])
                .skip(offset)
                .limit(limit)
            )

    def get_trade_analytics_summary(
        self, mode: Optional[str] = None, strategy: Optional[str] = None
    ) -> List[dict]:
        """Агрегація по (strategy, symbol) серед ЗАКРИТИХ угод: win-rate,
        середній ROE%, скільки разів у середньому переносився SL — найшвидший
        спосіб побачити, яка стратегія/монета відпрацьовує, а яка зливає."""
        match: Dict[str, Any] = {"status": "CLOSED"}
        if mode:
            match["mode"] = mode
        if strategy:
            match["strategy"] = strategy

        pipeline = [
            {"$match": match},
            {"$group": {
                "_id": {"strategy": "$strategy", "symbol": "$symbol"},
                "trades": {"$sum": 1},
                "wins": {"$sum": {"$cond": [{"$gt": ["$net_pnl", 0]}, 1, 0]}},
                "losses": {"$sum": {"$cond": [{"$lt": ["$net_pnl", 0]}, 1, 0]}},
                "total_net_pnl": {"$sum": "$net_pnl"},
                "avg_net_pnl": {"$avg": "$net_pnl"},
                "avg_roe_percent": {"$avg": "$roe_percent"},
                "avg_sl_moves": {"$avg": {"$size": {"$ifNull": ["$sl_moves", []]}}},
                "avg_duration_seconds": {"$avg": "$duration_seconds"},
            }},
            {"$sort": {"total_net_pnl": 1}},
        ]

        with self._lock:
            rows = list(self.db.trade_analytics.aggregate(pipeline))

        result = []
        for row in rows:
            key = row.pop("_id")
            row["strategy"] = key.get("strategy")
            row["symbol"] = key.get("symbol")
            result.append(row)
        return result
