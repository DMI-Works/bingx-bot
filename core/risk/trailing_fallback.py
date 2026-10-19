"""
Аварійне відновлення SL TrailingStopManager: якщо старий стоп уже скасовано,
а новий не вдалось виставити (або позиція взагалі без SL), ставимо:
  1) останній позитивний SL, що реально стояв на біржі;
  2) інакше — найперший SL стратегії (initial_stop_loss_price).
"""

from __future__ import annotations

import logging

from ..common.position_math import price_fraction, roi_percent
from ..exchange.orders import new_client_order_id, place_stop_market
from .trailing_state import TrailState

logger = logging.getLogger(__name__)


def _to_float(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class StopFallbackMixin:
    """Частина TrailingStopManager: використовує його exchange, _states,
    _safe_leverage, _track_last_positive, _apply_new_stop, _record_sl_move
    і _notify_critical."""

    def _fallback_candidates(self, position: dict, state: TrailState, entry, side: str, leverage: float) -> list:
        candidates = []

        # 1) Тільки останній позитивний SL, який реально був застосований.
        last_positive = _to_float(state.last_positive_stop_price)
        if last_positive is not None and entry and price_fraction(entry, side, last_positive) > 0:
            candidates.append(('last_positive', last_positive))

        if not candidates:
            current_stop = _to_float(position.get('stop_loss_price'))
            if current_stop is not None and entry and price_fraction(entry, side, current_stop) > 0:
                self._track_last_positive(state, entry, side, current_stop, leverage)
                candidates.append(('last_positive', current_stop))

        # 2) Найперший SL — тільки якщо останній позитивний не вдалося створити.
        if state.initial_stop_price is None:
            state.initial_stop_price = _to_float(position.get('initial_stop_loss_price'))
        initial_stop_price = _to_float(state.initial_stop_price)
        if initial_stop_price is not None:
            if not candidates or abs(initial_stop_price - candidates[-1][1]) > 1e-12:
                candidates.append(('initial', initial_stop_price))

        return candidates

    async def _notify_once(self, state: TrailState, notice_key: str, message: str) -> None:
        if state.critical_notice_key != notice_key:
            state.critical_notice_key = notice_key
            await self._notify_critical(message)

    async def _place_fallback_to_last_stop(self, position_key: str, position: dict) -> bool:
        symbol = position['symbol']
        side = position['side']
        quantity = position.get('remaining_quantity') or position.get('quantity')
        leverage = self._safe_leverage(position, position_key)
        # фіксуємо ДО того, як цикл нижче перезапише position['stop_loss_price'] —
        # інакше в trade_analytics old==new на кожному fallback-переносі
        pre_fallback_stop_price = position.get('stop_loss_price')

        state = self._states.setdefault(position_key, TrailState())
        entry = _to_float(position.get('entry_price'))

        candidates = self._fallback_candidates(position, state, entry, side, leverage)
        if not candidates:
            logger.error(
                f"TrailingStop: {position_key} has no valid fallback SL; "
                f"automatic protection cannot be recreated"
            )
            await self._notify_once(
                state, f"no_fallback:{position_key}",
                f"🚨 КРИТИЧНО: не знайдено fallback SL для {symbol} {side}. "
                f"Система не має ціни для автоматичного захисту."
            )
            return False

        for candidate_name, stop_price in candidates:
            client_order_id = new_client_order_id(f"sl-fallback-{candidate_name}")
            try:
                new_order_id = await place_stop_market(
                    self.exchange,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    stop_price=stop_price,
                    client_order_id=client_order_id,
                )
            except Exception as e:
                logger.error(
                    f"TrailingStop: fallback {candidate_name} failed for {position_key}: "
                    f"{getattr(e, 'code', '?')} {getattr(e, 'msg', str(e))}"
                )
                continue

            self._apply_new_stop(position, stop_price, new_order_id, client_order_id, context="fallback SL")
            stop_roi = roi_percent(entry, side, stop_price, leverage) if entry else 0.0
            self._track_last_positive(state, entry, side, stop_price, leverage)
            self._record_sl_move(
                position_key, position,
                trigger=f"fallback:{candidate_name}",
                old_stop_price=pre_fallback_stop_price,
                new_stop_price=stop_price,
                old_order_id=None,
                leverage=leverage,
                roi_percent=stop_roi,
                reason='fallback',
            )

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
        await self._notify_once(
            state, f"all_fallbacks_failed:{position_key}",
            f"🚨 КРИТИЧНО: всі автоматичні fallback SL для {symbol} {side} "
            f"не вдалося виставити. Система продовжить автоматичні спроби."
        )
        return False
