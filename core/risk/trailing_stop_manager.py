"""
Керування SL у рантаймі: єдиний стоп-лосс позиції переставляється
СХОДИНКАМИ у бік прибутку, поки ціна не досягла чергового порогу — і
ніколи не рухається назад.

Два режими (вибираються по позиції, а не глобально):
  - ladder (за замовчуванням): пороги у % ROI з trail_levels_percent;
  - atr_3step: якщо в position['trail_meta'] mode == 'atr_3step' (його
    кладе TrendSupertrendStrategy через сигнал) — див. trailing_atr.py.

Аварійне відновлення SL (fallback) — у trailing_fallback.py.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from ..common.market_data import parse_price_tick
from ..common.position_math import parse_leverage, position_key as make_position_key, price_fraction, roi_percent
from ..common.position_metadata import persist_position_metadata
from ..events import EventBus, Event, EventType, publish_error
from ..exchange.errors import (
    RATE_LIMIT_CODE,
    SL_ALREADY_EXISTS_CODE,
    BingXAPIError,
    is_gone_error,
    parse_retry_after,
)
from ..exchange.orders import new_client_order_id, place_stop_market
from .trailing_atr import AtrThreeStepMixin, is_atr_mode
from .trailing_fallback import StopFallbackMixin
from .trailing_state import TrailState

logger = logging.getLogger(__name__)


class TrailingStopManager(AtrThreeStepMixin, StopFallbackMixin):
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
        self.market_safety_buffer_price_percent: float = cfg.get('market_safety_buffer_price_percent', 0.05)
        self.move_retry_cooldown_seconds: float = 10.0

        # Комісія однієї сторони (taker) для "безубитку" в режимі atr_3step:
        # SL ставиться на entry ± 2 * taker_fee_rate * entry (вхід + вихід).
        self.taker_fee_rate: float = cfg.get('taker_fee_rate', 0.0005)

        self._bg_tasks: set = set()
        self._states: Dict[str, TrailState] = {}
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
        tick = parse_price_tick(event.data)
        if tick is None:
            return
        symbol, price = tick

        for side in ('LONG', 'SHORT'):
            position_key = make_position_key(symbol, side)
            position = self.trader.open_positions.get(position_key)
            if not position:
                continue
            # ladder-позиції без заданих рівнів не чіпаємо;
            # atr_3step рівнів trail_levels_percent не потребує
            if not self.trail_levels_percent and not is_atr_mode(position):
                continue
            await self._process_position(position_key, position, price)

    # ---------- допоміжне ----------

    @staticmethod
    def _safe_leverage(position: dict, position_key: str = "") -> float:
        leverage = parse_leverage(position.get('leverage') or 1)
        if leverage is None:
            logger.warning(
                f"TrailingStop: {position_key} invalid leverage={position.get('leverage')!r}, falling back to 1x"
            )
            return 1.0
        return leverage

    @staticmethod
    def _track_last_positive(
        state: TrailState, entry_price: float, side: str, stop_price: Optional[float], leverage: float,
    ) -> None:
        if stop_price is None or not entry_price:
            return
        fraction = price_fraction(entry_price, side, stop_price)
        if fraction > 0:
            state.last_positive_stop_price = stop_price
            state.last_positive_roi_percent = fraction * leverage * 100.0

    def _apply_new_stop(
        self, position: dict, stop_price: float, order_id: Optional[str], client_order_id: str, context: str
    ) -> None:
        position['stop_loss_price'] = stop_price
        position['sl_order_id'] = order_id
        position['sl_client_order_id'] = client_order_id
        persist_position_metadata(self.db, position, context=f"TrailingStop: {context}")

    def _record_sl_move(self, position_key: str, position: dict, **move) -> None:
        """Рядок у trade_analytics.sl_moves (аналітика — збій лише логуємо)."""
        try:
            self.db.append_sl_move(
                order_id=position['order_id'],
                new_order_id=position.get('sl_order_id'),
                **move,
            )
        except Exception as e:
            logger.error(f"TrailingStop: failed to append sl_move to trade_analytics for {position_key}: {e}", exc_info=True)

    async def _notify_critical(self, message: str) -> None:
        await publish_error(self.event_bus, context=message, critical=True, source="TrailingStopManager")

    def _apply_rate_limit_backoff(self, position_key: str, error_msg: Optional[str]) -> None:
        self._retry_after[position_key] = parse_retry_after(error_msg) or (time.time() + 60.0)

    # ---------- ladder ----------

    def _highest_reached_level_index(
        self, favorable_fraction: float, last_applied_index: int, leverage: float
    ) -> int:
        target = last_applied_index
        for i, level_roi_percent in enumerate(self.trail_levels_percent):
            if i <= target:
                continue
            if favorable_fraction < level_roi_percent / leverage / 100.0:
                break
            target = i
        return target

    def _stop_price_for_level(
        self, entry_price: float, side: str, level_roi_percent: float, leverage: float
    ) -> float:
        level_price_percent = level_roi_percent / leverage
        buffer_price_percent = min(
            self.stop_buffer_percent / leverage,
            level_price_percent * self.max_buffer_fraction_of_level,
        )
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
            return min(desired_stop_price, current_market_price * (1 - buffer))
        return max(desired_stop_price, current_market_price * (1 + buffer))

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
        favorable_fraction = price_fraction(entry_price, side, price)

        state = self._states.setdefault(position_key, TrailState())
        if state.last_positive_stop_price is None:
            self._track_last_positive(state, entry_price, side, position.get('stop_loss_price'), leverage)

        if is_atr_mode(position):
            await self._process_atr_position(position_key, position, state, price)
            return

        self._log_ladder_status(position_key, position, state, favorable_fraction, leverage)

        if favorable_fraction <= 0:
            return

        target_index = self._highest_reached_level_index(
            favorable_fraction, state.last_applied_level_index, leverage
        )
        if target_index <= state.last_applied_level_index:
            return

        candidates = [
            (idx, self.trail_levels_percent[idx],
             self._stop_price_for_level(entry_price, side, self.trail_levels_percent[idx], leverage))
            for idx in range(target_index, state.last_applied_level_index, -1)
        ]

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
                    state.reset_notices()
            else:
                self._retry_after[position_key] = time.time() + self.move_retry_cooldown_seconds

    def _log_ladder_status(
        self, position_key: str, position: dict, state: TrailState, favorable_fraction: float, leverage: float
    ) -> None:
        if not logger.isEnabledFor(logging.DEBUG):
            return
        current_sl_price = position.get('stop_loss_price')
        sl_roi_str = (
            f"{roi_percent(position['entry_price'], position['side'], current_sl_price, leverage):+.2f}%ROI"
            if current_sl_price is not None else "None"
        )
        next_index = state.last_applied_level_index + 1
        next_level_str = (
            f"{self.trail_levels_percent[next_index]:g}%ROI"
            if next_index < len(self.trail_levels_percent) else "none left"
        )
        logger.debug(
            f"TrailingStop: {position_key} price={favorable_fraction * leverage * 100.0:+.2f}%ROI, "
            f"sl={sl_roi_str}, next_level={next_level_str}"
        )

    # ---------- перенесення SL (cancel -> create) ----------

    async def _move_stop_loss(
        self, position_key: str, position: dict, state: TrailState, candidates: List[tuple],
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
        old_sl_order_id = position.get('sl_order_id')
        old_stop_price = position.get('stop_loss_price')

        try:
            await self.exchange.cancel_order(symbol, old_sl_order_id)
        except BingXAPIError as e:
            if e.code == RATE_LIMIT_CODE:
                self._apply_rate_limit_backoff(position_key, e.msg)
                logger.warning(f"TrailingStop: rate limited ({RATE_LIMIT_CODE}) cancelling SL for {position_key}")
            elif not is_gone_error(e):
                logger.error(f"TrailingStop: failed to cancel SL for {position_key}: {e.code} {e.msg}")
            return None
        except Exception as e:
            logger.error(f"TrailingStop: unexpected error cancelling SL for {position_key}: {e}", exc_info=True)
            return None

        position['sl_order_id'] = None
        persist_position_metadata(self.db, position, context="TrailingStop: cleared sl_order_id after cancel")

        current_market_price: Optional[float] = None
        try:
            current_market_price = await self.exchange.get_mark_price(symbol)
        except Exception as e:
            logger.warning(
                f"TrailingStop: {position_key} failed to fetch fresh mark price before "
                f"placing SL, falling back to tick-derived candidate prices: {e}"
            )

        rate_limited = False
        for level_index, level_value, desired_stop_price in candidates:
            if current_market_price:
                anchored_price = self._anchor_stop_to_market(desired_stop_price, current_market_price, side)
                if old_stop_price is not None:
                    anchored_price = (
                        max(anchored_price, old_stop_price) if side == 'LONG'
                        else min(anchored_price, old_stop_price)
                    )
                if anchored_price != desired_stop_price:
                    logger.info(
                        f"TrailingStop: {position_key} {describe(level_index, level_value)} "
                        f"price anchored to live market {current_market_price:.6f}: "
                        f"{desired_stop_price:.6f} -> {anchored_price:.6f}"
                    )
                desired_stop_price = anchored_price

            client_order_id = new_client_order_id("sl")
            try:
                new_order_id = await place_stop_market(
                    self.exchange,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    stop_price=desired_stop_price,
                    client_order_id=client_order_id,
                    working_type=working_type,
                )
            except BingXAPIError as e:
                if e.code == RATE_LIMIT_CODE:
                    self._apply_rate_limit_backoff(position_key, e.msg)
                    logger.error(
                        f"TrailingStop: rate limited ({RATE_LIMIT_CODE}) creating SL for {position_key} "
                        f"mid-ladder ({describe(level_index, level_value)}) — stopping, old SL already cancelled!"
                    )
                    rate_limited = True
                    break
                if is_gone_error(e):
                    return None
                if e.code == SL_ALREADY_EXISTS_CODE:
                    logger.warning(
                        f"TrailingStop: {position_key} got {SL_ALREADY_EXISTS_CODE} (SL already exists) — "
                        f"some SL order is present on the exchange, resync next tick"
                    )
                    return None
                logger.warning(
                    f"TrailingStop: {position_key} {describe(level_index, level_value)} "
                    f"({desired_stop_price:.6f}) REJECTED: {e.code} {e.msg} — trying next closer level..."
                )
                continue
            except Exception as e:
                logger.error(
                    f"TrailingStop: unexpected error creating SL for {position_key} at "
                    f"{describe(level_index, level_value)}: {e}", exc_info=True
                )
                continue

            if not new_order_id:
                logger.error(f"TrailingStop: SL created for {position_key} but no orderId in response.")
                await self._notify_critical(
                    f"SL для {symbol} {side} перевиставлено, але не вдалось прочитати orderId — перевірте вручну!"
                )

            self._apply_new_stop(position, desired_stop_price, new_order_id, client_order_id, context="moved SL")

            entry_price = position.get('entry_price')
            leverage = self._safe_leverage(position, position_key)
            self._track_last_positive(state, entry_price, side, desired_stop_price, leverage)

            if level_index != candidates[0][0]:
                logger.warning(
                    f"TrailingStop: {position_key} ЦІЛЬОВИЙ рівень ({describe(candidates[0][0], candidates[0][1])}) "
                    f"провалився — застосовано найближчий доступний ({describe(level_index, level_value)})"
                )

            trigger_label = f"{stage}_step_{level_index}" if stage else f"level_{level_value:g}pct_roi"
            await self._publish_stop_moved(position, trigger_label, old_stop_price, desired_stop_price, leverage)
            self._record_sl_move(
                position_key, position,
                trigger=trigger_label,
                old_stop_price=old_stop_price,
                new_stop_price=desired_stop_price,
                old_order_id=old_sl_order_id,
                leverage=leverage,
                roi_percent=roi_percent(entry_price, side, desired_stop_price, leverage) if entry_price else None,
                reason='trail',
            )
            return level_index

        if rate_limited:
            return None

        logger.error(
            f"TrailingStop: ALL {len(candidates)} level candidates failed for {position_key} "
            f"(old SL already cancelled) — escalating to last_positive/initial fallback"
        )
        ok = await self._place_fallback_to_last_stop(position_key, position)
        return state.last_applied_level_index if ok else None

    async def _publish_stop_moved(
        self, position: dict, stage: str, old_stop_price, new_stop_price: float, leverage: float
    ) -> None:
        try:
            await self.event_bus.publish(Event(
                type=EventType.STOP_LOSS_MOVED,
                data={
                    'symbol': position['symbol'],
                    'side': position['side'],
                    'stage': stage,
                    'entry_price': position.get('entry_price'),
                    'old_stop_price': old_stop_price,
                    'new_stop_price': new_stop_price,
                    'leverage': leverage,
                    'strategy': position.get('strategy'),
                },
                source="TrailingStopManager",
            ))
        except Exception as e:
            logger.error(f"TrailingStop: failed to publish STOP_LOSS_MOVED event: {e}")

    # ---------- прибирання стану при закритті позиції ----------

    async def _on_position_closed(self, event: Event) -> None:
        symbol = event.data.get('symbol')
        side = event.data.get('side')
        if not symbol or not side:
            return
        position_key = make_position_key(symbol, side)
        self._states.pop(position_key, None)
        self._retry_after.pop(position_key, None)
