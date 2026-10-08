import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

import certifi
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.database import Database as MongoDatabase
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

# Режим, у якому зараз працює бот — пишеться в кожен запис trade_analytics,
# щоб тестові та бойові угоди завжди можна було відфільтрувати окремо навіть
# якщо колись дані з обох БД (_testnet / основної) опиняться поряд (напр.
# експорт в один CSV для порівняння). У звичайному випадку testnet і prod і
# так лежать у РІЗНИХ базах (MONGO_DB_NAME vs MONGO_DB_NAME_testnet) — це
# поле лишається додатковим запобіжником, а не єдиним механізмом розділення.
TRADE_MODE = "testnet" if IS_TESTNET else "live"

MONGO_URI = os.getenv("MONGO_URI")

_base_db_name = os.getenv("MONGO_DB_NAME") or "trading_bot"

MONGO_DB_NAME = f"{_base_db_name}_testnet" if IS_TESTNET else _base_db_name

if not MONGO_URI:
    raise RuntimeError(
        "MONGO_URI не задано. Додай у .env рядок:\n"
        "MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?appName=<app>"
    )


def _entry_derived_fields(
    side: Optional[str],
    entry_price: Optional[float],
    quantity: Optional[float],
    leverage: Any,
    margin_usdt: Optional[float],
    stop_loss_price_initial: Optional[float],
    reference_price: Optional[float],
    equity_at_entry: Optional[float],
    opened_at: datetime,
) -> Dict[str, Any]:
    """Похідні метрики входу — рахуються один раз при відкритті (чиста функція,
    без БД). Усі "pct" — у відсотках; slippage > 0 означає ПОГІРШЕННЯ."""
    out: Dict[str, Any] = {
        "opened_hour_utc": opened_at.hour,
        "opened_weekday": opened_at.weekday(),  # 0 = понеділок
    }
    try:
        leverage_value = float(leverage)
    except (TypeError, ValueError):
        leverage_value = None

    if entry_price and quantity:
        out["notional_usdt"] = entry_price * quantity

        if stop_loss_price_initial:
            distance = abs(entry_price - stop_loss_price_initial)
            out["stop_distance_pct"] = distance / entry_price * 100.0
            out["planned_risk_usdt"] = quantity * distance
            if leverage_value:
                out["stop_distance_roi_pct"] = out["stop_distance_pct"] * leverage_value

    if entry_price and reference_price and side in ("LONG", "SHORT"):
        sign = 1.0 if side == "LONG" else -1.0
        out["entry_slippage_pct"] = sign * (entry_price - reference_price) / reference_price * 100.0

    if equity_at_entry and equity_at_entry > 0:
        out["equity_at_entry"] = equity_at_entry
        if margin_usdt:
            out["margin_pct_of_equity"] = margin_usdt / equity_at_entry * 100.0
        if "planned_risk_usdt" in out:
            out["planned_risk_pct_of_equity"] = out["planned_risk_usdt"] / equity_at_entry * 100.0
    return out


