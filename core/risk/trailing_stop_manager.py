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

# Коди помилок BingX, які означають "ордера/позиції більше не існує" —
# ретраїти нема сенсу, просто пропускаємо тик.
_GONE_ERROR_CODES = (100404, 100400, 109400, 109420)
_GONE_ERROR_SUBSTRINGS = ('not exist', 'not found')


@dataclass
class _TrailState:
    """Стан сходинки для однієї позиції (position_key). Живе лише в пам'яті
    цього модуля — єдине джерело правди про сам SL-ордер (id, ціна) лишається
    в SimpleTrader.open_positions (position['sl_order_id'] / ['stop_loss_price']),
    цей модуль лише читає й (при переміщенні) оновлює ці поля."""
    last_applied_level_index: int = -1     # індекс останнього застосованого порогу в trail_levels_percent; -1 = жодного ще не застосовано
    initial_stop_price: Optional[float] = None  # найперший SL позиції; незмінний fallback
    last_positive_stop_price: Optional[float] = None  # останній ФАКТИЧНО застосований SL вище входу
    last_positive_roi_percent: Optional[float] = None  # ROI% цього SL для повідомлень
    fallback_notice_key: Optional[str] = None  # не спамимо одним і тим же fallback на кожному retry
    critical_notice_key: Optional[str] = None  # не спамимо критичною помилкою на кожному retry
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TrailingStopManager:
    def __init__(
        self,
        event_bus: EventBus,
        exchange,          # BingXClient
        db,                 # Database
        trader,              # SimpleTrader — читає й оновлює його open_positions напряму
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

        self.stop_buffer_percent: float = cfg.get('dynamic_stop_buffer_percent', 0.5)  # 0.5% ROI

        self.max_buffer_fraction_of_level: float = cfg.get('max_buffer_fraction_of_level', 0.8)

        self.move_retry_cooldown_seconds: float = cfg.get('move_retry_cooldown_seconds', 15.0)

        self._states: Dict[str, _TrailState] = {}
        self._retry_after: Dict[str, float] = {}

        if self.enabled:
            self.event_bus.subscribe(EventType.PRICE_UPDATED, self._on_price_update)
            self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_closed)
        else:
            logger.info("TrailingStopManager initialized but disabled via config")

    # ---------- вхідна точка: ціна оновилась ----------

    async def _on_price_update(self, event: Event) -> None:
        """
        event.data — той самий формат, що й у BaseStrategy._on_price_update:
        список трейдів з полями 's' (symbol) і 'p' (price) від WS.
        """
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

        # позиція може бути LONG і/або SHORT одночасно (hedge mode) — перевіряємо обидві
        for side in ('LONG', 'SHORT'):
            position_key = f"{symbol}_{side}"
            position = self.trader.open_positions.get(position_key)
            if not position:
                continue
            await self._process_position(position_key, position, price)

    def _highest_reached_level_index(
        self, favorable_fraction: float, last_applied_index: int, leverage: float
    ) -> int:
        """Повертає індекс найвищого ЩЕ НЕ застосованого порогу з
        trail_levels_percent, якого досягла поточна сприятлива зміна ціни
        (favorable_fraction — частка від ціни входу, тобто "price%").
        trail_levels_percent задається в ROI%, тому перед порівнянням
        переводимо поріг у price% діленням на leverage: поріг 2.0% ROI
        при leverage=10 відповідає лише 0.2% руху ціни. Якщо ціна одним
        тіком проскочила відразу кілька порогів (геп/волатильність) —
        повертає найвищий з них, а не перший. Якщо новий поріг не
        досягнуто — повертає last_applied_index без змін.
        """
        target = last_applied_index
        for i, level_roi_percent in enumerate(self.trail_levels_percent):
            if i <= target:
                continue
            level_price_percent = level_roi_percent / leverage
            if favorable_fraction >= level_price_percent / 100.0:
                target = i
        return target

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
            # немає жодного SL на позиції — не наша задача його створювати
            logger.debug(f"TrailingStop: {position_key} SKIP: no sl_order_id on position")
            return

        # trail_levels_percent задано в ROI% (аналогічно SL/TP стратегій),
        # тому для порівняння з favorable_fraction (price%) переводимо
        # через leverage конкретної позиції
        leverage = position.get('leverage') or 1
        try:
            leverage = float(leverage)
        except (TypeError, ValueError):
            logger.warning(
                f"TrailingStop: {position_key} invalid leverage={leverage!r}, falling back to 1x"
            )
            leverage = 1.0
        if leverage <= 0:
            leverage = 1.0

        favorable_fraction = (
            (price - entry_price) / entry_price if side == 'LONG'
            else (entry_price - price) / entry_price
        )
        favorable_roi_percent = favorable_fraction * leverage * 100.0

        state = self._states.setdefault(position_key, _TrailState())

        # Якщо менеджер піднявся вже після відкриття позиції, відновлюємо
        # останній позитивний SL з поточного стану позиції. Після цього всі
        # успішні trailing-перестановки оновлюють це поле явно.
        current_known_stop = position.get('stop_loss_price')
        try:
            current_known_stop = float(current_known_stop) if current_known_stop is not None else None
        except (TypeError, ValueError):
            current_known_stop = None
        if state.last_positive_stop_price is None and current_known_stop is not None:
            is_positive = (
                current_known_stop > float(entry_price) if side == 'LONG'
                else current_known_stop < float(entry_price)
            )
            if is_positive:
                state.last_positive_stop_price = current_known_stop
                state.last_positive_roi_percent = (
                    ((current_known_stop - float(entry_price)) / float(entry_price) if side == 'LONG'
                     else (float(entry_price) - current_known_stop) / float(entry_price))
                    * leverage * 100.0
                )

        # --- єдиний лог "серцебиття" позиції: показує, куди рухається
        # ціна відносно входу та наступного порогу сходинки (в ROI%, щоб
        # легко звіряти з тим, що видно на біржі), щоб було видно, ЧОМУ
        # стоп не рухається (у мінусі / ще не дійшло до порогу / порогів
        # вже нема) без спаму по всіх символах підряд — рядок друкується
        # лише для позицій, що реально відкриті.
        applied_level = (
            self.trail_levels_percent[state.last_applied_level_index]
            if state.last_applied_level_index >= 0 else None
        )
        next_level = (
            self.trail_levels_percent[state.last_applied_level_index + 1]
            if state.last_applied_level_index + 1 < len(self.trail_levels_percent) else None
        )
        current_sl_price = position.get('stop_loss_price')
        if current_sl_price is not None:
            current_sl_fraction = (
                (current_sl_price - entry_price) / entry_price if side == 'LONG'
                else (entry_price - current_sl_price) / entry_price
            )
            current_sl_roi_percent = current_sl_fraction * leverage * 100.0
            current_sl_str = f"{current_sl_fraction:+.3%}price/{current_sl_roi_percent:+.2f}%ROI ({current_sl_price:.6f})"
        else:
            current_sl_str = "None"
        logger.info(
            f"TrailingStop: {position_key} price={price:.6f} entry={entry_price:.6f} "
            f"favorable_price={favorable_fraction:+.3%} favorable_roi={favorable_roi_percent:+.2f}% "
            f"leverage={leverage:g}x applied_level={applied_level}%ROI "
            f"next_level={next_level}%ROI current_sl={current_sl_str}"
        )

        if favorable_fraction <= 0:
            # позиція в мінусі або рівно на вході — НІКОЛИ не рухаємо SL у
            # цей бік, лише вперед, у прибуток
            return

        target_index = self._highest_reached_level_index(
            favorable_fraction, state.last_applied_level_index, leverage
        )
        if target_index <= state.last_applied_level_index:
            return  # жодного нового порогу не досягнуто

        level_roi_percent = self.trail_levels_percent[target_index]
        level_price_percent = level_roi_percent / leverage

        buffer_price_percent = self.stop_buffer_percent / leverage
        max_buffer_price_percent = level_price_percent * self.max_buffer_fraction_of_level
        if buffer_price_percent > max_buffer_price_percent:
            logger.warning(
                f"TrailingStop: {position_key} buffer {buffer_price_percent:.4f}% price would exceed "
                f"{self.max_buffer_fraction_of_level:.0%} of level {level_price_percent:.4f}% price "
                f"(level={level_roi_percent:g}%ROI, leverage={leverage:g}x) — capping buffer to avoid SL landing at entry"
            )
            buffer_price_percent = max_buffer_price_percent

        effective_percent = max(level_price_percent - buffer_price_percent, 0.0)
        desired_stop = (
            entry_price * (1 + effective_percent / 100.0) if side == 'LONG'
            else entry_price * (1 - effective_percent / 100.0)
        )

        async with state.lock:
            # перечитуємо: поки чекали на лок, інший тик міг уже застосувати
            # цей самий або вищий рівень, або позиція могла закритись
            if target_index <= state.last_applied_level_index:
                return
            position = self.trader.open_positions.get(position_key)
            if not position or not position.get('sl_order_id'):
                return

            current_stop = position.get('stop_loss_price')
            if current_stop is not None:
                # SL рухається ЛИШЕ вперед: якщо порахований desired_stop
                # чомусь не кращий за вже виставлений (наприклад, буфер
                # з'їв усю відстань між близькими рівнями) — не чіпаємо
                is_improvement = (
                    desired_stop > current_stop if side == 'LONG'
                    else desired_stop < current_stop
                )
                if not is_improvement:
                    state.last_applied_level_index = target_index  # рівень технічно "пройдено", просто нема чого рухати
                    return

            success = await self._move_stop_loss(position_key, position, desired_stop, level_roi_percent)
            if success:
                state.last_applied_level_index = target_index

                # Зберігаємо саме ФАКТИЧНУ ціну останнього позитивного SL.
                # Це і є перший fallback при наступному невдалому move.
                stop_fraction = (
                    (desired_stop - entry_price) / entry_price if side == 'LONG'
                    else (entry_price - desired_stop) / entry_price
                )
                stop_roi_percent = stop_fraction * leverage * 100.0
                if stop_fraction > 0:
                    state.last_positive_stop_price = desired_stop
                    state.last_positive_roi_percent = stop_roi_percent

                # Новий успішний trailing-рівень = новий цикл повідомлень.
                state.fallback_notice_key = None
                state.critical_notice_key = None
            else:
                # НЕ позначаємо рівень застосованим — спробуємо ще раз
                # пізніше (після короткого кулдауну, щоб не спамити біржу)
                self._retry_after[position_key] = time.time() + self.move_retry_cooldown_seconds

    # ---------- реальне переміщення SL на біржі ----------

    async def _move_stop_loss(
        self, position_key: str, position: dict, desired_stop_price: float, level_roi_percent: float
    ) -> bool:
        """Переставляє єдиний SL позиції (cancel старого + create нового).
        level_roi_percent — тригерний рівень у ROI% (лише для логів/подій,
        сама ціна desired_stop_price вже порахована з урахуванням leverage
        та буфера у _process_position). Повертає True при успіху, False
        при будь-якій невдачі (виклик, що не позначає рівень застосованим
        і намагається ще раз пізніше).

        Якщо старий SL вже відмінено, а новий створити не вдалось —
        автоматично запускається fallback-ланцюжок: останній позитивний SL,
        а якщо він не створюється — найперший SL позиції. Якщо обидва варіанти
        не вдалися, повертається False; наступні тики автоматично повторюють
        спробу після cooldown, без ручного втручання."""
        symbol = position['symbol']
        side = position['side']
        quantity = position.get('remaining_quantity') or position.get('quantity')
        close_side = 'SELL' if side == 'LONG' else 'BUY'
        position_side = side

        old_sl_order_id = position.get('sl_order_id')
        old_stop_price = position.get('stop_loss_price')

        # 1) відміняємо поточний SL
        try:
            await self.exchange.cancel_order(symbol, old_sl_order_id)
        except BingXAPIError as e:
            if e.code == 109429:
                self._apply_rate_limit_backoff(position_key, e.msg)
                logger.warning(f"TrailingStop: rate limited (109429) cancelling SL for {position_key}")
                return False
            if self._is_gone_error(e):
                # ордер/позиція вже не існує на біржі — або позиція щойно
                # закрилась (SL спрацював), або це zombie-позиція в
                # open_positions. У будь-якому разі виставляти новий SL
                # безглуздо — виходимо без помилки/паніки.
                logger.info(
                    f"TrailingStop: {position_key} SL/position already gone "
                    f"(orderId={old_sl_order_id}): {e.code} {e.msg}, skipping"
                )
                return False
            logger.error(f"TrailingStop: failed to cancel SL for {position_key}: {e.code} {e.msg}")
            return False
        except Exception as e:
            logger.error(f"TrailingStop: unexpected error cancelling SL for {position_key}: {e}", exc_info=True)
            return False

        # 2) створюємо новий SL — ОДНА спроба, без перерахунку від live price
        # і без каскаду ретраїв. Якщо не вдалось — одразу fallback на
        # initial_stop_loss_price (найпростіший надійний варіант замість
        # нарощування ще однієї гілки розрахункової логіки).
        client_order_id = f"sl-{int(time.time() * 1000)}"
        try:
            response = await self.exchange.create_order(
                symbol=symbol,
                side=close_side,
                order_type='STOP_MARKET',
                quantity=quantity,
                stop_price=desired_stop_price,
                position_side=position_side,
                close_position=True,
                client_order_id=client_order_id,
            )
        except BingXAPIError as e:
            if e.code == 109429:
                self._apply_rate_limit_backoff(position_key, e.msg)
                logger.error(
                    f"TrailingStop: rate limited (109429) creating new SL for {position_key} "
                    f"AFTER cancelling the old one — falling back to initial stop!"
                )
                return await self._place_fallback_to_last_stop(position_key, position)
            if self._is_gone_error(e):
                # позиція закрилась між cancel і create — нема куди ставити
                # SL, це не помилка
                logger.info(f"TrailingStop: {position_key} position gone before new SL could be created, skipping")
                return False
            if e.code == 110406:
                # "Position SL order already exists" — попри те, що ми щойно
                # відмінили старий, біржа бачить якийсь SL на позиції.
                # Ймовірно, позиція ВСЕ Ж захищена (просто не тим ордером,
                # який ми відстежуємо) — не панікуємо, ресинк наступного тіку.
                logger.warning(
                    f"TrailingStop: {position_key} got 110406 (SL already exists) right after our own "
                    f"cancel — some SL order is present on the exchange, but our tracked id is stale. "
                    f"Skipping this attempt; will resync next tick."
                )
                return False
            logger.error(
                f"TrailingStop: failed to create new SL for {position_key} at {desired_stop_price:.6f} "
                f"(level {level_roi_percent:g}%ROI, old SL already cancelled): {e.code} {e.msg}. "
                f"Falling back to initial strategy stop..."
            )
            await self._notify_critical(
                f"⚠️ Не вдалося перевиставити SL для {symbol} {side} на {desired_stop_price:.6f} "
                f"(рівень {level_roi_percent:g}% ROI, старий SL вже відмінено): {e.code} {e.msg}. "
                f"Повертаю початковий SL стратегії..."
            )
            return await self._place_fallback_to_last_stop(position_key, position)
        except Exception as e:
            logger.error(f"TrailingStop: unexpected error creating new SL for {position_key}: {e}", exc_info=True)
            logger.error(
                f"TrailingStop: falling back to initial strategy stop for {position_key} after unexpected error"
            )
            return await self._place_fallback_to_last_stop(position_key, position)

        new_stop_price = desired_stop_price
        new_order_id = None
        if 'data' in response and 'order' in response['data']:
            new_order_id = response['data']['order'].get('orderId')

        if not new_order_id:
            logger.error(
                f"TrailingStop: SL created for {position_key} but no orderId in response: {response}."
            )
            await self._notify_critical(
                f"SL для {symbol} {side} перевиставлено, але не вдалось прочитати orderId — перевірте вручну!"
            )

        # 3) оновлюємо СПІЛЬНИЙ стан позиції (той самий словник, що бачить
        # SimpleTrader/TelegramBot) — це єдиний SL позиції, інших полів нема
        position['stop_loss_price'] = new_stop_price
        position['sl_order_id'] = str(new_order_id) if new_order_id else None
        position['sl_client_order_id'] = client_order_id

        try:
            self.db.update_position_metadata(
                order_id=position['order_id'],
                metadata=json.dumps(position),
            )
        except Exception as e:
            logger.error(f"TrailingStop: failed to persist moved SL to DB for {position_key}: {e}", exc_info=True)

        logger.info(
            f"TrailingStop: SL for {position_key} -> {new_stop_price:.6f} "
            f"(trigger_level={level_roi_percent:g}%ROI, buffer={self.stop_buffer_percent:g}%price, "
            f"old_order={old_sl_order_id}, new_order={new_order_id})"
        )

        event_leverage = position.get('leverage') or 1
        try:
            event_leverage = float(event_leverage)
        except (TypeError, ValueError):
            event_leverage = 1.0
        if event_leverage <= 0:
            event_leverage = 1.0

        try:
            await self.event_bus.publish(Event(
                type=EventType.STOP_LOSS_MOVED,
                data={
                    'symbol': symbol,
                    'side': side,
                    'stage': f"level_{level_roi_percent:g}pct_roi",
                    'entry_price': position.get('entry_price'),
                    'old_stop_price': old_stop_price,
                    'new_stop_price': new_stop_price,
                    'leverage': event_leverage,
                    'strategy': position.get('strategy'),
                },
                source="TrailingStopManager",
            ))
        except Exception as e:
            logger.error(f"TrailingStop: failed to publish STOP_LOSS_MOVED event: {e}")

        return True

    # ---------- автоматичний fallback: last positive -> initial ----------

    async def _place_fallback_to_last_stop(self, position_key: str, position: dict) -> bool:
        """Автоматичний fallback без ручного втручання.

        Порядок завжди жорсткий:
        1. останній ФАКТИЧНО застосований позитивний SL;
        2. якщо його не вдалося створити — найперший SL позиції.

        Важливо: успішний fallback НЕ позначає trailing-рівень виконаним.
        Тому після відновлення, наприклад, +2.50% ROI бот продовжить
        автоматично намагатися виставити наступний рівень +16% ROI.
        """
        symbol = position['symbol']
        side = position['side']
        quantity = position.get('remaining_quantity') or position.get('quantity')
        close_side = 'SELL' if side == 'LONG' else 'BUY'
        entry_price = position.get('entry_price')

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
                is_positive = (
                    last_positive > entry if side == 'LONG'
                    else last_positive < entry
                )
                if is_positive:
                    candidates.append(('last_positive', last_positive))
            except (TypeError, ValueError):
                pass

        # Сумісність/відновлення для старого стану: якщо окреме поле ще не
        # заповнене, беремо поточний SL, але тільки якщо він реально в плюсі.
        if not candidates:
            current_stop = position.get('stop_loss_price')
            try:
                current_stop = float(current_stop) if current_stop is not None else None
            except (TypeError, ValueError):
                current_stop = None
            if current_stop is not None and entry:
                is_positive = (
                    current_stop > entry if side == 'LONG'
                    else current_stop < entry
                )
                if is_positive:
                    state.last_positive_stop_price = current_stop
                    state.last_positive_roi_percent = (
                        ((current_stop - entry) / entry if side == 'LONG'
                         else (entry - current_stop) / entry) * float(position.get('leverage') or 1) * 100.0
                    )
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

            try:
                self.db.update_position_metadata(
                    order_id=position['order_id'],
                    metadata=json.dumps(position),
                )
            except Exception as e:
                logger.error(
                    f"TrailingStop: failed to persist fallback SL for {position_key}: {e}",
                    exc_info=True,
                )

            # ROI стопа, а не його абсолютна біржова ціна.
            leverage = position.get('leverage') or 1
            try:
                leverage = float(leverage)
            except (TypeError, ValueError):
                leverage = 1.0
            if leverage <= 0:
                leverage = 1.0

            if entry:
                stop_fraction = (
                    (stop_price - entry) / entry if side == 'LONG'
                    else (entry - stop_price) / entry
                )
                stop_roi = stop_fraction * leverage * 100.0
            else:
                stop_roi = 0.0

            # Якщо fallback був позитивним — він залишається нашим last_positive.
            if stop_roi > 0:
                state.last_positive_stop_price = stop_price
                state.last_positive_roi_percent = stop_roi

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

        # Навіть коли fallback повністю провалився, не спамимо однаковою
        # критичною помилкою на кожному наступному retry.
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