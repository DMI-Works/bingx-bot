import asyncio
import logging
import time
from typing import Optional, Dict, Any
from datetime import datetime
import json

from ..exchange import BingXClient
from ..exchange.bingx_client import BingXAPIError
from ..events import EventBus, Event, EventType
from ..risk import RiskManager
from ..database import Database


logger = logging.getLogger(__name__)


class SimpleTrader:
    """Простий обробник торгових сигналів - відкриває/закриває позиції"""

    def __init__(
        self,
        exchange: BingXClient,
        event_bus: EventBus,
        db: Database,
        risk_manager: Optional[RiskManager] = None,
    ):
        self.exchange = exchange
        self.event_bus = event_bus
        self.db = db
        self.risk_manager = risk_manager

        self.open_positions = {}

        # Підписуємось на сигнали від стратегій
        self.event_bus.subscribe(EventType.SIGNAL_GENERATED, self._handle_signal)

        # Підписуємось на оновлення ордерів з WebSocket
        self.event_bus.subscribe(EventType.ORDER_FILLED, self._handle_order_update)

        self.event_bus.subscribe(EventType.BALANCE_UPDATED, self._handle_account_update)

        # Відновлюємо активні позиції з БД при старті (переживають рестарт бота)
        self._restore_open_positions()

    def _restore_open_positions(self) -> None:
        """Підтягуємо з БД позиції, які залишились OPEN з попереднього запуску"""
        try:
            rows = self.db.get_active_positions()
            for row in rows:
                position_key = f"{row['symbol']}_{row['side']}"
                metadata = {}
                try:
                    metadata = json.loads(row['metadata']) if row['metadata'] else {}
                except (TypeError, ValueError):
                    metadata = {}

                self.open_positions[position_key] = {
                    'order_id': row['order_id'],
                    'symbol': row['symbol'],
                    'side': row['side'],
                    'quantity': metadata.get('quantity', 0),
                    'entry_price': metadata.get('entry_price', 0.0),
                    'leverage': metadata.get('leverage', 10),
                    'stop_loss_price': metadata.get('stop_loss_price'),
                    'take_profit_levels': metadata.get('take_profit_levels'),
                    'opened_by': metadata.get('opened_by', 'bot'),
                    'sl_order_id': metadata.get('sl_order_id'),
                    'tp_order_ids': metadata.get('tp_order_ids', []),
                    'sl_client_order_id': metadata.get('sl_client_order_id'),
                    'tp_client_order_ids': metadata.get('tp_client_order_ids', []),
                    'strategy': metadata.get('strategy'),
                    'realized_pnl_accum': metadata.get('realized_pnl_accum', 0.0),
                    'commission_accum': metadata.get('commission_accum', 0.0),
                    'remaining_quantity': metadata.get('remaining_quantity'),
                    'closing_trade_ids': metadata.get('closing_trade_ids', []),
                    'closing_orders': metadata.get('closing_orders', []),
                }

            if rows:
                logger.info(f"Restored {len(rows)} open positions from DB")
        except Exception as e:
            logger.error(f"Failed to restore open positions from DB: {e}", exc_info=True)

    async def _notify_error(self, error: str, context: str, critical: bool = False) -> None:
        """
        Публікує подію ERROR/CRITICAL_ERROR в event_bus — TelegramBot вже
        підписаний на обидва типи (_on_error/_on_critical_error) і надішле
        коротке повідомлення в чат. Раніше ці обробники існували, але їх
        ніхто не викликав — помилки залишались тільки в лог-файлі.
        """
        try:
            await self.event_bus.publish(Event(
                type=EventType.CRITICAL_ERROR if critical else EventType.ERROR,
                data={'error': error, 'context': context}
            ))
        except Exception as e:
            logger.error(f"Failed to publish error notification ({context}): {e}")

    async def _handle_signal(self, event: Event) -> None:
        """Обробка сигналу від стратегії"""
        signal = event.data
        action = signal.get('action')

        if action == 'OPEN':
            await self.open_position(
                symbol=signal['symbol'],
                side=signal['side'],
                quantity=signal['quantity'],
                leverage=signal.get('leverage', 10),
                stop_loss_price=signal.get('stop_loss_price'),
                take_profit_levels=signal.get('take_profit_levels'),
                strategy=signal.get('strategy'),
                # ціна, від якої стратегія рахувала % для SL/TP — потрібна,
                # щоб перерахувати рівні під фактичну ціну виконання ринкового ордера
                reference_price=signal.get('reference_price')
            )

    @staticmethod
    def _parse_fill(order_info: Optional[Dict[str, Any]]):
        """Безпечно витягує (status, avg_price, executed_qty) з об'єкта ордера біржі."""
        if not order_info:
            return None, 0.0, 0.0

        status = order_info.get('status')
        try:
            avg_price = float(order_info.get('avgPrice', 0) or 0)
        except (TypeError, ValueError):
            avg_price = 0.0
        try:
            executed_qty = float(order_info.get('executedQty', 0) or 0)
        except (TypeError, ValueError):
            executed_qty = 0.0

        return status, avg_price, executed_qty

    async def _wait_for_confirmed_fill(
        self,
        symbol: str,
        order_id: str,
        max_attempts: int = 6,
        delay_seconds: float = 0.3
    ) -> Optional[Dict[str, Any]]:
        """
        Для MARKET-ордера синхронна відповідь create_order часто повертає
        avgPrice=0 / executedQty=0 — фактичне виконання ще не встигло
        долетіти до REST-шару біржі. Тому запитуємо ордер окремо через
        GET /trade/order, поки не отримаємо статус FILLED з реальними
        avgPrice/executedQty (або не вичерпаємо спроби).
        """
        last_order_info = None

        for attempt in range(1, max_attempts + 1):
            try:
                order_info = await self.exchange.get_order(symbol, order_id)
            except Exception as e:
                logger.warning(f"get_order attempt {attempt} failed for {symbol} order {order_id}: {e}")
                order_info = None

            if order_info:
                last_order_info = order_info
                if attempt == 1:
                    logger.debug(f"Raw get_order response for {symbol} order {order_id}: {order_info}")
                status, avg_price, executed_qty = self._parse_fill(order_info)

                if status == 'FILLED' and avg_price > 0 and executed_qty > 0:
                    logger.info(
                        f"Confirmed fill for {symbol} order {order_id} on attempt {attempt}: "
                        f"avgPrice={avg_price}, executedQty={executed_qty}"
                    )
                    return order_info

            if attempt < max_attempts:
                await asyncio.sleep(delay_seconds)

        logger.warning(
            f"Could not confirm fill for {symbol} order {order_id} after {max_attempts} attempts "
            f"(last seen: {last_order_info}). Falling back to whatever data is available."
        )
        return last_order_info

    async def _fetch_entry_from_positions(self, symbol: str, side: str) -> Optional[Dict[str, float]]:
        """
        Fallback-джерело реальної ціни входу: не статус ордера, а фактичний
        стан позиції на біржі (entryPrice/positionAmt). Використовується,
        коли get_order() за відведені спроби так і не показав FILLED —
        позиція вже може бути фізично відкрита, навіть якщо статус ордера
        це ще не відображає.
        """
        try:
            positions = await self.exchange.get_positions()
        except Exception as e:
            logger.warning(f"Failed to fetch positions as entry_price fallback for {symbol}: {e}")
            return None

        position_side = 'LONG' if side == 'LONG' else 'SHORT'
        for pos in positions:
            if pos.get('symbol') != symbol:
                continue
            if pos.get('positionSide') != position_side:
                continue
            try:
                entry_price = float(pos.get('entryPrice', 0) or 0)
                position_amt = abs(float(pos.get('positionAmt', 0) or 0))
            except (TypeError, ValueError):
                continue
            if entry_price > 0 and position_amt > 0:
                logger.info(
                    f"Fallback entry_price for {symbol} {side} from positions endpoint: "
                    f"entryPrice={entry_price}, positionAmt={position_amt}"
                )
                return {'entry_price': entry_price, 'quantity': position_amt}

        return None

    async def open_position(
        self,
        symbol: str,
        side: str,
        quantity: float,
        leverage: int = 10,
        stop_loss_price: Optional[float] = None,
        take_profit_levels: Optional[list] = None,
        strategy: Optional[str] = None,
        reference_price: Optional[float] = None
    ) -> bool:
        try:
            positions_info_message = None
            if self.risk_manager:
                can_open, reason = await self.risk_manager.can_open_position(symbol)
                positions_info_message = reason if reason else "Позицію відкрито."

                if not can_open:
                    logger.warning(f"Risk manager blocked {symbol} {side}: {reason}")
                    return False
            else:
                positions_info_message = "Позицію відкрито."

            if strategy:
                positions_info_message = f"{positions_info_message} | Стратегія: {strategy}"

            logger.info(f"Opening position: {symbol} {side} {quantity} (strategy={strategy})")

            try:
                await self.exchange.set_leverage(symbol, leverage, side=side)
            except BingXAPIError as e:
                logger.warning(
                    f"set_leverage returned an error for {symbol} (leverage={leverage}, side={side}): "
                    f"{e.code} {e.msg}. Continuing with order placement anyway."
                )

            order_side = 'BUY' if side == 'LONG' else 'SELL'

            try:
                exchange_order = await self.exchange.create_order(
                    symbol=symbol,
                    side=order_side,
                    order_type='MARKET',
                    quantity=quantity
                )
            except BingXAPIError as e:
                if e.code == 109400 and 'temporarily disabled' in e.msg:
                    # це обмеження біржі через волатильність, а не баг —
                    # не спамимо в Telegram, лога достатньо
                    logger.warning(
                        f"Order rejected by exchange (API orders temporarily disabled) "
                        f"for {symbol} {side}: {e.msg}"
                    )
                else:
                    logger.error(f"Order rejected by exchange for {symbol} {side}: {e.code} {e.msg}")
                    await self._notify_error(
                        error=f"{e.code} {e.msg}",
                        context=f"Не вдалося відкрити позицію {symbol} {side}"
                    )
                return False

            logger.info(f"Exchange order response: {exchange_order}")

            order_id = None
            if 'data' in exchange_order and 'order' in exchange_order['data']:
                order_id = exchange_order['data']['order'].get('orderId')
            elif 'orderId' in exchange_order:
                order_id = exchange_order.get('orderId')
            elif 'data' in exchange_order and 'orderId' in exchange_order['data']:
                order_id = exchange_order['data'].get('orderId')

            if not order_id:
                logger.error(f"Order accepted but no orderId found in response: {exchange_order}")
                return False

            # Спершу пробуємо синхронну відповідь create_order напряму —
            # на практиці біржа часто вже повертає status=FILLED з реальними
            # avgPrice/executedQty одразу (без затримки). Опитування (poll)
            # і позиції — це fallback тільки на випадок, коли синхронна
            # відповідь цього не дає.
            entry_price = 0.0
            executed_qty = quantity

            immediate_order_info = None
            if 'data' in exchange_order and 'order' in exchange_order['data']:
                immediate_order_info = exchange_order['data']['order']

            status, avg_price, parsed_qty = self._parse_fill(immediate_order_info)

            if status == 'FILLED' and avg_price > 0 and parsed_qty > 0:
                entry_price = avg_price
                executed_qty = parsed_qty
                logger.info(
                    f"Fill confirmed directly from create_order response for {symbol} {side}: "
                    f"avgPrice={entry_price}, executedQty={executed_qty} (no extra polling needed)"
                )
            else:
                confirmed_order = await self._wait_for_confirmed_fill(symbol, str(order_id))
                if confirmed_order:
                    _, confirmed_avg_price, confirmed_qty = self._parse_fill(confirmed_order)
                    if confirmed_avg_price > 0:
                        entry_price = confirmed_avg_price
                    if confirmed_qty > 0:
                        executed_qty = confirmed_qty

            if entry_price <= 0:
                # get_order() за відведені спроби не показав FILLED з ціною —
                # пробуємо ще раз через фактичний стан позиції на біржі,
                # перш ніж здаватись і рахувати SL/TP "наосліп"
                fallback = await self._fetch_entry_from_positions(symbol, side)
                if fallback:
                    entry_price = fallback['entry_price']
                    executed_qty = fallback['quantity']
                else:
                    logger.warning(
                        f"Failed to confirm entry_price for {symbol} {side} order {order_id} "
                        f"(both order status and positions endpoint gave nothing usable) — "
                        f"SL/TP will be based on unconfirmed price, may be inaccurate"
                    )

            if executed_qty != quantity:
                logger.info(
                    f"Using executedQty={executed_qty} instead of requested quantity={quantity} "
                    f"for {symbol} {side} SL/TP sizing"
                )

            # Якщо стратегія передала reference_price (ціну, від якої рахувала % для SL/TP),
            # перераховуємо рівні під фактичну ціну виконання ринкового ордера —
            # інакше SL/TP залишаться прив'язані до "старої" ціни сигналу, а не до реального входу
            if reference_price and entry_price and reference_price > 0:
                scale = entry_price / reference_price
                if stop_loss_price:
                    stop_loss_price = stop_loss_price * scale
                if take_profit_levels:
                    take_profit_levels = [
                        {**lvl, 'price': lvl['price'] * scale}
                        for lvl in take_profit_levels
                    ]
                logger.info(
                    f"Rescaled SL/TP for {symbol} {side}: reference_price={reference_price}, "
                    f"entry_price={entry_price}, scale={scale:.6f}"
                )

            # Зберігаємо позицію в пам'яті
            position_key = f"{symbol}_{side}"
            position_data = {
                'order_id': str(order_id),
                'symbol': symbol,
                'side': side,
                'quantity': executed_qty,
                'entry_price': entry_price,
                'leverage': leverage,
                'stop_loss_price': stop_loss_price,
                'take_profit_levels': take_profit_levels,
                'opened_by': 'bot',
                'sl_order_id': None,
                'tp_order_ids': [],
                'sl_client_order_id': None,
                'tp_client_order_ids': [],
                # ім'я стратегії, яка згенерувала сигнал на відкриття
                'strategy': strategy,
                'realized_pnl_accum': 0.0,
                'commission_accum': 0.0,
            }
            self.open_positions[position_key] = position_data

            logger.info(f"Position tracked: orderId={order_id}, {symbol} {side}, strategy={strategy}")

            # Зберігаємо позицію в БД
            try:
                self.db.insert_position(
                    order_id=str(order_id),
                    symbol=symbol,
                    side=side,
                    status='OPEN',
                    metadata=json.dumps(position_data)
                )
            except Exception as e:
                logger.error(f"Failed to save position to DB: {e}", exc_info=True)

            # Створюємо стоп/тейк ордери — використовуємо реально виконаний обсяг
            # (executed_qty), а не запитаний quantity, щоб уникнути розсинхрону
            # з реальним залишком позиції на біржі
            if stop_loss_price:
                sl_order_id, sl_client_order_id = await self._create_stop_loss(
                    symbol, side, executed_qty, stop_loss_price, entry_price
                )
                if sl_order_id:
                    self.open_positions[position_key]['sl_order_id'] = str(sl_order_id)
                if sl_client_order_id:
                    self.open_positions[position_key]['sl_client_order_id'] = sl_client_order_id

            if take_profit_levels:
                tp_results = await self._create_take_profit_orders(symbol, side, executed_qty, take_profit_levels)
                self.open_positions[position_key]['tp_order_ids'] = [
                    str(r['order_id']) for r in tp_results if r.get('order_id')
                ]
                self.open_positions[position_key]['tp_client_order_ids'] = [
                    r['client_order_id'] for r in tp_results if r.get('client_order_id')
                ]

            # Оновлюємо metadata в БД з sl_order_id/tp_order_ids
            try:
                self.db.update_position_metadata(
                    order_id=str(order_id),
                    metadata=json.dumps(self.open_positions[position_key])
                )
            except Exception as e:
                logger.error(f"Failed to update position metadata in DB: {e}", exc_info=True)

            margin_usdt = self._calc_margin_usdt(self.open_positions[position_key])

            # Публікуємо подію POSITION_OPENED
            await self.event_bus.publish(Event(
                type=EventType.POSITION_OPENED,
                data={
                    'symbol': symbol,
                    'side': side,
                    'entry_price': entry_price,
                    'quantity': quantity,
                    'leverage': leverage,
                    'stop_loss_price': stop_loss_price,
                    'take_profit_levels': take_profit_levels,
                    'margin_usdt': margin_usdt,
                    'positions_info_message': positions_info_message
                }
            ))

            return True

        except Exception as e:
            logger.error(f"Failed to open position: {e}", exc_info=True)
            await self._notify_error(
                error=str(e),
                context=f"Неочікувана помилка при відкритті позиції {symbol} {side}"
            )
            return False

    async def _create_stop_loss(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_loss_price: float,
        entry_price: Optional[float] = None
    ) -> tuple:
        """
        Повертає (order_id, client_order_id).

        client_order_id генеруємо самі і передаємо в create_order —
        це ЄДИНИЙ надійний спосіб пізніше розпізнати "цей filled-ордер —
        наш SL", тому що для умовних (STOP_MARKET/TAKE_PROFIT_MARKET) ордерів
        orderId, який повертається при РОЗМІЩЕННІ (planned order id), і orderId,
        який приходить в стрімі при фактичному ВИКОНАННІ (execution id),
        на біржах цього типу (BingX/Binance-style) можуть НЕ збігатися.
        clientOrderId, який ми самі задаємо, біржа зобов'язана повернути
        незмінним в обох випадках — тому саме на нього і матчимо закриття.

        Параметр client_order_id тут відповідає однойменному аргументу
        BingXClient.create_order(), який відправляється біржі як
        'clientOrderID' і повертається в ORDER_TRADE_UPDATE як поле 'c'.
        """
        close_side = 'SELL' if side == 'LONG' else 'BUY'
        position_side = 'LONG' if side == 'LONG' else 'SHORT'

        # SL завжди закриває позицію повністю. Невеликий safety margin (0.1%)
        # захищає від помилки 110424 на межі округлення: BingX округлює
        # надісланий quantity під свою precision, і якщо округлення йде вгору,
        # запитуваний обсяг стає більшим за реальний залишок позиції.
        current_qty = quantity * 0.999
        current_stop_price = stop_loss_price

        # % буфер SL відносно ціни входу — потрібен, щоб при "переякоренні"
        # (див. нижче) зберегти той самий відсотковий відступ, а не абсолютну ціну
        buffer_percent = None
        if entry_price and entry_price > 0 and stop_loss_price:
            buffer_percent = abs(entry_price - stop_loss_price) / entry_price

        # Генеруємо один раз — навіть якщо доведеться ретраїти запит (той самий
        # логічний SL), client_order_id має лишатись тим самим для матчингу
        client_order_id = f"sl-{symbol}-{position_side}-{int(time.time() * 1000)}"

        max_attempts = 3
        last_error: Optional[BingXAPIError] = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = await self.exchange.create_order(
                    symbol=symbol,
                    side=close_side,
                    order_type='STOP_MARKET',
                    stop_price=current_stop_price,
                    position_side=position_side,
                    quantity=current_qty,
                    client_order_id=client_order_id,  
                )

                order_id = None
                if 'data' in response and 'order' in response['data']:
                    order_id = response['data']['order'].get('orderId')

                logger.info(
                    f"Stop loss created: {symbol} @ {current_stop_price}, "
                    f"orderId={order_id}, clientOrderId={client_order_id}"
                )
                return order_id, client_order_id

            except BingXAPIError as e:
                last_error = e

                if e.code == 110424 and attempt < max_attempts:
                    # впираємось у "must be less than available" на межі округлення —
                    # зменшуємо обсяг ще трохи і пробуємо ще раз
                    current_qty = current_qty * 0.999
                    logger.warning(
                        f"SL create hit 110424 for {symbol}, retrying (attempt {attempt+1}) "
                        f"with quantity={current_qty}"
                    )
                    continue

                if 'current price' in (e.msg or '').lower() and buffer_percent is not None and attempt < max_attempts:
                    # ціна встигла пройти наш SL, поки запит летів до біржі (типово
                    # для вузького % буфера на волатильних/тонких парах) —
                    # переякорюємо SL до актуальної ринкової ціни з тим самим % буфером
                    try:
                        live_price = await self.exchange.get_ticker_price(symbol)
                        new_stop_price = (
                            live_price * (1 - buffer_percent) if side == 'LONG'
                            else live_price * (1 + buffer_percent)
                        )
                        logger.warning(
                            f"SL for {symbol} rejected as stale (price already passed target "
                            f"{current_stop_price}). Re-anchoring to live price {live_price} -> "
                            f"new stop={new_stop_price}, retrying (attempt {attempt+1})"
                        )
                        current_stop_price = new_stop_price
                        continue
                    except Exception as fetch_err:
                        logger.error(f"Failed to fetch live price to re-anchor SL for {symbol}: {fetch_err}")
                        break

                # інша помилка, або спроби вичерпані — далі не ретраїмо
                break

            except Exception as e:
                logger.error(f"Failed to create stop loss: {e}")
                await self._notify_error(
                    error=str(e),
                    context=f"Не вдалося поставити SL для {symbol} — позиція без захисту!",
                    critical=True
                )
                return None, None

        # усі спроби вичерпані
        logger.error(f"Failed to create stop loss for {symbol}: {last_error.code if last_error else '?'} {last_error.msg if last_error else ''}")
        await self._notify_error(
            error=f"{last_error.code} {last_error.msg}" if last_error else "unknown error",
            context=f"Не вдалося поставити SL для {symbol} — позиція без захисту!",
            critical=True
        )
        return None, None


    async def _create_take_profit_orders(self, symbol: str, side: str, quantity: float, tp_levels: list) -> list:
        """
        Повертає список {'order_id': ..., 'client_order_id': ...} по кожному рівню TP.
        Див. докстрінг _create_stop_loss щодо того, навіщо потрібен client_order_id.
        """
        results = []

        # якщо це ЄДИНИЙ рівень і він закриває 100% — використовуємо
        # closePosition=true замість розрахованого quantity, щоб уникнути
        # помилки 110424 через округлення quantity біржею (див. _create_stop_loss)
        is_single_full_close = len(tp_levels) == 1 and tp_levels[0].get('close_percent') == 100

        for i, tp_level in enumerate(tp_levels):
            client_order_id = f"tp{i+1}-{symbol}-{side}-{int(time.time() * 1000)}"
            try:
                tp_price = tp_level['price']
                close_side = 'SELL' if side == 'LONG' else 'BUY'
                position_side = 'LONG' if side == 'LONG' else 'SHORT'

                if is_single_full_close:
                    # той самий safety margin (0.1%), що і для SL повного закриття —
                    # захист від 110424 на межі округлення quantity биржею
                    safe_quantity = quantity * 0.999

                    async def _attempt(qty: float):
                        return await self.exchange.create_order(
                            symbol=symbol,
                            side=close_side,
                            order_type='TAKE_PROFIT_MARKET',
                            stop_price=tp_price,
                            position_side=position_side,
                            quantity=qty,
                            client_order_id=client_order_id
                        )

                    try:
                        response = await _attempt(safe_quantity)
                    except BingXAPIError as e:
                        if e.code == 110424:
                            retry_quantity = safe_quantity * 0.999
                            logger.warning(
                                f"TP create hit 110424 for {symbol} with quantity={safe_quantity}, "
                                f"retrying once with quantity={retry_quantity}"
                            )
                            response = await _attempt(retry_quantity)
                        else:
                            raise
                else:
                    # справжній частковий TP (декілька рівнів) — тут quantity
                    # обов'язковий, і проблема округлення поки залишається
                    # актуальною для цього випадку (потрібне округлення під
                    # precision символу — окрема задача, якщо почнеш
                    # використовувати кілька рівнів TP замість одного повного)
                    tp_quantity = quantity * (tp_level['close_percent'] / 100)
                    response = await self.exchange.create_order(
                        symbol=symbol,
                        side=close_side,
                        order_type='TAKE_PROFIT_MARKET',
                        quantity=tp_quantity,
                        stop_price=tp_price,
                        position_side=position_side,
                        client_order_id=client_order_id
                    )

                order_id = None
                if 'data' in response and 'order' in response['data']:
                    order_id = response['data']['order'].get('orderId')

                results.append({'order_id': order_id, 'client_order_id': client_order_id})
                logger.info(
                    f"Take profit {i+1} created: {symbol} @ {tp_price}, "
                    f"orderId={order_id}, clientOrderId={client_order_id}"
                )

            except BingXAPIError as e:
                logger.error(f"Failed to create take profit {i+1} for {symbol}: {e.code} {e.msg}")
                await self._notify_error(
                    error=f"{e.code} {e.msg}",
                    context=f"Не вдалося поставити TP{i+1} для {symbol} (SL, якщо є, залишається активним)"
                )
                results.append({'order_id': None, 'client_order_id': None})
            except Exception as e:
                logger.error(f"Failed to create take profit {i+1}: {e}")
                results.append({'order_id': None, 'client_order_id': None})

        return results

    @staticmethod
    def _calc_roe_percent(position: dict, realized_pnl: float) -> Optional[float]:
        """
        ROE% = PnL відносно маржі, використаної на відкриття (entry_price * quantity / leverage) —
        так само, як біржа рахує ROE% в інтерфейсі. Повертає None, якщо бракує
        даних для розрахунку (замість того щоб мовчки підставити 0).
        """
        entry_price = position.get('entry_price')
        quantity = position.get('quantity')
        leverage = position.get('leverage') or 1

        if not entry_price or not quantity or entry_price <= 0 or quantity <= 0:
            return None
        # leverage може прилетіти з конфігу як щось не-числове (наприклад
        # кортеж через баговану кому в конфізі стратегії) — краще явно
        # впасти в None, ніж кидати неопрацьований TypeError і губити
        # публікацію POSITION_CLOSED цілком
        try:
            leverage = float(leverage)
        except (TypeError, ValueError):
            logger.error(f"Invalid leverage value in position data, cannot calc ROE%: {leverage!r}")
            return None

        if leverage <= 0:
            return None

        margin = (entry_price * quantity) / leverage
        if margin <= 0:
            return None

        return (realized_pnl / margin) * 100

    @staticmethod
    def _calc_margin_usdt(position: dict) -> Optional[float]:
        """Скільки USDT реально було вкладено (маржа) — потрібно для статистики в $."""
        entry_price = position.get('entry_price')
        quantity = position.get('quantity')
        leverage = position.get('leverage') or 1

        if not entry_price or not quantity or entry_price <= 0 or quantity <= 0:
            return None
        try:
            leverage = float(leverage)
        except (TypeError, ValueError):
            return None
        if leverage <= 0:
            return None

        return (entry_price * quantity) / leverage

    async def _handle_order_update(self, event: Event) -> None:
        order_data = event.data.get('o', {})
        exchange_order_id = str(order_data.get('i'))
        client_order_id = order_data.get('c')  # clientOrderId — те, що ми самі задали при створенні SL/TP
        status = order_data.get('X')
        order_type = order_data.get('o')
        symbol = order_data.get('s')
        position_side = order_data.get('ps')

        if status == 'FILLED' and order_type in ('STOP_MARKET', 'TAKE_PROFIT_MARKET', 'MARKET') and order_data.get('ro') == True:
            position_key = f"{symbol}_{position_side}"
            position = self.open_positions.get(position_key)

            if not position:
                logger.debug(f"No open position tracked for {symbol} {position_side}, skipping")
                return

            # Матчимо ПЕРШ ЗА ВСЕ по clientOrderId — це надійний ідентифікатор,
            # який ми самі згенерували і передали біржі при створенні SL/TP.
            # Матч по exchange orderId лишаємо як fallback (для сумісності зі
            # старими позиціями, збереженими до цього фіксу, де client_order_id
            # ще не зберігався).
            known_bot_client_order_ids = set(position.get('tp_client_order_ids', []) or [])
            if position.get('sl_client_order_id'):
                known_bot_client_order_ids.add(position['sl_client_order_id'])

            known_bot_order_ids = set(position.get('tp_order_ids', []) or [])
            if position.get('sl_order_id'):
                known_bot_order_ids.add(position['sl_order_id'])

            if client_order_id and client_order_id in known_bot_client_order_ids:
                closed_by = 'bot'
            elif exchange_order_id in known_bot_order_ids:
                closed_by = 'bot'
            else:
                closed_by = 'user'

            trade_id = order_data.get('t')
            # 'q' — це початковий розмір ОРДЕРА (order quantity), не факт
            # виконання цього конкретного fill'а. Для матчингу обсягу
            # закриття потрібне поле 'l' (last executed quantity) —
            # інакше remaining_quantity/is_full_close рахуються неправильно
            # при часткових закриттях.
            filled_qty = float(order_data.get('l', 0) or order_data.get('q', 0) or 0)
            # 'rp' — реалізований PnL САМЕ цього закриваючого fill'а (не кумулятивний
            # за всю позицію), тому накопичуємо його по всіх часткових закриттях
            trade_realized_pnl = float(order_data.get('rp', 0) or 0)
            commission = float(order_data.get('n', 0) or 0)
            commission_asset = order_data.get('N')

            # накопичуємо ID закриваючих угод — тільки ID, жодних цін/PnL
            position.setdefault('closing_trade_ids', [])
            if trade_id is not None:
                position['closing_trade_ids'].append(trade_id)
            position.setdefault('closing_orders', [])
            position['closing_orders'].append({
                'order_id': exchange_order_id,
                'client_order_id': client_order_id,
                'closed_by': closed_by
            })

            position['realized_pnl_accum'] = position.get('realized_pnl_accum', 0.0) + trade_realized_pnl
            position['commission_accum'] = position.get('commission_accum', 0.0) + commission
            if commission_asset:
                position['commission_asset'] = commission_asset

            remaining = position.get('remaining_quantity', position.get('quantity', 0)) - filled_qty
            position['remaining_quantity'] = max(0.0, remaining)

            logger.info(
                f"Partial/full close fill: {symbol} {position_side}, order={exchange_order_id}, "
                f"client_order_id={client_order_id}, trade_id={trade_id}, filled_qty={filled_qty}, "
                f"trade_pnl={trade_realized_pnl}, commission={commission}, "
                f"remaining={position['remaining_quantity']:.8f}, closed_by={closed_by}"
            )

            # SL завжди закриває решту повністю (STOP_MARKET без closePosition тут не має 'quantity' часткового рівня)
            is_full_close = order_type == 'STOP_MARKET' or position['remaining_quantity'] <= 1e-8

            try:
                self.db.update_position_metadata(
                    order_id=position['order_id'],
                    metadata=json.dumps(position)
                )
            except Exception as e:
                logger.error(f"Failed to update position metadata (partial close) in DB: {e}", exc_info=True)

            if not is_full_close:
                # позиція ще частково відкрита — НЕ видаляємо, НЕ закриваємо в БД
                return

            del self.open_positions[position_key]

            logger.info(f"Position fully closed: {symbol} {position_side}, closed_by={closed_by}")

            close_price = float(order_data.get('ap', 0) or 0)
            realized_pnl = position.get('realized_pnl_accum', 0.0)
            commission_total = position.get('commission_accum', 0.0)
            net_pnl = realized_pnl - commission_total
            margin_usdt = self._calc_margin_usdt(position)
            try:
                roe_percent = self._calc_roe_percent(position, realized_pnl)
            except Exception as e:
                logger.error(f"Failed to calc ROE% for {symbol} {position_side}: {e}", exc_info=True)
                roe_percent = None

            try:
                self.db.update_position_status(
                    order_id=position['order_id'],
                    status='CLOSED',
                    closed_at=datetime.utcnow(),
                    close_price=close_price,
                    realized_pnl=realized_pnl,
                    roe_percent=roe_percent,
                    commission_usdt=commission_total,
                    net_pnl=net_pnl,
                    margin_usdt=margin_usdt,
                )
            except Exception as e:
                logger.error(f"Failed to update position status in DB: {e}", exc_info=True)

            strategy = position.get('strategy')
            close_info_message = f"Стратегія: {strategy}" if strategy else None

            await self.event_bus.publish(Event(
                type=EventType.POSITION_CLOSED,
                data={
                    'symbol': symbol,
                    'side': position_side,
                    'close_price': close_price,
                    'realized_pnl': realized_pnl,
                    'commission_usdt': commission_total,
                    'net_pnl': net_pnl,
                    'margin_usdt': margin_usdt,
                    'roe_percent': roe_percent,
                    'closed_by': closed_by,
                    'order_id': position.get('order_id'),
                    'entry_price': position.get('entry_price'),
                    'quantity': position.get('quantity'),
                    'leverage': position.get('leverage'),
                    'strategy': strategy,
                    'positions_info_message': close_info_message
                }
            ))

    async def _handle_account_update(self, event: Event) -> None:
        """Ловить ручні дії на біржі, які не пройшли через ORDER_TRADE_UPDATE обробник (safety net)"""
        positions_data = event.data.get('a', {}).get('P', [])

        for pos in positions_data:
            symbol = pos.get('s')
            position_side = pos.get('ps')
            pa = float(pos.get('pa', 0))

            if not symbol or not position_side:
                continue

            position_key = f"{symbol}_{position_side}"
            existing = self.open_positions.get(position_key)

            if pa != 0 and not existing:
                # Позиція відкрита вручну на біржі — бот про неї не знав
                manual_order_id = f"manual-{symbol}-{position_side}-{int(time.time())}"
                position_data = {
                    'order_id': manual_order_id,
                    'symbol': symbol,
                    'side': position_side,
                    'entry_price': float(pos.get('ep', 0)),
                    'quantity': abs(pa),
                    'opened_by': 'user',
                    'sl_order_id': None,
                    'tp_order_ids': [],
                    'sl_client_order_id': None,
                    'tp_client_order_ids': [],
                    'strategy': None,
                    'realized_pnl_accum': 0.0,
                    'commission_accum': 0.0,
                }
                self.open_positions[position_key] = position_data
                logger.info(f"Manual position detected and tracked: {symbol} {position_side}")

                # Зберігаємо позицію в БД
                try:
                    self.db.insert_position(
                        order_id=manual_order_id,
                        symbol=symbol,
                        side=position_side,
                        status='OPEN',
                        metadata=json.dumps(position_data)
                    )
                except Exception as e:
                    logger.error(f"Failed to save manual position to DB: {e}", exc_info=True)

                margin_usdt = self._calc_margin_usdt(position_data)

                # Публікуємо подію POSITION_OPENED
                await self.event_bus.publish(Event(
                    type=EventType.POSITION_OPENED,
                    data={
                        'symbol': symbol,
                        'side': position_side,
                        'entry_price': float(pos.get('ep', 0)),
                        'quantity': abs(pa),
                        'leverage': 0,
                        'stop_loss_price': None,
                        'margin_usdt': margin_usdt,
                        'positions_info_message': "Позицію відкрито вручну."
                    }
                ))

            elif pa == 0 and existing:
                # Позиція закрита на біржі
                del self.open_positions[position_key]

                logger.info(f"Position closed (detected via account update): {symbol} {position_side}")

                entry_price = existing.get('entry_price')
                quantity = existing.get('quantity')

                # Те, що вже накопичено з попередніх часткових закриттів (TP/SL),
                # які пройшли через ORDER_TRADE_UPDATE (_handle_order_update).
                # Раніше ці значення ігнорувались і realized_pnl рахувався
                # "з нуля" по цьому останньому закриттю — через це підсумковий
                # PnL губив прибуток/збиток від попередніх часткових TP/SL
                # закриттів. Тепер просто додаємо PnL останнього закриття
                # до вже накопиченого.
                already_accumulated_pnl = existing.get('realized_pnl_accum', 0.0)
                already_accumulated_commission = existing.get('commission_accum', 0.0)

                # 'cr' в ACCOUNT_UPDATE — це PnL САМЕ цього (останнього) закриття,
                # яке не пройшло через ORDER_TRADE_UPDATE (напр. ручне закриття
                # залишку позиції на біржі)
                last_fill_pnl = float(pos.get('cr', 0) or 0)
                realized_pnl = already_accumulated_pnl + last_fill_pnl

                commission_total = already_accumulated_commission
                net_pnl = realized_pnl - commission_total
                margin_usdt = self._calc_margin_usdt(existing)
                try:
                    roe_percent = self._calc_roe_percent(existing, realized_pnl)
                except Exception as e:
                    logger.error(f"Failed to calc ROE% for {symbol} {position_side}: {e}", exc_info=True)
                    roe_percent = None

                # close_price виводимо математично з entry_price/quantity/сукупного realized_pnl,
                # а не з поточної ціни тікера (яка на момент запиту вже може відрізнятись
                # від фактичної ціни виконання закриваючого ордера)
                close_price = 0.0
                if entry_price and quantity and quantity > 0:
                    if position_side == 'LONG':
                        close_price = entry_price + (realized_pnl / quantity)
                    else:
                        close_price = entry_price - (realized_pnl / quantity)
                else:
                    try:
                        close_price = await self.exchange.get_ticker_price(symbol)
                    except Exception as e:
                        logger.warning(f"Failed to fetch close_price ticker for {symbol}: {e}")

                # Оновлюємо статус в БД
                try:
                    self.db.update_position_status(
                        order_id=existing['order_id'],
                        status='CLOSED',
                        closed_at=datetime.utcnow(),
                        close_price=close_price,
                        realized_pnl=realized_pnl,
                        roe_percent=roe_percent,
                        commission_usdt=commission_total,
                        net_pnl=net_pnl,
                        margin_usdt=margin_usdt,
                    )
                except Exception as e:
                    logger.error(f"Failed to update manual position status in DB: {e}", exc_info=True)

                strategy = existing.get('strategy')
                close_info_message = f"Стратегія: {strategy}" if strategy else None

                # Публікуємо подію POSITION_CLOSED
                await self.event_bus.publish(Event(
                    type=EventType.POSITION_CLOSED,
                    data={
                        'symbol': symbol,
                        'side': position_side,
                        'close_price': close_price,
                        'realized_pnl': realized_pnl,
                        'commission_usdt': commission_total,
                        'net_pnl': net_pnl,
                        'margin_usdt': margin_usdt,
                        'roe_percent': roe_percent,
                        'closed_by': existing.get('opened_by', 'user'),
                        'order_id': existing.get('order_id'),
                        'entry_price': existing.get('entry_price'),
                        'quantity': existing.get('quantity'),
                        'leverage': existing.get('leverage'),
                        'strategy': strategy,
                        'positions_info_message': close_info_message
                    }
                ))