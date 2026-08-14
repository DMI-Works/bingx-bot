"""
Керування SL у рантаймі: перенесення стопу в беззбиток + трейлінг за ціною,
щоб зменшувати кількість збиткових угод, які не встигають дійти до тейку.

Логіка порогова, у одиницях R (risk unit):

    R = |entry_price - initial_stop_loss_price| / entry_price   (частка від ціни входу)

Два етапи (обидва рахуються від "сприятливого" руху ціни від входу):

    Stage BREAKEVEN — коли рух у нашу сторону досягає breakeven_trigger_r * R,
        стоп переноситься в entry_price + буфер на комісію/прослизання
        (буфер завжди в напрямку прибутку, тобто чуть краще за чистий беззбиток).

    Stage TRAIL — коли рух досягає trail_trigger_r * R, стоп починає трейлитись
        за найкращою досягнутою ціною (peak) на відстані trail_distance_r * R.

Стоп ніколи не рухається назад (у бік збільшення ризику) — тільки в напрямку
прибутку. Кожне реальне переміщення стопу на біржі — це cancel старого
STOP_MARKET ордера + create нового (BingX не має публічного "amend price"
для swap; є лише окремий cancel і окремий create).

ВАЖЛИВО: цей модуль навмисно НЕ веде власного стану позицій — він читає й
модифікує ТОЙ САМИЙ словник `SimpleTrader.open_positions`, який є єдиним
джерелом правди в рантаймі (sl_order_id, remaining_quantity тощо). Так
уникаємо розсинхрону між кількома незалежними копіями стану, з якого вже
був баг раніше в цьому проєкті.
"""

from __future__ import annotations

import asyncio
import logging
import time
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..events import EventBus, Event, EventType
from ..exchange.bingx_client import BingXAPIError

logger = logging.getLogger(__name__)


@dataclass
class _TrailState:
    """Додаткові поля, яких немає в position_data з SimpleTrader — тримаємо
    окремо по position_key, щоб не засмічувати схему, яку серіалізує/читає
    SimpleTrader/TelegramBot."""
    initial_risk_fraction: float          # R, частка від entry_price
    peak_price: float                     # найкраща досягнута ціна в нашу сторону
    stage: str = "initial"                # "initial" -> "breakeven" -> "trailing"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_move_at: float = 0.0             # time.monotonic(), для троттлінгу API-викликів


