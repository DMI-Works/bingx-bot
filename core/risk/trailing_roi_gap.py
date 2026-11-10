"""
Режим roi_gap TrailingStopManager (його вмикає TsMomentumStrategy через
trail_meta сигналу). Усе — у % ROI від маржі (рух ціни × плече):
  1) ROI >= breakeven_roi -> SL на безубиток із комісіями та запасом на
     проковзування (2 × round-trip комісія, при 10x це +2% ROI);
  2) ROI >= start_roi     -> SL = пік ROI - gap_roi, далі тягнеться за піком.
SL рухається ТІЛЬКИ в бік прибутку і не частіше, ніж раз на step_roi
покращення (щоб не смикати біржу на кожен тік).

На відміну від ROI-лестниці WallBreakout (рівень 6% -> SL на 5.5%, тобто
впритул до ціни) тут між ціною і стопом лишається запас gap_roi: дневна
позиція не вилітає на звичайному шумі, а прибуток усе одно фіксується.
"""

from __future__ import annotations

import logging
import time

from ..common.position_math import roi_percent, side_sign
from .trailing_state import TrailState

logger = logging.getLogger(__name__)

ROI_GAP_MODE = 'roi_gap'

# стоп безубитку = кратне round-trip комісії: рівно 1× не покривав проковзування
# стопу (до 0.05% ціни) і угоди закривались у невеликий мінус
_BREAKEVEN_FEE_MULTIPLE = 2.0
# мінімальне покращення стопу (% ROI) для перестановки ордера
_MIN_STEP_ROI = 5.0
# допуск на похибку float: стоп на безубитку читався як 0.9999999% ROI при
# цілі 1.0% і переставлявся на кожен тік (06.11: 10 000 переносів AVAX)
_ROI_EPS = 0.05
# пауза між перестановками стопу однієї позиції — запобіжник від спаму біржі
_MOVE_COOLDOWN_SECONDS = 30.0


def is_roi_gap_mode(position: dict) -> bool:
    meta = position.get('trail_meta')
    return isinstance(meta, dict) and meta.get('mode') == ROI_GAP_MODE


def roi_gap_target(meta: dict, peak_roi: float, breakeven_sl_roi: float):
    """Цільовий SL у % ROI для досягнутого піку або None (ще рано).
    breakeven_sl_roi — ROI стопу «безубиток + комісії + запас»."""
    breakeven_roi = float(meta.get('breakeven_roi') or 0)
    start_roi = float(meta.get('start_roi') or 0)
    gap_roi = float(meta.get('gap_roi') or 0)

    target = None
    if breakeven_roi > 0 and peak_roi >= breakeven_roi:
        target = breakeven_sl_roi
    if start_roi > 0 and gap_roi > 0 and peak_roi >= start_roi:
        trail = peak_roi - gap_roi
        target = trail if target is None else max(target, trail)
    return target


class RoiGapTrailMixin:
    """Частина TrailingStopManager: використовує його taker_fee_rate,
    _move_stop_loss, _retry_after і move_retry_cooldown_seconds."""

    async def _process_roi_gap_position(
        self, position_key: str, position: dict, state: TrailState, price: float, leverage: float
    ) -> None:
        meta = position['trail_meta']
        entry_price = position['entry_price']
        side = position['side']

        roi_now = roi_percent(entry_price, side, price, leverage)
        if state.peak_roi_percent is None or roi_now > state.peak_roi_percent:
            state.peak_roi_percent = roi_now
        peak = state.peak_roi_percent

        breakeven_sl_roi = _BREAKEVEN_FEE_MULTIPLE * 2.0 * self.taker_fee_rate * leverage * 100.0
        target_roi = roi_gap_target(meta, peak, breakeven_sl_roi)
        if target_roi is None:
            return

        if time.time() - state.roi_gap_last_move_at < _MOVE_COOLDOWN_SECONDS:
            return

        current_stop = position.get('stop_loss_price')
        current_roi = (
            roi_percent(entry_price, side, current_stop, leverage) if current_stop is not None else None
        )
        if current_roi is not None:
            # стоп ще нижче безубитку — переносимо одразу; уже на ньому — лише кроком
            min_step = _MIN_STEP_ROI if current_roi >= breakeven_sl_roi - _ROI_EPS else 0.0
            if target_roi <= current_roi + max(min_step, _ROI_EPS):
                return

        target_price = entry_price * (1 + side_sign(side) * target_roi / 100.0 / leverage)

        async with state.lock:
            position = self.trader.open_positions.get(position_key)
            if not position or not position.get('sl_order_id'):
                return
            logger.info(
                f"TrailingStop: {position_key} roi_gap: ROI {roi_now:+.1f}% (peak {peak:+.1f}%) -> "
                f"SL {current_roi if current_roi is not None else float('nan'):+.1f}% -> {target_roi:+.1f}% ROI"
            )
            state.roi_gap_last_move_at = time.time()
            moved = await self._move_stop_loss(
                position_key, position, state, [(1, target_roi, target_price)], stage=ROI_GAP_MODE,
            )
            if moved is None:
                self._retry_after[position_key] = time.time() + self.move_retry_cooldown_seconds
