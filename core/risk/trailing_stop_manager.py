"""
Керування SL у рантаймі: єдиний стоп-лосс позиції переставляється
СХОДИНКАМИ у бік прибутку, поки ціна не досягла чергового порогу — і
ніколи не рухається назад.

Два режими (вибираються по позиції, а не глобально):
  - ladder (за замовчуванням): пороги у % ROI з trail_levels_percent;
  - atr_3step: якщо в position['trail_meta'] mode == 'atr_3step' (його
    кладе TrendSupertrendStrategy через сигнал), стоп веде 3-крокова
    схема в кратних ATR на момент входу — див. _process_atr_position.

"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..events import EventBus, Event, EventType
from ..exchange.bingx_client import BingXAPIError
from ..strategies.indicators import interval_to_ms

logger = logging.getLogger(__name__)

_GONE_ERROR_CODES = (100404, 100400, 109400, 109420)
_GONE_ERROR_SUBSTRINGS = ('not exist', 'not found')

@dataclass
class _TrailState:
    last_applied_level_index: int = -1
    initial_stop_price: Optional[float] = None
    last_positive_stop_price: Optional[float] = None
    last_positive_roi_percent: Optional[float] = None
    fallback_notice_key: Optional[str] = None
    critical_notice_key: Optional[str] = None
    # --- режим atr_3step: кеш крок-3 (свінг-стоп за екстремумом свічок) ---
    atr_swing_stop: Optional[float] = None
    atr_candle_bucket: Optional[int] = None
    atr_refresh_inflight: bool = False
    atr_refresh_retry_after: float = 0.0
    atr_not_ready_count: int = 0
    atr_invalid_notice: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

class TrailingStopManager:
    def __init__(
        self,
        event_bus: EventBus,
        exchange,
        db,
        trader,
        config: Optional[dict] = None,
    ):
        self.event_bus = event_bus
        self.exchange = exchange
        self.db = db
        self.trader = trader

        cfg = config or {}
        self.enabled: bool = cfg.get('enabled', True)

        # Пороги НЕ читаются из config.yaml и не имеют здесь хардкод-дефолта:
        # единственный источник — параметр trail_levels_percent стратегии
        # (DEFAULT_PARAMS -> БД -> мини-апп). Значение приходит через
        # set_levels() сразу после старта StrategyManager и при каждом
        # изменении из мини-аппа. Пока уровней нет — трейлинг ничего не делает.
        self.trail_levels_percent: List[float] = []

        self.stop_buffer_percent: float = cfg.get('dynamic_stop_buffer_percent', 0.5)

        self.max_buffer_fraction_of_level: float = cfg.get('max_buffer_fraction_of_level', 0.8)

        self.market_safety_buffer_price_percent: float = cfg.get(
            'market_safety_buffer_price_percent', 0.05
        )

        self.move_retry_cooldown_seconds: float = 10.0 

        # Комісія однієї сторони (taker) для розрахунку "безубитку" в режимі
        # atr_3step: SL ставиться на entry ± 2 * taker_fee_rate * entry
        # (вхід + вихід), щоб закриття по безубитку не йшло в мінус.
        self.taker_fee_rate: float = cfg.get('taker_fee_rate', 0.0005)

        self._bg_tasks: set = set()

        self._states: Dict[str, _TrailState] = {}
        self._retry_after: Dict[str, float] = {}

        if self.enabled:
            self.event_bus.subscribe(EventType.PRICE_UPDATED, self._on_price_update)
            self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_closed)

    def set_levels(self, raw_levels) -> None:
        """Применяет новые пороги (% ROI) на лету. Дубликаты и значения ≤ 0
        отбрасываются, список сортируется по возрастанию. Уже достигнутые
        позициями уровни (last_applied_level_index) индексируются по
        отсортированному списку, поэтому после смены списка они
        пересчитываются от новых порогов — SL назад всё равно не двигается."""
        try:
            levels = sorted({float(x) for x in (raw_levels or []) if float(x) > 0})
        except (TypeError, ValueError):
            logger.error(f"TrailingStop: invalid trail_levels_percent {raw_levels!r} — ignored")
            return

        if levels == self.trail_levels_percent:
            return
        self.trail_levels_percent = levels

        reset_count = 0
        for state in self._states.values():
            if state.last_applied_level_index != -1:
                state.last_applied_level_index = -1
                reset_count += 1

        if levels:
            logger.info(
                f"TrailingStop: levels updated (%ROI): {levels}"
                + (f" — re-evaluating {reset_count} open position(s) from scratch" if reset_count else "")
            )
        else:
            logger.warning("TrailingStop: levels list is empty — trailing is effectively OFF")

    async def _on_price_update(self, event: Event) -> None:
        if not self.enabled:
            return

        raw = event.data
        if not raw:
            return

        try:
            tick = raw[0] if isinstance(raw, list) else raw
        except (IndexError, TypeError):
            return

        symbol = tick.get('s')
        try:
            price = float(tick.get('p', 0))
        except (TypeError, ValueError):
            price = 0.0

        if not symbol or price <= 0:
            return

        for side in ('LONG', 'SHORT'):
            position_key = f"{symbol}_{side}"
            position = self.trader.open_positions.get(position_key)
            if not position:
                continue
            # ladder-позиції без заданих рівнів не чіпаємо (як і раніше);
            # atr_3step рівнів trail_levels_percent не потребує
            if not self.trail_levels_percent and not self._is_atr_mode(position):
                continue
            await self._process_position(position_key, position, price)

    @staticmethod
    def _price_fraction(entry_price: float, side: str, price: float) -> float:
        return (
            (price - entry_price) / entry_price if side == 'LONG'
            else (entry_price - price) / entry_price
        )

    def _roi_percent(self, entry_price: float, side: str, price: float, leverage: float) -> float:
        return self._price_fraction(entry_price, side, price) * leverage * 100.0

    @staticmethod
    def _safe_leverage(position: dict, position_key: str = "") -> float:
        leverage = position.get('leverage') or 1
        try:
            leverage = float(leverage)
        except (TypeError, ValueError):
            logger.warning(
                f"TrailingStop: {position_key} invalid leverage={leverage!r}, falling back to 1x"
            )
            leverage = 1.0
        return leverage if leverage > 0 else 1.0

    def _track_last_positive(
        self, state: "_TrailState", entry_price: float, side: str,
        stop_price: Optional[float], leverage: float,
    ) -> None:
        if stop_price is None or not entry_price:
            return
        fraction = self._price_fraction(entry_price, side, stop_price)
        if fraction > 0:
            state.last_positive_stop_price = stop_price
            state.last_positive_roi_percent = fraction * leverage * 100.0

    def _persist_position(self, position: dict, context: str) -> None:
        try:
            self.db.update_position_metadata(order_id=position['order_id'], metadata=json.dumps(position))
        except Exception as e:
            logger.error(
                f"TrailingStop: failed to persist position metadata ({context}) for "
                f"{position.get('symbol')}_{position.get('side')}: {e}", exc_info=True
            )

    def _highest_reached_level_index(
        self, favorable_fraction: float, last_applied_index: int, leverage: float
    ) -> int:
        target = last_applied_index
        for i, level_roi_percent in enumerate(self.trail_levels_percent):
            if i <= target:
                continue
            level_price_percent = level_roi_percent / leverage
            if favorable_fraction < level_price_percent / 100.0:
                break
            target = i
        return target

    def _stop_price_for_level(
        self, entry_price: float, side: str, level_roi_percent: float, leverage: float
    ) -> float:
        level_price_percent = level_roi_percent / leverage
        buffer_price_percent = self.stop_buffer_percent / leverage
        max_buffer_price_percent = level_price_percent * self.max_buffer_fraction_of_level
        if buffer_price_percent > max_buffer_price_percent:
            buffer_price_percent = max_buffer_price_percent
        effective_percent = max(level_price_percent - buffer_price_percent, 0.0)
        return (
            entry_price * (1 + effective_percent / 100.0) if side == 'LONG'
            else entry_price * (1 - effective_percent / 100.0)
        )

    def _anchor_stop_to_market(
        self, desired_stop_price: float, current_market_price: float, side: str
    ) -> float:
        if not current_market_price or current_market_price <= 0:
            return desired_stop_price
        buffer = self.market_safety_buffer_price_percent / 100.0
        if side == 'LONG':
            max_valid_stop = current_market_price * (1 - buffer)
            return min(desired_stop_price, max_valid_stop)
        else:
            min_valid_stop = current_market_price * (1 + buffer)
            return max(desired_stop_price, min_valid_stop)

    async def _process_position(self, position_key: str, position: dict, price: float) -> None:
        retry_after = self._retry_after.get(position_key)
        if retry_after is not None:
            if time.time() < retry_after:
                return
            self._retry_after.pop(position_key, None)

        entry_price = position.get('entry_price')
        side = position.get('side')
        if not entry_price or entry_price <= 0 or side not in ('LONG', 'SHORT'):
            logger.debug(
                f"TrailingStop: {position_key} SKIP: invalid entry_price/side "
                f"(entry_price={entry_price}, side={side})"
            )
            return
        if not position.get('sl_order_id'):
            logger.warning(
                f"TrailingStop: {position_key} has NO stop loss on the exchange — "
                f"position is UNPROTECTED, attempting emergency recreation"
            )
            await self._place_fallback_to_last_stop(position_key, position)
            self._retry_after[position_key] = time.time() + self.move_retry_cooldown_seconds
            return

        leverage = self._safe_leverage(position, position_key)

        favorable_fraction = self._price_fraction(entry_price, side, price)
        favorable_roi_percent = favorable_fraction * leverage * 100.0

        state = self._states.setdefault(position_key, _TrailState())

        if state.last_positive_stop_price is None:
            self._track_last_positive(state, entry_price, side, position.get('stop_loss_price'), leverage)

        if self._is_atr_mode(position):
            await self._process_atr_position(position_key, position, state, price)
            return

        current_sl_price = position.get('stop_loss_price')
        sl_roi_str = (
            f"{self._roi_percent(entry_price, side, current_sl_price, leverage):+.2f}%ROI"
            if current_sl_price is not None else "None"
        )
        next_level = (
            self.trail_levels_percent[state.last_applied_level_index + 1]
            if state.last_applied_level_index + 1 < len(self.trail_levels_percent) else None
        )
        next_level_str = f"{next_level:g}%ROI" if next_level is not None else "none left"
        logger.debug(
            f"TrailingStop: {position_key} price={favorable_roi_percent:+.2f}%ROI, "
            f"sl={sl_roi_str}, next_level={next_level_str}"
        )

        if favorable_fraction <= 0:
            return

        target_index = self._highest_reached_level_index(
            favorable_fraction, state.last_applied_level_index, leverage
        )
        if target_index <= state.last_applied_level_index:
            return

        level_roi_percent = self.trail_levels_percent[target_index]

        candidates = []
        for idx in range(target_index, state.last_applied_level_index, -1):
            lvl = self.trail_levels_percent[idx]
            stop_price = self._stop_price_for_level(entry_price, side, lvl, leverage)
            candidates.append((idx, lvl, stop_price))

        async with state.lock:
            if target_index <= state.last_applied_level_index:
                return
            position = self.trader.open_positions.get(position_key)
            if not position or not position.get('sl_order_id'):
                return

            current_stop = position.get('stop_loss_price')
            if current_stop is not None:
                candidates = [
                    c for c in candidates
                    if (c[2] > current_stop if side == 'LONG' else c[2] < current_stop)
                ]
                if not candidates:
                    state.last_applied_level_index = target_index
                    return

            new_index = await self._move_stop_loss(position_key, position, state, candidates)
            if new_index is not None:
                advanced = new_index > state.last_applied_level_index
                state.last_applied_level_index = new_index
                if advanced:
                    state.fallback_notice_key = None
                    state.critical_notice_key = None
            else:
                self._retry_after[position_key] = time.time() + self.move_retry_cooldown_seconds

    async def _move_stop_loss(
        self, position_key: str, position: dict, state: "_TrailState", candidates: List[tuple],
        stage: Optional[str] = None, working_type: Optional[str] = None,
    ) -> Optional[int]:
        """candidates: [(index, value, stop_price), ...] від найкращого до гіршого.
        stage=None — ladder (value = рівень у % ROI); stage='atr_3step' —
        index = номер кроку 1..3 (value не використовується в тексті).
        working_type прокидається в create_order (напр. 'MARK_PRICE')."""
        def describe(idx: int, value: float) -> str:
            return f"{stage} step {idx}" if stage else f"level {value:g}%ROI"

        symbol = position['symbol']
        side = position['side']
        quantity = position.get('remaining_quantity') or position.get('quantity')
        close_side = 'SELL' if side == 'LONG' else 'BUY'

        old_sl_order_id = position.get('sl_order_id')
        old_stop_price = position.get('stop_loss_price')

        try:
            await self.exchange.cancel_order(symbol, old_sl_order_id)
        except BingXAPIError as e:
            if e.code == 109429:
                self._apply_rate_limit_backoff(position_key, e.msg)
                logger.warning(f"TrailingStop: rate limited (109429) cancelling SL for {position_key}")
                return None
            if self._is_gone_error(e):
                return None
            logger.error(f"TrailingStop: failed to cancel SL for {position_key}: {e.code} {e.msg}")
            return None
        except Exception as e:
            logger.error(f"TrailingStop: unexpected error cancelling SL for {position_key}: {e}", exc_info=True)
            return None

        position['sl_order_id'] = None
        self._persist_position(position, context="cleared sl_order_id after cancel")

        current_market_price: Optional[float] = None
        try:
            current_market_price = await self.exchange.get_mark_price(symbol)
        except Exception as e:
            logger.warning(
                f"TrailingStop: {position_key} failed to fetch fresh mark price before "
                f"placing SL, falling back to tick-derived candidate prices: {e}"
            )

        rate_limited = False
        for level_index, level_roi_percent, desired_stop_price in candidates:
            if current_market_price:
                anchored_price = self._anchor_stop_to_market(
                    desired_stop_price, current_market_price, side
                )
                if old_stop_price is not None:
                    anchored_price = (
                        max(anchored_price, old_stop_price) if side == 'LONG'
                        else min(anchored_price, old_stop_price)
                    )
                if anchored_price != desired_stop_price:
                    logger.info(
                        f"TrailingStop: {position_key} {describe(level_index, level_roi_percent)} "
                        f"price anchored to live market {current_market_price:.6f}: "
                        f"{desired_stop_price:.6f} -> {anchored_price:.6f}"
                    )
                desired_stop_price = anchored_price
            client_order_id = f"sl-{int(time.time() * 1000)}"
            try:
                response = await self.exchange.create_order(
                    symbol=symbol,
                    side=close_side,
                    order_type='STOP_MARKET',
                    quantity=quantity,
                    stop_price=desired_stop_price,
                    position_side=side,
                    close_position=True,
                    client_order_id=client_order_id,
                    working_type=working_type,
                )
            except BingXAPIError as e:
                if e.code == 109429:
                    self._apply_rate_limit_backoff(position_key, e.msg)
                    logger.error(
                        f"TrailingStop: rate limited (109429) creating SL for {position_key} "
                        f"mid-ladder ({describe(level_index, level_roi_percent)}) — stopping, old SL already cancelled!"
                    )
                    rate_limited = True
                    break
                if self._is_gone_error(e):
                    return None
                if e.code == 110406:
                    logger.warning(
                        f"TrailingStop: {position_key} got 110406 (SL already exists) — some SL order "
                        f"is present on the exchange, resync next tick"
                    )
                    return None
                logger.warning(
                    f"TrailingStop: {position_key} {describe(level_index, level_roi_percent)} "
                    f"({desired_stop_price:.6f}) REJECTED: {e.code} {e.msg} — trying next closer level..."
                )
                continue
            except Exception as e:
                logger.error(
                    f"TrailingStop: unexpected error creating SL for {position_key} at "
                    f"{describe(level_index, level_roi_percent)}: {e}", exc_info=True
                )
                continue

            new_order_id = None
            if response and 'data' in response and 'order' in response['data']:
                new_order_id = response['data']['order'].get('orderId')
            if not new_order_id:
                logger.error(f"TrailingStop: SL created for {position_key} but no orderId in response: {response}.")
                await self._notify_critical(
                    f"SL для {symbol} {side} перевиставлено, але не вдалось прочитати orderId — перевірте вручну!"
                )

            position['stop_loss_price'] = desired_stop_price
            position['sl_order_id'] = str(new_order_id) if new_order_id else None
            position['sl_client_order_id'] = client_order_id
            self._persist_position(position, context="moved SL")

            entry_price = position.get('entry_price')
            leverage = self._safe_leverage(position, position_key)
            self._track_last_positive(state, entry_price, side, desired_stop_price, leverage)

            if level_index != candidates[0][0]:
                logger.warning(
                    f"TrailingStop: {position_key} ЦІЛЬОВИЙ рівень ({describe(candidates[0][0], candidates[0][1])}) "
                    f"провалився — застосовано найближчий доступний ({describe(level_index, level_roi_percent)})"
                )

            trigger_label = f"{stage}_step_{level_index}" if stage else f"level_{level_roi_percent:g}pct_roi"

            try:
                await self.event_bus.publish(Event(
                    type=EventType.STOP_LOSS_MOVED,
                    data={
                        'symbol': symbol,
                        'side': side,
                        'stage': trigger_label,
                        'entry_price': entry_price,
                        'old_stop_price': old_stop_price,
                        'new_stop_price': desired_stop_price,
                        'leverage': leverage,
                        'strategy': position.get('strategy'),
                    },
                    source="TrailingStopManager",
                ))
            except Exception as e:
                logger.error(f"TrailingStop: failed to publish STOP_LOSS_MOVED event: {e}")

            try:
                self.db.append_sl_move(
                    order_id=position['order_id'],
                    trigger=trigger_label,
                    old_stop_price=old_stop_price,
                    new_stop_price=desired_stop_price,
                    old_order_id=old_sl_order_id,
                    new_order_id=position.get('sl_order_id'),
                    leverage=leverage,
                    roi_percent=self._roi_percent(entry_price, side, desired_stop_price, leverage) if entry_price else None,
                    reason='trail',
                )
            except Exception as e:
                logger.error(f"TrailingStop: failed to append sl_move to trade_analytics for {position_key}: {e}", exc_info=True)

            return level_index

        if rate_limited:
            return None

        logger.error(
            f"TrailingStop: ALL {len(candidates)} level candidates failed for {position_key} "
            f"(old SL already cancelled) — escalating to last_positive/initial fallback"
        )
        ok = await self._place_fallback_to_last_stop(position_key, position)
        return state.last_applied_level_index if ok else None

    # ---------- режим atr_3step ----------

    @staticmethod
    def _is_atr_mode(position: dict) -> bool:
        meta = position.get('trail_meta')
        return isinstance(meta, dict) and meta.get('mode') == 'atr_3step'

    async def _process_atr_position(
        self, position_key: str, position: dict, state: "_TrailState", price: float
    ) -> None:
        """
        3-кроковий супровід (усе в кратних ATR, виміряному в момент входу):
          1) профіт >= breakeven_atr*ATR  -> SL = entry ± 2*taker_fee*entry (безубиток з комісіями)
          2) профіт >= lock_trigger_atr*ATR -> SL = entry ± lock_offset_atr*ATR
          3) після кроку 2 -> SL за екстремумом останніх `lookback` ЗАКРИТИХ
             свічок робочого ТФ (Low для LONG / High для SHORT) із зазором
             gap_atr*ATR. Оновлюється раз на закриття свічки.
        SL рухається ТІЛЬКИ в бік прибутку. Ордер переставляється через
        існуючий _move_stop_loss (cancel -> create, fallback, rate-limit),
        тригер — MARK_PRICE.
        """
        meta = position['trail_meta']
        entry_price = position['entry_price']
        side = position['side']

        try:
            atr_value = float(meta.get('atr') or 0)
            breakeven_atr = float(meta.get('breakeven_atr', 1.0))
            lock_trigger_atr = float(meta.get('lock_trigger_atr', 2.0))
            lock_offset_atr = float(meta.get('lock_offset_atr', 1.0))
        except (TypeError, ValueError):
            atr_value = 0.0
        if atr_value <= 0:
            if not state.atr_invalid_notice:
                state.atr_invalid_notice = True
                logger.error(f"TrailingStop: {position_key} atr_3step: invalid trail_meta {meta!r} — trailing skipped")
            return

        sign = 1.0 if side == 'LONG' else -1.0
        profit = sign * (price - entry_price)
        current_stop = position.get('stop_loss_price')

        breakeven_price = entry_price + sign * entry_price * 2.0 * self.taker_fee_rate
        lock_price = entry_price + sign * lock_offset_atr * atr_value

        def improves(candidate_price: float, stop: Optional[float]) -> bool:
            if stop is None:
                return True
            return candidate_price > stop if side == 'LONG' else candidate_price < stop

        step3_active = profit >= lock_trigger_atr * atr_value or (
            current_stop is not None
            and (current_stop >= lock_price if side == 'LONG' else current_stop <= lock_price)
        )

        candidates = []
        if profit >= breakeven_atr * atr_value:
            candidates.append((1, breakeven_atr, breakeven_price))
        if profit >= lock_trigger_atr * atr_value:
            candidates.append((2, lock_trigger_atr, lock_price))
        if step3_active:
            self._schedule_swing_refresh(position_key, position, state, meta, atr_value)
            if state.atr_swing_stop is not None:
                candidates.append((3, float(meta.get('gap_atr', 0.5)), state.atr_swing_stop))

        candidates = [c for c in candidates if improves(c[2], current_stop)]
        if not candidates:
            return
        candidates.sort(key=lambda c: c[2], reverse=(side == 'LONG'))  # найкращий — першим

        async with state.lock:
            position = self.trader.open_positions.get(position_key)
            if not position or not position.get('sl_order_id'):
                return
            current_stop = position.get('stop_loss_price')
            candidates = [c for c in candidates if improves(c[2], current_stop)]
            if not candidates:
                return

            new_index = await self._move_stop_loss(
                position_key, position, state, candidates,
                stage='atr_3step', working_type='MARK_PRICE',
            )
            if new_index is None:
                self._retry_after[position_key] = time.time() + self.move_retry_cooldown_seconds
            else:
                state.last_applied_level_index = max(state.last_applied_level_index, new_index)
                state.fallback_notice_key = None
                state.critical_notice_key = None

    def _schedule_swing_refresh(
        self, position_key: str, position: dict, state: "_TrailState", meta: dict, atr_value: float
    ) -> None:
        """Раз на закриття свічки робочого ТФ оновлює state.atr_swing_stop у
        ФОНОВІЙ задачі (EventBus послідовний — REST у обробнику тіків
        заблокував би всю шину)."""
        try:
            interval = meta['interval']
            interval_ms = interval_to_ms(interval)
        except (KeyError, ValueError):
            if not state.atr_invalid_notice:
                state.atr_invalid_notice = True
                logger.error(f"TrailingStop: {position_key} atr_3step: invalid interval in trail_meta {meta!r}")
            return

        bucket = int(time.time() * 1000) // interval_ms
        if (
            state.atr_candle_bucket == bucket
            or state.atr_refresh_inflight
            or time.time() < state.atr_refresh_retry_after
        ):
            return

        state.atr_refresh_inflight = True
        task = asyncio.create_task(self._refresh_swing_stop(
            position_key, position['symbol'], position['side'], state, meta, atr_value, interval_ms, bucket
        ))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _refresh_swing_stop(
        self, position_key: str, symbol: str, side: str, state: "_TrailState",
        meta: dict, atr_value: float, interval_ms: int, bucket: int,
    ) -> None:
        try:
            lookback = max(1, int(meta.get('lookback', 3)))
            gap = float(meta.get('gap_atr', 0.5)) * atr_value
            bucket_start_ms = bucket * interval_ms

            klines = await self.exchange.get_klines(symbol, meta['interval'], limit=lookback + 3)
            closed = [k for k in klines if int(k['time']) < bucket_start_ms]

            latest_is_fresh = bool(closed) and int(closed[-1]['time']) == bucket_start_ms - interval_ms
            if len(closed) < lookback or not latest_is_fresh:
                # свічка могла ще не долетіти до REST біржі — кілька швидких
                # повторів; якщо так і нема (у свічці не було угод) — беремо,
                # що є, аби не стояти на старому стопі
                state.atr_not_ready_count += 1
                if not closed or state.atr_not_ready_count < 5:
                    state.atr_refresh_retry_after = time.time() + 3.0
                    return

            state.atr_not_ready_count = 0
            window = closed[-lookback:]
            if side == 'LONG':
                state.atr_swing_stop = min(float(k['low']) for k in window) - gap
            else:
                state.atr_swing_stop = max(float(k['high']) for k in window) + gap
            state.atr_candle_bucket = bucket
            logger.debug(
                f"TrailingStop: {position_key} atr_3step swing stop refreshed: "
                f"{state.atr_swing_stop:.6f} (lookback={lookback}, gap={gap:.6f})"
            )
        except Exception as e:
            logger.error(f"TrailingStop: {position_key} failed to refresh swing stop: {e}", exc_info=True)
            state.atr_refresh_retry_after = time.time() + self.move_retry_cooldown_seconds
        finally:
            state.atr_refresh_inflight = False

    # ---------- автоматичний fallback: last positive -> initial ----------

    async def _place_fallback_to_last_stop(self, position_key: str, position: dict) -> bool:
        symbol = position['symbol']
        side = position['side']
        quantity = position.get('remaining_quantity') or position.get('quantity')
        close_side = 'SELL' if side == 'LONG' else 'BUY'
        entry_price = position.get('entry_price')
        leverage = self._safe_leverage(position, position_key)
        # фіксуємо ДО того, як цикл нижче перезапише position['stop_loss_price'] —
        # інакше в trade_analytics old==new на кожному fallback-переносі
        pre_fallback_stop_price = position.get('stop_loss_price')

        state = self._states.setdefault(position_key, _TrailState())

        try:
            entry = float(entry_price) if entry_price is not None else None
        except (TypeError, ValueError):
            entry = None

        candidates = []

        # 1) Тільки останній позитивний SL, який реально був застосований.
        last_positive = state.last_positive_stop_price
        if last_positive is not None and entry:
            try:
                last_positive = float(last_positive)
            except (TypeError, ValueError):
                last_positive = None
            if last_positive is not None and self._price_fraction(entry, side, last_positive) > 0:
                candidates.append(('last_positive', last_positive))

        if not candidates:
            current_stop = position.get('stop_loss_price')
            try:
                current_stop = float(current_stop) if current_stop is not None else None
            except (TypeError, ValueError):
                current_stop = None
            if current_stop is not None and entry and self._price_fraction(entry, side, current_stop) > 0:
                self._track_last_positive(state, entry, side, current_stop, leverage)
                candidates.append(('last_positive', current_stop))

        # 2) Найперший SL — тільки якщо останній позитивний не вдалося створити.
        initial_stop_price = state.initial_stop_price
        if initial_stop_price is None:
            persisted_initial = position.get('initial_stop_loss_price')
            try:
                if persisted_initial is not None:
                    initial_stop_price = float(persisted_initial)
                    state.initial_stop_price = initial_stop_price
            except (TypeError, ValueError):
                initial_stop_price = None

        if initial_stop_price is not None:
            try:
                initial_stop_price = float(initial_stop_price)
                if not candidates or abs(initial_stop_price - candidates[-1][1]) > 1e-12:
                    candidates.append(('initial', initial_stop_price))
            except (TypeError, ValueError):
                initial_stop_price = None

        if not candidates:
            logger.error(
                f"TrailingStop: {position_key} has no valid fallback SL; "
                f"automatic protection cannot be recreated"
            )
            notice_key = f"no_fallback:{position_key}"
            if state.critical_notice_key != notice_key:
                state.critical_notice_key = notice_key
                await self._notify_critical(
                    f"🚨 КРИТИЧНО: не знайдено fallback SL для {symbol} {side}. "
                    f"Система не має ціни для автоматичного захисту."
                )
            return False

        for candidate_name, stop_price in candidates:
            client_order_id = f"sl-fallback-{candidate_name}-{int(time.time() * 1000)}"
            try:
                response = await self.exchange.create_order(
                    symbol=symbol,
                    side=close_side,
                    order_type='STOP_MARKET',
                    quantity=quantity,
                    stop_price=stop_price,
                    position_side=side,
                    close_position=True,
                    client_order_id=client_order_id,
                )
            except Exception as e:
                code = getattr(e, 'code', '?')
                msg = getattr(e, 'msg', str(e))
                logger.error(
                    f"TrailingStop: fallback {candidate_name} failed for {position_key}: "
                    f"{code} {msg}"
                )
                continue

            new_order_id = None
            if response and 'data' in response and 'order' in response['data']:
                new_order_id = response['data']['order'].get('orderId')

            position['stop_loss_price'] = stop_price
            position['sl_order_id'] = str(new_order_id) if new_order_id else None
            position['sl_client_order_id'] = client_order_id
            self._persist_position(position, context="fallback SL")

            stop_roi = self._roi_percent(entry, side, stop_price, leverage) if entry else 0.0

            self._track_last_positive(state, entry, side, stop_price, leverage)

            try:
                self.db.append_sl_move(
                    order_id=position['order_id'],
                    trigger=f"fallback:{candidate_name}",
                    old_stop_price=pre_fallback_stop_price,
                    new_stop_price=stop_price,
                    old_order_id=None,
                    new_order_id=position.get('sl_order_id'),
                    leverage=leverage,
                    roi_percent=stop_roi,
                    reason='fallback',
                )
            except Exception as e:
                logger.error(f"TrailingStop: failed to append fallback sl_move to trade_analytics for {position_key}: {e}", exc_info=True)

            notice_key = f"fallback:{position_key}:{candidate_name}:{stop_price:.12g}"
            if state.fallback_notice_key != notice_key:
                state.fallback_notice_key = notice_key
                state.critical_notice_key = None
                await self._notify_critical(
                    f"⚠️ Автоматично відновлено SL для {symbol} {side}: "
                    f"{candidate_name} = {stop_roi:+.2f}% ROI. Ручне втручання не потрібне."
                )

            logger.warning(
                f"TrailingStop: {position_key} automatic fallback -> "
                f"{candidate_name} SL {stop_roi:+.2f}% ROI (price={stop_price:.10f})"
            )
            return True

        logger.error(f"TrailingStop: ALL automatic fallbacks failed for {position_key}")
        notice_key = f"all_fallbacks_failed:{position_key}"
        if state.critical_notice_key != notice_key:
            state.critical_notice_key = notice_key
            await self._notify_critical(
                f"🚨 КРИТИЧНО: всі автоматичні fallback SL для {symbol} {side} "
                f"не вдалося виставити. Система продовжить автоматичні спроби."
            )
        return False

    # ---------- допоміжне ----------

    @staticmethod
    def _is_gone_error(e: BingXAPIError) -> bool:
        if e.code in _GONE_ERROR_CODES:
            return True
        msg = (e.msg or '').lower()
        return any(s in msg for s in _GONE_ERROR_SUBSTRINGS)

    def _apply_rate_limit_backoff(self, position_key: str, error_msg: Optional[str]) -> None:
        retry_at = self._parse_retry_after(error_msg) or (time.time() + 60.0)
        self._retry_after[position_key] = retry_at

    @staticmethod
    def _parse_retry_after(msg: Optional[str]) -> Optional[float]:
        """Парсить 'can retry after time: 1786999966671' (мс epoch, unix) з
        тіла помилки BingX і повертає unix timestamp у секундах, до якого
        варто утриматись від нових запитів для цього ендпоінту/акаунту."""
        if not msg:
            return None
        m = re.search(r'retry after time:\s*(\d+)', msg)
        if not m:
            return None
        try:
            return int(m.group(1)) / 1000.0
        except ValueError:
            return None

    async def _notify_critical(self, message: str) -> None:
        try:
            await self.event_bus.publish(Event(
                type=EventType.CRITICAL_ERROR,
                data={'context': message, 'error': ''},
            ))
        except Exception as e:
            logger.error(f"TrailingStop: failed to publish critical error notification: {e}")

    # ---------- прибирання стану при закритті позиції ----------

    async def _on_position_closed(self, event: Event) -> None:
        symbol = event.data.get('symbol')
        side = event.data.get('side')
        if not symbol or not side:
            return
        position_key = f"{symbol}_{side}"
        self._states.pop(position_key, None)
        self._retry_after.pop(position_key, None)