class TrailingStopManager:
    def __init__(
        self,
        event_bus: EventBus,
        exchange,          # BingXClient
        db,                 # Database
        trader,              # SimpleTrader — читаємо/пишемо його open_positions напряму
        config: Optional[dict] = None,
    ):
        self.event_bus = event_bus
        self.exchange = exchange
        self.db = db
        self.trader = trader

        cfg = config or {}
        self.enabled: bool = cfg.get('enabled', True)
        # поріг переносу в беззбиток, в одиницях R
        self.breakeven_trigger_r: float = cfg.get('breakeven_trigger_r', 1.0)
        # буфер понад чистий вхід, щоб не зловити мікро-мінус на комісії/проскальзуванні
        self.breakeven_buffer_percent: float = cfg.get('breakeven_buffer_percent', 0.002)  # 0.2%
        # поріг початку трейлінгу, в одиницях R
        self.trail_trigger_r: float = cfg.get('trail_trigger_r', 1.5)
        # відстань трейлінгу від піку, в одиницях R
        self.trail_distance_r: float = cfg.get('trail_distance_r', 1.0)
        # мінімальний зсув стопу, щоб не дьоргати ордер на кожен шум (у частках ціни)
        self.min_move_percent: float = cfg.get('min_move_percent', 0.0008)  # 0.08%
        # не частіше ніж раз на N секунд реально рухаємо ордер на біржі для однієї позиції
        self.min_seconds_between_moves: float = cfg.get('min_seconds_between_moves', 5.0)

        self._states: Dict[str, _TrailState] = {}

        if self.enabled:
            self.event_bus.subscribe(EventType.PRICE_UPDATED, self._on_price_update)
            self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_closed)
            logger.info(
                f"TrailingStopManager initialized: breakeven@{self.breakeven_trigger_r}R, "
                f"trail@{self.trail_trigger_r}R (distance {self.trail_distance_r}R)"
            )
        else:
            logger.info("TrailingStopManager initialized but disabled via config")

    # ---------- вхідна точка: ціна оновилась ----------

    async def _on_price_update(self, event: Event) -> None:
        """
        event.data — той самий формат, що й у BaseStrategy._on_price_update:
        список трейдів з полями 's' (symbol) і 'p' (price) від WS.
        """
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

        # позиція може бути LONG і/або SHORT одночасно (hedge mode) — перевіряємо обидві
        for side in ('LONG', 'SHORT'):
            position_key = f"{symbol}_{side}"
            position = self.trader.open_positions.get(position_key)
            if not position:
                continue
            if not position.get('sl_order_id'):
                # немає активного SL-ордера на біржі — рухати нічого
                continue

            await self._process_position(position_key, position, price)

    async def _process_position(self, position_key: str, position: dict, price: float) -> None:
        entry_price = position.get('entry_price')
        side = position.get('side')
        current_stop = position.get('stop_loss_price')

        if not entry_price or entry_price <= 0 or not current_stop or side not in ('LONG', 'SHORT'):
            return

        state = self._states.get(position_key)
        if state is None:
            initial_r = abs(entry_price - current_stop) / entry_price
            if initial_r <= 0:
                logger.warning(f"TrailingStop: zero/invalid initial R for {position_key}, skipping")
                return
            state = _TrailState(initial_risk_fraction=initial_r, peak_price=entry_price)
            self._states[position_key] = state

        # оновлюємо пік у сприятливий бік
        if side == 'LONG':
            if price > state.peak_price:
                state.peak_price = price
            favorable_fraction = (state.peak_price - entry_price) / entry_price
        else:
            if price < state.peak_price:
                state.peak_price = price
            favorable_fraction = (entry_price - state.peak_price) / entry_price

        if favorable_fraction <= 0:
            return  # ще не в плюсі відносно входу — нічого робити

        r = state.initial_risk_fraction
        favorable_r = favorable_fraction / r

        desired_stop: Optional[float] = None
        new_stage = state.stage

        if favorable_r >= self.trail_trigger_r:
            new_stage = "trailing"
            trail_dist = self.trail_distance_r * r
            if side == 'LONG':
                desired_stop = state.peak_price * (1 - trail_dist)
            else:
                desired_stop = state.peak_price * (1 + trail_dist)

        elif favorable_r >= self.breakeven_trigger_r:
            new_stage = "breakeven"
            if side == 'LONG':
                desired_stop = entry_price * (1 + self.breakeven_buffer_percent)
            else:
                desired_stop = entry_price * (1 - self.breakeven_buffer_percent)

        if desired_stop is None:
            return

        # стоп рухаємо ЛИШЕ в бік прибутку і лише якщо зсув достатньо великий,
        # щоб не дьоргати ордер на кожен тік
        if side == 'LONG':
            improved = desired_stop > current_stop * (1 + self.min_move_percent)
        else:
            improved = desired_stop < current_stop * (1 - self.min_move_percent)

        if not improved:
            return

        state.stage = new_stage

        now = time.monotonic()
        async with state.lock:
            # перечитуємо позицію всередині лока — за час очікування лока
            # позиція могла вже закритись/змінитись
            position = self.trader.open_positions.get(position_key)
            if not position or not position.get('sl_order_id'):
                return
            current_stop = position.get('stop_loss_price')
            if current_stop is None:
                return
            if side == 'LONG' and not (desired_stop > current_stop * (1 + self.min_move_percent)):
                return
            if side == 'SHORT' and not (desired_stop < current_stop * (1 - self.min_move_percent)):
                return
            if now - state.last_move_at < self.min_seconds_between_moves:
                return

            state.last_move_at = now
            await self._move_stop_loss(position_key, position, desired_stop, new_stage)

    # ---------- реальне переміщення SL на біржі ----------

    async def _move_stop_loss(self, position_key: str, position: dict, new_stop_price: float, stage: str) -> None:
        symbol = position['symbol']
        side = position['side']
        old_sl_order_id = position.get('sl_order_id')
        quantity = position.get('remaining_quantity') or position.get('quantity')

        close_side = 'SELL' if side == 'LONG' else 'BUY'
        position_side = side

        # 1) відміняємо старий SL
        try:
            await self.exchange.cancel_order(symbol, old_sl_order_id)
        except BingXAPIError as e:
            if e.code in (100404, 100400, 109400) or 'not exist' in (e.msg or '').lower() or 'not found' in (e.msg or '').lower():
                logger.info(
                    f"TrailingStop: old SL for {position_key} already gone "
                    f"(orderId={old_sl_order_id}), skipping move: {e.code} {e.msg}"
                )
                return
            logger.error(f"TrailingStop: failed to cancel old SL for {position_key}: {e.code} {e.msg}")
            return
        except Exception as e:
            logger.error(f"TrailingStop: unexpected error cancelling old SL for {position_key}: {e}", exc_info=True)
            return

        # 2) створюємо новий SL на новій ціні (та сама схема, що й SimpleTrader._create_stop_loss)
        client_order_id = f"sl-{int(time.time() * 1000)}"
        try:
            response = await self.exchange.create_order(
                symbol=symbol,
                side=close_side,
                order_type='STOP_MARKET',
                quantity=quantity,
                stop_price=new_stop_price,
                position_side=position_side,
                close_position=True,
                client_order_id=client_order_id,
            )
        except BingXAPIError as e:
            logger.error(
                f"TrailingStop: failed to create new SL for {position_key} @ {new_stop_price}: "
                f"{e.code} {e.msg}. POSITION MAY NOW BE WITHOUT A STOP — check manually!"
            )
            await self._notify_critical(
                f"Не вдалося перенести SL для {symbol} {side} на {new_stop_price:.6f} "
                f"(стара позиція, можливо, БЕЗ захисту): {e.code} {e.msg}"
            )
            return
        except Exception as e:
            logger.error(f"TrailingStop: unexpected error creating new SL for {position_key}: {e}", exc_info=True)
            await self._notify_critical(
                f"Не вдалося перенести SL для {symbol} {side} (позиція, можливо, БЕЗ захисту): {e}"
            )
            return

        new_order_id = None
        if 'data' in response and 'order' in response['data']:
            new_order_id = response['data']['order'].get('orderId')

        if not new_order_id:
            logger.error(
                f"TrailingStop: new SL created for {position_key} but no orderId in response: {response}. "
                f"POSITION MAY NOW BE WITHOUT A TRACKED STOP — check manually!"
            )
            await self._notify_critical(
                f"SL для {symbol} {side} перевиставлено, але не вдалось прочитати orderId — перевірте вручну!"
            )

        # 3) оновлюємо СПІЛЬНИЙ стан (той самий словник, що бачить SimpleTrader/TelegramBot)
        old_stop_price = position.get('stop_loss_price')
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
            f"TrailingStop: moved SL for {position_key} -> {new_stop_price:.6f} "
            f"(stage={stage}, old_order={old_sl_order_id}, new_order={new_order_id})"
        )

        try:
            await self.event_bus.publish(Event(
                type=EventType.STOP_LOSS_MOVED,
                data={
                    'symbol': symbol,
                    'side': side,
                    'stage': stage,  # "breakeven" | "trailing"
                    'entry_price': position.get('entry_price'),
                    'old_stop_price': old_stop_price,
                    'new_stop_price': new_stop_price,
                    'strategy': position.get('strategy'),
                },
                source="TrailingStopManager",
            ))
        except Exception as e:
            logger.error(f"TrailingStop: failed to publish STOP_LOSS_MOVED event: {e}")

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