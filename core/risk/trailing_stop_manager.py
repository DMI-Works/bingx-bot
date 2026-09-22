"""
Керування SL у рантаймі: єдиний стоп-лосс позиції переставляється
СХОДИНКАМИ у бік прибутку, поки ціна не досягла чергового порогу — і
ніколи не рухається назад.

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

        raw_levels = cfg.get('trail_levels_percent', [2.0, 4.0, 8.0, 16.0, 32.0, 64.0])
        self.trail_levels_percent: List[float] = sorted({float(x) for x in raw_levels if x > 0})

        self.stop_buffer_percent: float = cfg.get('dynamic_stop_buffer_percent', 0.5)

        self.max_buffer_fraction_of_level: float = cfg.get('max_buffer_fraction_of_level', 0.8)

        self.market_safety_buffer_price_percent: float = cfg.get(
            'market_safety_buffer_price_percent', 0.05
        )

        self.move_retry_cooldown_seconds: float = 10.0

        # Позиція БЕЗ SL на біржі — найнебезпечніший стан (стоп скасовано, новий
        # ще не стоїть). Повторюємо значно частіше, ніж звичайний retry.
        self.unprotected_retry_seconds: float = float(cfg.get('unprotected_retry_seconds', 3.0))

        # Hard-stop watchdog: незалежна від біржових умовних ордерів перевірка на
        # кожному тіку. Якщо ціна пройшла рівень стопа на hard_stop_overshoot_percent
        # (у % ціни), а позиція досі відкрита — стоп не спрацював (збій біржі,
        # відхилений ордер, вікно cancel->create) і позицію закриваємо по ринку.
        # Якщо SL на біржі взагалі немає — достатньо будь-якого перетину рівня.
        self.hard_stop_enabled: bool = bool(cfg.get('hard_stop_enabled', True))
        self.hard_stop_overshoot_percent: float = float(cfg.get('hard_stop_overshoot_percent', 0.3))
        self.force_close_cooldown_seconds: float = 5.0
        self._force_close_after: Dict[str, float] = {}

        self._states: Dict[str, _TrailState] = {}
        self._retry_after: Dict[str, float] = {}

        if self.enabled:
            self.event_bus.subscribe(EventType.PRICE_UPDATED, self._on_price_update)
            self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_closed)

    async def _on_price_update(self, event: Event) -> None:
        if not self.enabled or not self.trail_levels_percent:
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

            entry_price = position.get('entry_price')
            if entry_price and entry_price > 0:
                self._track_excursion(position, entry_price, side, price)

            if await self._guard_stop_breach(position_key, position, price):
                continue

            await self._process_position(position_key, position, price)

    def _track_excursion(self, position: dict, entry_price: float, side: str, price: float) -> None:
        """MFE/MAE в %ROI за весь час життя позиції — дає відповідь, куди йшла
        ціна ДО вибивання (чи доходили угоди хоч до першого рівня трейлінгу)."""
        leverage = self._safe_leverage(position)
        roi = self._roi_percent(entry_price, side, price, leverage)
        if roi > (position.get('max_favorable_roi') or 0.0):
            position['max_favorable_roi'] = round(roi, 2)
        if roi < (position.get('max_adverse_roi') or 0.0):
            position['max_adverse_roi'] = round(roi, 2)

    async def _guard_stop_breach(self, position_key: str, position: dict, price: float) -> bool:
        """
        Повертає True, якщо позиція "оброблена" сторожем (аварійне закриття
        надіслано або триває кулдаун) — тоді звичайну логіку трейлінгу
        для цього тіку пропускаємо.
        """
        if not self.hard_stop_enabled or position.get('sl_pending'):
            return False

        side = position.get('side')
        stop = position.get('stop_loss_price')
        if not stop or side not in ('LONG', 'SHORT'):
            return False
        try:
            stop = float(stop)
        except (TypeError, ValueError):
            return False
        if stop <= 0:
            return False

        overshoot_pct = ((stop - price) if side == 'LONG' else (price - stop)) / stop * 100.0
        has_exchange_sl = bool(position.get('sl_order_id'))
        threshold = self.hard_stop_overshoot_percent if has_exchange_sl else 0.0
        if overshoot_pct <= threshold:
            return False

        now = time.time()
        if now < self._force_close_after.get(position_key, 0.0):
            return True
        self._force_close_after[position_key] = now + self.force_close_cooldown_seconds

        quantity = position.get('remaining_quantity') or position.get('quantity')
        if not quantity:
            return False

        reason = (
            f"hard-stop: ціна {price} пройшла стоп {stop} на {overshoot_pct:.3f}% "
            f"({'біржовий SL не спрацював' if has_exchange_sl else 'SL на біржі відсутній'})"
        )
        logger.error(f"TrailingStop: {position_key} {reason} — закриваю по ринку")
        await self.trader.emergency_close_position(position['symbol'], side, quantity, reason)
        return True

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
        if position.get('sl_pending'):
            # SimpleTrader.open_position ще ставить початковий SL — не втручаємось
            return

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
            self._retry_after[position_key] = time.time() + self.unprotected_retry_seconds
            return

        leverage = self._safe_leverage(position, position_key)

        favorable_fraction = self._price_fraction(entry_price, side, price)
        favorable_roi_percent = favorable_fraction * leverage * 100.0

        state = self._states.setdefault(position_key, _TrailState())

        if state.last_positive_stop_price is None:
            self._track_last_positive(state, entry_price, side, position.get('stop_loss_price'), leverage)

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
        self, position_key: str, position: dict, state: "_TrailState", candidates: List[tuple]
    ) -> Optional[int]:
        symbol = position['symbol']
        side = position['side']
        quantity = position.get('remaining_quantity') or position.get('quantity')
        close_side = 'SELL' if side == 'LONG' else 'BUY'

        old_sl_order_id = position.get('sl_order_id')
        old_stop_price = position.get('stop_loss_price')

        # Позначаємо: старий SL ЩЕ живий, поки не підтверджено новий — інакше
        # hard-stop watchdog (TrailingStopManager._guard_stop_breach) вважатиме
        # позицію незахищеною в цьому короткому вікні між cancel і create.
        position['sl_pending'] = True

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

        rate_limited = False
        placed_response = None
        placed_level_index = placed_level_roi = placed_stop_price = None

        # На кожен кандидат — до CANDIDATE_ATTEMPTS спроб зі СВІЖОЮ mark price
        # (раніше ціна бралась ОДИН раз на всю драбинку рівнів, тому "хвіст"
        # кандидатів міг валитись через застарілу anchor-ціну навіть коли ринок
        # давно пішов далі й дозволяв би валідний стоп).
        CANDIDATE_ATTEMPTS = 2
        for level_index, level_roi_percent, desired_stop_price in candidates:
            for attempt in range(1, CANDIDATE_ATTEMPTS + 1):
                current_market_price: Optional[float] = None
                try:
                    current_market_price = await self.exchange.get_mark_price(symbol)
                except Exception as e:
                    logger.warning(
                        f"TrailingStop: {position_key} failed to fetch fresh mark price before "
                        f"placing SL, falling back to tick-derived candidate price: {e}"
                    )

                attempt_stop_price = desired_stop_price
                if current_market_price:
                    anchored_price = self._anchor_stop_to_market(
                        attempt_stop_price, current_market_price, side
                    )
                    if old_stop_price is not None:
                        anchored_price = (
                            max(anchored_price, old_stop_price) if side == 'LONG'
                            else min(anchored_price, old_stop_price)
                        )
                    if anchored_price != attempt_stop_price:
                        logger.info(
                            f"TrailingStop: {position_key} level {level_roi_percent:g}%ROI "
                            f"price anchored to live market {current_market_price:.6f}: "
                            f"{attempt_stop_price:.6f} -> {anchored_price:.6f}"
                        )
                    attempt_stop_price = anchored_price

                client_order_id = f"sl-{int(time.time() * 1000)}"
                try:
                    response = await self.exchange.create_order(
                        symbol=symbol,
                        side=close_side,
                        order_type='STOP_MARKET',
                        quantity=quantity,
                        stop_price=attempt_stop_price,
                        position_side=side,
                        close_position=True,
                        client_order_id=client_order_id,
                    )
                except BingXAPIError as e:
                    if e.code == 109429:
                        self._apply_rate_limit_backoff(position_key, e.msg)
                        logger.error(
                            f"TrailingStop: rate limited (109429) creating SL for {position_key} "
                            f"mid-ladder (level {level_roi_percent:g}%ROI) — stopping, old SL already cancelled!"
                        )
                        rate_limited = True
                        break
                    if self._is_gone_error(e):
                        position['sl_pending'] = False
                        return None
                    if e.code == 110406:
                        logger.warning(
                            f"TrailingStop: {position_key} got 110406 (SL already exists) — some SL order "
                            f"is present on the exchange, resync next tick"
                        )
                        position['sl_pending'] = False
                        return None
                    logger.warning(
                        f"TrailingStop: {position_key} level {level_roi_percent:g}%ROI "
                        f"({attempt_stop_price:.6f}) REJECTED attempt {attempt}/{CANDIDATE_ATTEMPTS}: "
                        f"{e.code} {e.msg}"
                    )
                    continue
                except Exception as e:
                    logger.error(
                        f"TrailingStop: unexpected error creating SL for {position_key} at "
                        f"level {level_roi_percent:g}%ROI (attempt {attempt}/{CANDIDATE_ATTEMPTS}): {e}",
                        exc_info=True
                    )
                    continue

                placed_response = response
                placed_level_index, placed_level_roi, placed_stop_price = (
                    level_index, level_roi_percent, attempt_stop_price
                )
                break  # успіх — далі не пробуємо ні цей рівень, ні слабші

            if rate_limited or placed_response is not None:
                break

        if rate_limited:
            position['sl_pending'] = False
            return None

        if placed_response is not None:
            response = placed_response
            level_index, level_roi_percent, desired_stop_price = (
                placed_level_index, placed_level_roi, placed_stop_price
            )

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
                    f"TrailingStop: {position_key} ЦІЛЬОВИЙ рівень {candidates[0][1]:g}%ROI "
                    f"провалився — застосовано найближчий доступний {level_roi_percent:g}%ROI"
                )

            try:
                await self.event_bus.publish(Event(
                    type=EventType.STOP_LOSS_MOVED,
                    data={
                        'symbol': symbol,
                        'side': side,
                        'stage': f"level_{level_roi_percent:g}pct_roi",
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

            position['sl_pending'] = False
            return level_index

        logger.error(
            f"TrailingStop: ALL {len(candidates)} level candidates failed for {position_key} "
            f"(old SL already cancelled, old_stop_price={old_stop_price}) — "
            f"escalating to fallback (never worse than old_stop_price)"
        )
        ok = await self._place_fallback_to_last_stop(position_key, position, floor_stop_price=old_stop_price)
        position['sl_pending'] = False
        return state.last_applied_level_index if ok else None

    # ---------- автоматичний fallback: last positive -> current(cancelled) -> initial ----------

    async def _place_fallback_to_last_stop(
        self, position_key: str, position: dict, floor_stop_price: Optional[float] = None
    ) -> bool:
        """
        floor_stop_price — якщо задано (переданий зі _move_stop_loss як щойно
        СКАСОВАНИЙ стоп), ГАРАНТУЄ, що новий SL не буде виставлено ГІРШЕ за
        нього. Це закриває баг: раніше при провалі ВСІХ кандидатів рівнів
        драбинки на ПЕРШІЙ спробі трейлінгу (коли last_positive_stop_price ще
        порожній) фолбек стрибав одразу до initial_stop_loss_price — тобто
        відкочував захист назад до ПОЧАТКОВОГО -stop_loss_percent%ROI навіть
        якщо ціна вже давно пройшла кілька рівнів драбинки вгору.
        """
        symbol = position['symbol']
        side = position['side']
        quantity = position.get('remaining_quantity') or position.get('quantity')
        close_side = 'SELL' if side == 'LONG' else 'BUY'
        entry_price = position.get('entry_price')
        leverage = self._safe_leverage(position, position_key)

        state = self._states.setdefault(position_key, _TrailState())

        try:
            entry = float(entry_price) if entry_price is not None else None
        except (TypeError, ValueError):
            entry = None

        candidates = []

        def _is_at_least_as_good(price: float, floor_price: Optional[float]) -> bool:
            """price не гірший за floor_price (для LONG: не нижче; для SHORT: не вище).
            Якщо floor_price немає — будь-яка ціна прохідна."""
            if floor_price is None:
                return True
            return price >= floor_price if side == 'LONG' else price <= floor_price

        # 0) Найвищий пріоритет: ЩОЙНО СКАСОВАНИЙ стоп (floor_stop_price). Його
        # відтворення НІКОЛИ не є регресом — це рівно те, що вже захищало
        # позицію секунду тому. Раніше цей кандидат був відсутній (перевірка
        # "> 0" відсіювала його, якщо стоп ще не в плюсі), і фолбек з ПЕРШОЇ ж
        # невдалої спроби трейлінгу стрибав одразу на initial_stop_loss_price,
        # відкочуючи захист до -stop_loss_percent%ROI незалежно від того,
        # скільки рівнів драбинки ціна вже пройшла.
        floor_price: Optional[float] = None
        if floor_stop_price is not None:
            try:
                floor_price = float(floor_stop_price)
                candidates.append(('current', floor_price))
            except (TypeError, ValueError):
                floor_price = None

        # 1) Останній позитивний SL, який реально був застосований — якщо він
        # не гірший за floor (звичайно так і є: last_positive оновлюється
        # лише при УСПІШНИХ рухах, тобто >= floor за побудовою).
        last_positive = state.last_positive_stop_price
        if last_positive is not None:
            try:
                last_positive = float(last_positive)
            except (TypeError, ValueError):
                last_positive = None
            if last_positive is not None and _is_at_least_as_good(last_positive, floor_price):
                candidates.append(('last_positive', last_positive))

        if not candidates and entry:
            current_stop = position.get('stop_loss_price')
            try:
                current_stop = float(current_stop) if current_stop is not None else None
            except (TypeError, ValueError):
                current_stop = None
            if current_stop is not None and self._price_fraction(entry, side, current_stop) > 0:
                self._track_last_positive(state, entry, side, current_stop, leverage)
                candidates.append(('last_positive', current_stop))

        # 2) Найперший SL зі входу — лише якщо він НЕ ГІРШИЙ за floor. Якщо
        # floor кращий (позиція вже встигла кудись просунутись), initial сюди
        # взагалі не потрапляє — інакше саме це й було б регресом захисту.
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
                already_present = any(abs(initial_stop_price - c[1]) < 1e-12 for c in candidates)
                if not already_present and _is_at_least_as_good(initial_stop_price, floor_price):
                    candidates.append(('initial', initial_stop_price))
            except (TypeError, ValueError):
                initial_stop_price = None

        # Кандидати впорядковуємо від НАЙКРАЩОГО (найдальше в прибуток) до
        # найгіршого — щоб пробувати спершу кращий, а не перший-як-влучилось.
        candidates.sort(key=lambda c: c[1], reverse=(side == 'LONG'))

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
        self._force_close_after.pop(position_key, None)