def _exit_derived_fields(
    row: Optional[dict],
    close_price: Optional[float],
    net_pnl: Optional[float],
    commission_open: Optional[float],
    commission_close: Optional[float],
    close_reason: Optional[str],
) -> Dict[str, Any]:
    """Похідні метрики виходу з уже збереженого рядка trade_analytics (чиста функція)."""
    out: Dict[str, Any] = {}
    if not row:
        return out

    entry_price = row.get("entry_price")
    side = row.get("side")
    sign = 1.0 if side == "LONG" else -1.0
    moves = row.get("sl_moves") or []

    out["sl_moves_count"] = len(moves)
    final_stop = moves[-1].get("new_stop_price") if moves else row.get("stop_loss_price_initial")
    out["final_stop_price"] = final_stop

    planned_risk = row.get("planned_risk_usdt")
    if planned_risk and planned_risk > 0 and net_pnl is not None:
        out["r_multiple"] = net_pnl / planned_risk  # -1.0 = збиток рівно в запланований ризик

    if entry_price and close_price and side in ("LONG", "SHORT"):
        out["price_move_pct"] = sign * (close_price - entry_price) / entry_price * 100.0
        if close_reason == "stop_loss" and final_stop:
            # > 0: закрито ГІРШЕ за рівень стопу (проскальзування), у % від ціни входу
            out["stop_slippage_pct"] = sign * (final_stop - close_price) / entry_price * 100.0

    notional = row.get("notional_usdt")
    if notional and (commission_open is not None or commission_close is not None):
        fees = abs((commission_open or 0.0) + (commission_close or 0.0))
        out["commission_pct_of_notional"] = fees / notional * 100.0
    return out


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
        logger.info(f"Database initialized: db={self.db_name} (testnet={IS_TESTNET})")

    def _create_indexes(self) -> None:
        self.db.balance.create_index([("asset", ASCENDING), ("timestamp", DESCENDING)])

        self.db.positions.create_index("order_id", unique=True, sparse=True)
        self.db.positions.create_index([("status", ASCENDING), ("created_at", DESCENDING)])
        self.db.positions.create_index([("status", ASCENDING), ("closed_at", DESCENDING)])
        self.db.positions.create_index([("symbol", ASCENDING), ("side", ASCENDING), ("status", ASCENDING)])

        self.db.settings.create_index("key", unique=True)

        # trade_analytics — окрема від positions таблиця: тут накопичується
        # ВСЯ історія угоди (а не лише останній стан), включно з кожним
        # перенесенням SL і кожним частковим TP. positions лишається
        # недоторканою (її читає webapp, risk_manager і т.д.) — trade_analytics
        # існує ПОРЯД, спеціально для аналізу "що саме пішло не так".
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
            # Розбивка commission_usdt на складові (ТЗ TZ_fix_pnl_accounting.md,
            # задача 2) — commission_usdt лишається сумою цих двох (+funding
            # окремо не входить у нього, funding не комісія біржі) заради
            # зворотної сумісності з усім, що вже читає саме commission_usdt
            # (webapp /api/stats, generate_pnl_card, risk_manager).
            "commission_open": None,
            "commission_close": None,
            "funding_fee": None,
            "net_pnl": None,
            # Значення net_pnl ДО фіксу формули знаку (задача 1 ТЗ) — пишеться
            # окремо скриптом міграції історичних даних (задача 3), а НЕ тут;
            # поле заведено заздалегідь, щоб міграція не потребувала зміни
            # схеми. Для нових позицій (після фіксу) лишається None.
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
        """
        Оновлює статус позиції. Усі метрики — опціональні: якщо не передані,
        відповідні поля не чіпаються (тільки $set по переданих полях, щоб
        проміжний виклик не затер вже записані значення None-ом).

        commission_open/commission_close/funding_fee — розбивка (ТЗ, задача 2),
        додана поряд з уже існуючим сумарним commission_usdt, а не замість
        нього: весь код, що вже читає commission_usdt (webapp /api/stats,
        generate_pnl_card, risk_manager), продовжує працювати без змін.
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
        if commission_open is not None:
            update_fields["commission_open"] = commission_open
        if commission_close is not None:
            update_fields["commission_close"] = commission_close
        if funding_fee is not None:
            update_fields["funding_fee"] = funding_fee
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
    # trade_analytics — повна історія угоди: як саме переставлявся SL,
    # якими частками спрацював TP, з якими налаштуваннями стратегії угода
    # була відкрита, в якому режимі (testnet/live). positions лишається
    # "оперативною" таблицею (останній стан), trade_analytics — архівом
    # для розбору "чому саме ця угода пішла в мінус".
    # ------------------------------------------------------------------

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
    ) -> Any:
        """
        equity_at_entry / concurrent_positions / signal_context — контекст
        входу для розбору "чому збиткова": equity на момент входу, скільки
        інших позицій було відкрито, і знімок індикаторів стратегії на сигнальній
        свічці (ATR%, об'єм до середнього, відстань до EMA тощо). Окремо від
        них рахуються похідні метрики (notional, стоп у %, запланований ризик,
        проскальзування входу, година/день тижня) — див. _entry_derived_fields.

        Пишеться ОДИН раз при відкритті угоди (бот або ручне відкриття,
        що його підхопив _handle_account_update). strategy_params — знімок
        активних параметрів стратегії станом на момент входу (з
        StrategySettingsStore.get_params), щоб пізніше можна було чесно
        порівняти результати угод, відкритих ДО і ПІСЛЯ зміни налаштувань
        в міні-аппі — на відміну від positions.metadata, це значення
        НІКОЛИ не перезаписується.
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
            # MFE/MAE по ходу угоди пише ExcursionTracker (update_trade_excursion)
            "excursion": None,
        }
        doc.update(_entry_derived_fields(
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
        Один рядок на КОЖНЕ успішне перенесення SL (ladder-рівень, atr_3step
        крок, або аварійний fallback). trigger — людиночитний опис, що саме
        спрацювало (напр. "level_2pct_roi", "atr_3step_step_2",
        "fallback:last_positive"). reason розрізняє штатний trail від
        аварійного fallback — саме це дозволить потім подивитись "скільки
        разів бот взагалі залишався без штатного SL".
        Якщо запису в trade_analytics ще немає (угода відкрита до того, як
        з'явилась ця таблиця) — update_one з order_id, що не matched,
        просто нічого не зробить, і це нормально (не кидаємо виняток).
        """
        move = {
            "at": datetime.utcnow(),
            "trigger": trigger,
            "reason": reason,
            "old_stop_price": old_stop_price,
            "new_stop_price": new_stop_price,
            "old_order_id": old_order_id,
            "new_order_id": new_order_id,
            "leverage": leverage,
            "roi_percent": roi_percent,
        }
        with self._lock:
            self.db.trade_analytics.update_one(
                {"order_id": order_id},
                {"$push": {"sl_moves": move}},
            )

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
        """Один рядок на кожне (часткове чи фінальне) спрацювання TAKE_PROFIT_MARKET ордера."""
        fill = {
            "at": datetime.utcnow(),
            "order_id": tp_order_id,
            "client_order_id": client_order_id,
            "price": price,
            "quantity": quantity,
            "realized_pnl": realized_pnl,
            "commission": commission,
            "trade_id": trade_id,
        }
        with self._lock:
            self.db.trade_analytics.update_one(
                {"order_id": order_id},
                {"$push": {"tp_fills": fill}},
            )

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
        """Пишеться ОДИН раз, коли угода закрита ПОВНІСТЮ (а не на кожному
        частковому закритті — для цього є append_tp_fill/append_sl_move)."""
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
            update_fields.update(_exit_derived_fields(
                row, close_price, net_pnl, commission_open, commission_close, close_reason,
            ))

            self.db.trade_analytics.update_one(
                {"order_id": order_id},
                {"$set": update_fields},
            )

    def update_trade_excursion(self, order_id: str, excursion: dict) -> None:
        """Перезаписує піддокумент excursion (MFE/MAE) — його повністю
        перераховує ExcursionTracker, тому $set цілого піддокумента безпечний."""
        with self._lock:
            self.db.trade_analytics.update_one(
                {"order_id": order_id},
                {"$set": {"excursion": excursion}},
            )

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
        query: Dict[str, Any] = {}
        if symbol:
            query["symbol"] = symbol
        if strategy:
            query["strategy"] = strategy
        if mode:
            query["mode"] = mode
        if status:
            query["status"] = status

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
        """
        Агрегація по (strategy, symbol) серед ЗАКРИТИХ угод: win-rate,
        середній ROE%, скільки разів в середньому переносився SL до
        закриття — найшвидший спосіб побачити, яка стратегія/монета
        реально відпрацьовує, а яка зливає депозит.
        """
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