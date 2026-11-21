"""
Керування SL у рантаймі: єдиний стоп-лосс позиції переставляється у бік
прибутку і ніколи не рухається назад.

Супровід вмикає сама позиція: trail_meta.mode == 'roi_gap' (кладе
TsMomentumStrategy через сигнал) — див. trailing_roi_gap.py. Позиції без
trail_meta (ручні, старі) і з mode == 'none' не чіпаються.

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
from .trailing_roi_gap import RoiGapTrailMixin, is_roi_gap_mode
from .trailing_fallback import StopFallbackMixin
from .trailing_state import TrailState

logger = logging.getLogger(__name__)


class TrailingStopManager(RoiGapTrailMixin, StopFallbackMixin):
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

        self.market_safety_buffer_price_percent: float = cfg.get('market_safety_buffer_price_percent', 0.05)
        self.move_retry_cooldown_seconds: float = 10.0

        # Комісія однієї сторони (taker) для "безубитку" roi_gap, якщо
        # стратегія не задала breakeven_stop_roi (вхід + вихід).
        self.taker_fee_rate: float = cfg.get('taker_fee_rate', 0.0005)

        self._states: Dict[str, TrailState] = {}
        self._retry_after: Dict[str, float] = {}

        if self.enabled:
            self.event_bus.subscribe(EventType.PRICE_UPDATED, self._on_price_update)
            self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_closed)

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
            # супровід лише для roi_gap; без trail_meta / mode == 'none' —
            # стратегія (або людина) сама керує виходом
            if not is_roi_gap_mode(position):
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

    # ---------- допоміжне для перенесення ----------

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
        state = self._states.setdefault(position_key, TrailState())
        if state.last_positive_stop_price is None:
            self._track_last_positive(state, entry_price, side, position.get('stop_loss_price'), leverage)

        await self._process_roi_gap_position(position_key, position, state, price, leverage)

    # ---------- перенесення SL (cancel -> create) ----------

    async def _move_stop_loss(
        self, position_key: str, position: dict, state: TrailState, candidates: List[tuple],
        stage: Optional[str] = None, working_type: Optional[str] = None,
    ) -> Optional[int]:
        """candidates: [(index, value, stop_price), ...] від найкращого до гіршого
        (value — цільовий ROI, %). Повертає index застосованого кандидата або
        None, якщо стоп не переставлено. working_type прокидається в
        create_order (напр. 'MARK_PRICE')."""
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
                        f"({describe(level_index, level_value)}) — stopping, old SL already cancelled!"
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
        return 0 if ok else None

    async def _publish_stop_moved(
        self, position: dict, stage: str, old_stop_price, new_stop_price: float, leverage: float
    ) -> None:
        # стоп фактично не зрушив (та сама ціна) — не спамимо Telegram
        if old_stop_price and abs(new_stop_price - old_stop_price) <= abs(old_stop_price) * 1e-6:
            return
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
