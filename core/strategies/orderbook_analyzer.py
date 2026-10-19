"""
OrderBookAnalyzer — аналіз книги заявок (order book) підписаних символів на
предмет великих щільностей ("стін") і їх пробою.

Сам аналізатор і надалі лише детектить + логує + публікує подію в
EventBus (WALL_DETECTED/WALL_BREAKOUT); TelegramBot завжди підписаний на
обидві й шле сповіщення. Прямого виклику exchange.create_order тут немає
і не буде — це свідомо окремий шар "детект".

Чи відкриється реальна угода на основі WALL_BREAKOUT — залежить від
core/strategies/wall_breakout_strategy.py (WallBreakoutStrategy): вона
підписана на ту саму подію і, якщо увімкнена в мініаппі,
формує TradeSignal і публікує SIGNAL_GENERATED, який підхоплює
SimpleTrader. За замовчуванням ця стратегія вимкнена.

Джерело даних: EventType.ORDERBOOK_UPDATED від BingXClient._handle_depth_update
— це ПОВНИЙ знепшот топ-N рівнів (bids/asks), що приходить ~раз/сек, а НЕ
інкрементальні дельти. Тому окремої логіки синхронізації порядкового номера
(як для incrDepth) тут свідомо немає — кожне повідомлення повністю заміняє
попередній стан книги для символу.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, Dict, List, Optional, Tuple

from ..common.market_data import parse_price_tick
from ..events import Event, EventBus, EventType

logger = logging.getLogger(__name__)


@dataclass
class _Wall:
    price: float
    quantity: float             # поточний обсяг на рівні (оновлюється щоразу)
    side: str                   # 'bid' | 'ask'
    first_seen: datetime
    initial_quantity: float     # обсяг у момент першого детекту — база для % "з'їдання"
    avg_level_quantity: float = 0.0  # середній обсяг рівня того ж боку книги при детекті


@dataclass
class _SymbolState:
    bids: List[Tuple[float, float]] = field(default_factory=list)
    asks: List[Tuple[float, float]] = field(default_factory=list)
    walls: Dict[str, _Wall] = field(default_factory=dict)  # key = f"{side}:{price:.8f}"
    last_price: Optional[float] = None
    # (unix-час, ціна) за останні _PRICE_HISTORY_SECONDS — імпульс перед пробоєм
    prices: Deque[Tuple[float, float]] = field(default_factory=deque)


_PRICE_HISTORY_SECONDS = 300.0


class OrderBookAnalyzer:
    def __init__(self, event_bus: EventBus, config: Optional[dict] = None):
        self.event_bus = event_bus

        cfg = config or {}
        self.enabled: bool = cfg.get('enabled', True)

        # рівень вважається "стіною", якщо його обсяг у wall_multiplier разів
        # більший за середній обсяг рівня в тому ж боці книги
        self.wall_multiplier: float = cfg.get('wall_multiplier', 5.0)
        # додатковий абсолютний поріг (у базовому активі), щоб відсіяти
        # "стіни" на мікрооб'ємах у неліквідних інструментах; 0 — вимкнено
        self.min_wall_quantity: float = cfg.get('min_wall_quantity', 0.0)
        # наскільки близько (у %) ціна має підійти до рівня стіни, щоб
        # почати перевірку на пробій
        self.approach_threshold_pct: float = cfg.get('approach_threshold_pct', 0.5)
        # скільки % від початкового обсягу стіни має "з'їстись" ринковими
        # угодами, щоб перетин рівня рахувався пробоєм, а не шумом
        self.breakout_consumption_pct: float = cfg.get('breakout_consumption_pct', 30.0)
        # через скільки секунд неактуальна (зникла зі знепшоту) стіна
        # прибирається зі стану, якщо пробою так і не відбулось
        self.wall_stale_seconds: float = cfg.get('wall_stale_seconds', 300.0)

        self._states: Dict[str, _SymbolState] = {}

        if self.enabled:
            self.event_bus.subscribe(EventType.ORDERBOOK_UPDATED, self._on_orderbook_update)
            self.event_bus.subscribe(EventType.PRICE_UPDATED, self._on_price_update)
            logger.info(
                f"OrderBookAnalyzer initialized: wall_multiplier={self.wall_multiplier}x, "
                f"approach_threshold={self.approach_threshold_pct}%, "
                f"breakout_consumption={self.breakout_consumption_pct}% "
                f"(detect+log+publish only; trading via WallBreakoutStrategy if enabled)"
            )
        else:
            logger.info("OrderBookAnalyzer initialized but disabled via config")

    # ---------- допоміжне ----------

    @staticmethod
    def _parse_levels(raw_levels) -> List[Tuple[float, float]]:
        levels = []
        for lvl in raw_levels or []:
            try:
                price = float(lvl[0])
                qty = float(lvl[1])
            except (TypeError, ValueError, IndexError):
                continue
            if qty > 0:
                levels.append((price, qty))
        return levels

    def compute_imbalance(
        self, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]]
    ) -> float:
        """(bid_volume - ask_volume) / (bid_volume + ask_volume), у [-1, 1].
        >0 — переважає попит (bids товщі), <0 — переважає пропозиція."""
        bid_vol = sum(q for _, q in bids)
        ask_vol = sum(q for _, q in asks)
        total = bid_vol + ask_vol
        if total <= 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def _detect_walls(self, levels: List[Tuple[float, float]], side: str) -> List[_Wall]:
        if len(levels) < 3:
            return []
        avg_qty = statistics.mean(q for _, q in levels)
        if avg_qty <= 0:
            return []
        now = datetime.now(timezone.utc)
        return [
            _Wall(price=price, quantity=qty, side=side, first_seen=now, initial_quantity=qty,
                  avg_level_quantity=avg_qty)
            for price, qty in levels
            if qty >= avg_qty * self.wall_multiplier and qty >= self.min_wall_quantity
        ]

    # ---------- вхідні події ----------

    async def _on_orderbook_update(self, event: Event) -> None:
        if not self.enabled:
            return
        data = event.data or {}
        symbol = data.get('symbol')
        if not symbol:
            return

        bids = self._parse_levels(data.get('bids'))
        asks = self._parse_levels(data.get('asks'))
        if not bids and not asks:
            return

        state = self._states.setdefault(symbol, _SymbolState())
        state.bids = bids
        state.asks = asks

        imbalance = self.compute_imbalance(bids, asks)

        detected_now = self._detect_walls(bids, 'bid') + self._detect_walls(asks, 'ask')
        current_keys = set()

        for wall in detected_now:
            key = f"{wall.side}:{wall.price:.8f}"
            current_keys.add(key)
            existing = state.walls.get(key)
            if existing is None:
                state.walls[key] = wall
                await self._notify_wall(symbol, wall, imbalance)
            else:
                # оновлюємо поточний обсяг — це і є "спостереження за
                # з'їданням" стіни ринковими угодами для детекту пробою
                existing.quantity = wall.quantity

        # прибираємо стіни, які зникли зі знепшоту (виконались/відкликані)
        # і встигли застаріти без пробою — щоб не накопичувати сміття в стані
        now = datetime.now(timezone.utc)
        for key in list(state.walls.keys()):
            if key in current_keys:
                continue
            age = (now - state.walls[key].first_seen).total_seconds()
            if age > self.wall_stale_seconds:
                del state.walls[key]

    async def _on_price_update(self, event: Event) -> None:
        if not self.enabled:
            return
        tick = parse_price_tick(event.data)
        if tick is None:
            return
        symbol, price = tick

        state = self._states.get(symbol)
        if state is None:
            return
        state.last_price = price
        now_ts = time.time()
        state.prices.append((now_ts, price))
        while state.prices and now_ts - state.prices[0][0] > _PRICE_HISTORY_SECONDS:
            state.prices.popleft()
        if not state.walls:
            return

        for key, wall in list(state.walls.items()):
            distance_pct = abs(price - wall.price) / wall.price * 100.0
            if distance_pct > self.approach_threshold_pct:
                continue

            consumed_pct = (
                (1 - wall.quantity / wall.initial_quantity) * 100.0
                if wall.initial_quantity > 0 else 0.0
            )
            crossed = (
                (wall.side == 'ask' and price > wall.price) or
                (wall.side == 'bid' and price < wall.price)
            )
            if crossed and consumed_pct >= self.breakout_consumption_pct:
                logger.warning(
                    f"OrderBook: WALL BREAKOUT {symbol} {wall.side.upper()} "
                    f"wall_price={wall.price:.6f} consumed={consumed_pct:.0f}% "
                    f"current_price={price:.6f}"
                )
                context = self._breakout_context(state, wall, price, now_ts)
                await self._notify_breakout(symbol, wall, price, consumed_pct, context)
                del state.walls[key]

    # ---------- контекст пробою (для trade_analytics) ----------

    @staticmethod
    def _price_change_pct(state: _SymbolState, price: float, now_ts: float, seconds: float) -> Optional[float]:
        """Зміна ціни за останні `seconds` (None — історії ще замало)."""
        if not state.prices or now_ts - state.prices[0][0] < seconds * 0.8:
            return None
        base = next((p for t, p in state.prices if now_ts - t <= seconds), None)
        return (price - base) / base * 100.0 if base else None

    def _breakout_context(self, state: _SymbolState, wall: _Wall, price: float, now_ts: float) -> dict:
        bid_depth = sum(p * q for p, q in state.bids)
        ask_depth = sum(p * q for p, q in state.asks)
        best_bid = state.bids[0][0] if state.bids else None
        best_ask = state.asks[0][0] if state.asks else None
        return {
            'wall_side': wall.side,
            'wall_price': wall.price,
            'wall_notional_usdt': wall.initial_quantity * wall.price,
            'wall_multiple': (
                wall.initial_quantity / wall.avg_level_quantity if wall.avg_level_quantity > 0 else None
            ),
            'wall_age_seconds': (datetime.now(timezone.utc) - wall.first_seen).total_seconds(),
            'breakout_distance_pct': abs(price - wall.price) / wall.price * 100.0,
            'book_imbalance': self.compute_imbalance(state.bids, state.asks),
            'bid_depth_usdt': bid_depth,
            'ask_depth_usdt': ask_depth,
            'book_spread_pct': (
                (best_ask - best_bid) / best_bid * 100.0 if best_bid and best_ask and best_bid > 0 else None
            ),
            'price_change_60s_pct': self._price_change_pct(state, price, now_ts, 60.0),
            'price_change_300s_pct': self._price_change_pct(state, price, now_ts, 300.0),
        }

    # ---------- сповіщення ----------

    async def _notify_wall(self, symbol: str, wall: _Wall, imbalance: float) -> None:
        try:
            await self.event_bus.publish(Event(
                type=EventType.WALL_DETECTED,
                data={
                    'symbol': symbol,
                    'side': wall.side,
                    'price': wall.price,
                    'quantity': wall.quantity,
                    'imbalance': imbalance,
                },
                source="OrderBookAnalyzer",
            ))
        except Exception as e:
            logger.error(f"OrderBook: failed to publish WALL_DETECTED: {e}")

    async def _notify_breakout(
        self, symbol: str, wall: _Wall, price: float, consumed_pct: float, context: Optional[dict] = None,
    ) -> None:
        try:
            await self.event_bus.publish(Event(
                type=EventType.WALL_BREAKOUT,
                data={
                    'symbol': symbol,
                    'side': wall.side,
                    'wall_price': wall.price,
                    'current_price': price,
                    'consumed_pct': consumed_pct,
                    'context': context or {},
                },
                source="OrderBookAnalyzer",
            ))
        except Exception as e:
            logger.error(f"OrderBook: failed to publish WALL_BREAKOUT: {e}")