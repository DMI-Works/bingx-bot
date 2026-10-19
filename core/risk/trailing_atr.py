"""
Режим atr_3step TrailingStopManager (його вмикає TrendSupertrendStrategy
через trail_meta сигналу). Усе — в кратних ATR, виміряному в момент входу:
  1) профіт >= breakeven_atr*ATR   -> SL = entry ± 2*taker_fee*entry (безубиток з комісіями)
  2) профіт >= lock_trigger_atr*ATR -> SL = entry ± lock_offset_atr*ATR
  3) після кроку 2 -> SL за екстремумом останніх `lookback` ЗАКРИТИХ свічок
     робочого ТФ (Low для LONG / High для SHORT) із зазором gap_atr*ATR.
     Оновлюється раз на закриття свічки.
SL рухається ТІЛЬКИ в бік прибутку; тригер ордера — MARK_PRICE.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from ..common.position_math import side_sign
from ..strategies.indicators import interval_to_ms
from .trailing_state import TrailState

logger = logging.getLogger(__name__)

ATR_MODE = 'atr_3step'


def is_atr_mode(position: dict) -> bool:
    meta = position.get('trail_meta')
    return isinstance(meta, dict) and meta.get('mode') == ATR_MODE


class AtrThreeStepMixin:
    """Частина TrailingStopManager: використовує його exchange, trader,
    taker_fee_rate, _move_stop_loss, _retry_after і _bg_tasks."""

    async def _process_atr_position(
        self, position_key: str, position: dict, state: TrailState, price: float
    ) -> None:
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

        sign = side_sign(side)
        profit = sign * (price - entry_price)
        current_stop = position.get('stop_loss_price')

        breakeven_price = entry_price + sign * entry_price * 2.0 * self.taker_fee_rate
        lock_price = entry_price + sign * lock_offset_atr * atr_value

        def improves(candidate_price: float, stop: Optional[float]) -> bool:
            if stop is None:
                return True
            return candidate_price > stop if side == 'LONG' else candidate_price < stop

        # крок 3 активний, якщо досягнуто тригер кроку 2 АБО стоп уже стоїть
        # не гірше рівня кроку 2 (напр. після рестарту бота)
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
                stage=ATR_MODE, working_type='MARK_PRICE',
            )
            if new_index is None:
                self._retry_after[position_key] = time.time() + self.move_retry_cooldown_seconds
            else:
                state.last_applied_level_index = max(state.last_applied_level_index, new_index)
                state.reset_notices()

    def _schedule_swing_refresh(
        self, position_key: str, position: dict, state: TrailState, meta: dict, atr_value: float
    ) -> None:
        """Раз на закриття свічки робочого ТФ оновлює state.atr_swing_stop у
        ФОНОВІЙ задачі (EventBus послідовний — REST в обробнику тіків
        заблокував би всю шину)."""
        try:
            interval_ms = interval_to_ms(meta['interval'])
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
        self, position_key: str, symbol: str, side: str, state: TrailState,
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
