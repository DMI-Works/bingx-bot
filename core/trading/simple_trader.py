import asyncio
import logging
from typing import Optional
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
                    'tp_order_ids': metadata.get('tp_order_ids', [])
                }

            if rows:
                logger.info(f"Restored {len(rows)} open positions from DB")
        except Exception as e:
            logger.error(f"Failed to restore open positions from DB: {e}", exc_info=True)

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
                take_profit_levels=signal.get('take_profit_levels')
            )

    async def open_position(
        self,
        symbol: str,
        side: str,
        quantity: float,
        leverage: int = 10,
        stop_loss_price: Optional[float] = None,
        take_profit_levels: Optional[list] = None
    ) -> bool:
        try:
            positions_info_message = None
            if self.risk_manager:
                can_open, reason = await self.risk_manager.can_open_position(symbol)
                positions_info_message = reason if reason else "Position can be opened."

                if not can_open:
                    logger.warning(f"Risk manager blocked {symbol} {side}: {reason}")
                    return False

            logger.info(f"Opening position: {symbol} {side} {quantity}")

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
                    logger.warning(
                        f"Order rejected by exchange (API orders temporarily disabled) "
                        f"for {symbol} {side}: {e.msg}"
                    )
                else:
                    logger.error(f"Order rejected by exchange for {symbol} {side}: {e.code} {e.msg}")
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

            # Отримуємо entry_price з відповіді біржі
            entry_price = 0.0
            if 'data' in exchange_order and 'order' in exchange_order['data']:
                entry_price = float(exchange_order['data']['order'].get('avgPrice', 0))

            # Зберігаємо позицію в пам'яті
            position_key = f"{symbol}_{side}"
            position_data = {
                'order_id': str(order_id),
                'symbol': symbol,
                'side': side,
                'quantity': quantity,
                'entry_price': entry_price,
                'leverage': leverage,
                'stop_loss_price': stop_loss_price,
                'take_profit_levels': take_profit_levels,
                'opened_by': 'bot',
                'sl_order_id': None,
                'tp_order_ids': []
            }
            self.open_positions[position_key] = position_data

            logger.info(f"Position tracked: orderId={order_id}, {symbol} {side}")

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

            # Створюємо стоп/тейк ордери
            if stop_loss_price:
                sl_order_id = await self._create_stop_loss(symbol, side, quantity, stop_loss_price)
                if sl_order_id:
                    self.open_positions[position_key]['sl_order_id'] = str(sl_order_id)

            if take_profit_levels:
                tp_order_ids = await self._create_take_profit_orders(symbol, side, quantity, take_profit_levels)
                self.open_positions[position_key]['tp_order_ids'] = [str(tid) for tid in tp_order_ids if tid]

            # Оновлюємо metadata в БД з sl_order_id/tp_order_ids
            try:
                self.db.update_position_metadata(
                    order_id=str(order_id),
                    metadata=json.dumps(self.open_positions[position_key])
                )
            except Exception as e:
                logger.error(f"Failed to update position metadata in DB: {e}", exc_info=True)

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
                    'positions_info_message': positions_info_message
                }
            ))

            return True

        except Exception as e:
            logger.error(f"Failed to open position: {e}", exc_info=True)
            return False

    async def _create_stop_loss(self, symbol: str, side: str, quantity: float, stop_loss_price: float) -> Optional[str]:
        try:
            close_side = 'SELL' if side == 'LONG' else 'BUY'
            position_side = 'LONG' if side == 'LONG' else 'SHORT'

            response = await self.exchange.create_order(
                symbol=symbol,
                side=close_side,
                order_type='STOP_MARKET',
                quantity=quantity,
                stop_price=stop_loss_price,
                position_side=position_side
            )

            order_id = None
            if 'data' in response and 'order' in response['data']:
                order_id = response['data']['order'].get('orderId')

            logger.info(f"Stop loss created: {symbol} @ {stop_loss_price}, orderId={order_id}")
            return order_id

        except BingXAPIError as e:
            logger.error(f"Failed to create stop loss for {symbol}: {e.code} {e.msg}")
            return None
        except Exception as e:
            logger.error(f"Failed to create stop loss: {e}")
            return None


    async def _create_take_profit_orders(self, symbol: str, side: str, quantity: float, tp_levels: list) -> list:
        order_ids = []
        for i, tp_level in enumerate(tp_levels):
            try:
                tp_price = tp_level['price']
                tp_quantity = quantity * (tp_level['close_percent'] / 100)

                close_side = 'SELL' if side == 'LONG' else 'BUY'
                position_side = 'LONG' if side == 'LONG' else 'SHORT'

                response = await self.exchange.create_order(
                    symbol=symbol,
                    side=close_side,
                    order_type='TAKE_PROFIT_MARKET',
                    quantity=tp_quantity,
                    stop_price=tp_price,
                    position_side=position_side
                )

                order_id = None
                if 'data' in response and 'order' in response['data']:
                    order_id = response['data']['order'].get('orderId')

                order_ids.append(order_id)
                logger.info(f"Take profit {i+1} created: {symbol} @ {tp_price}, orderId={order_id}")

            except BingXAPIError as e:
                logger.error(f"Failed to create take profit {i+1} for {symbol}: {e.code} {e.msg}")
                order_ids.append(None)
            except Exception as e:
                logger.error(f"Failed to create take profit {i+1}: {e}")
                order_ids.append(None)

        return order_ids

    async def _handle_order_update(self, event: Event) -> None:
        order_data = event.data.get('o', {})
        exchange_order_id = str(order_data.get('i'))
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

            known_bot_order_ids = set(position.get('tp_order_ids', []))
            if position.get('sl_order_id'):
                known_bot_order_ids.add(position['sl_order_id'])
            closed_by = 'bot' if exchange_order_id in known_bot_order_ids else 'user'

            trade_id = order_data.get('t')
            filled_qty = float(order_data.get('q', 0))  # обсяг ЦЬОГО закриваючого ордера

            # накопичуємо ID закриваючих угод — тільки ID, жодних цін/PnL
            position.setdefault('closing_trade_ids', [])
            if trade_id is not None:
                position['closing_trade_ids'].append(trade_id)
            position.setdefault('closing_orders', [])
            position['closing_orders'].append({'order_id': exchange_order_id, 'closed_by': closed_by})

            remaining = position.get('remaining_quantity', position.get('quantity', 0)) - filled_qty
            position['remaining_quantity'] = max(0.0, remaining)

            logger.info(
                f"Partial/full close fill: {symbol} {position_side}, order={exchange_order_id}, "
                f"trade_id={trade_id}, filled_qty={filled_qty}, remaining={position['remaining_quantity']:.8f}"
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

            # позиція реально повністю закрита
            del self.open_positions[position_key]

            logger.info(f"Position fully closed: {symbol} {position_side}, closed_by={closed_by}")

            try:
                self.db.update_position_status(
                    order_id=position['order_id'],
                    status='CLOSED',
                    closed_at=datetime.utcnow()
                )
            except Exception as e:
                logger.error(f"Failed to update position status in DB: {e}", exc_info=True)

            await self.event_bus.publish(Event(
                type=EventType.POSITION_CLOSED,
                data={
                    'symbol': symbol,
                    'side': position_side,
                    'close_price': float(order_data.get('ap', 0)),
                    'closed_by': closed_by
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
                manual_order_id = f"manual-{symbol}-{position_side}-{int(datetime.utcnow().timestamp())}"
                position_data = {
                    'order_id': manual_order_id,
                    'symbol': symbol,
                    'side': position_side,
                    'entry_price': float(pos.get('ep', 0)),
                    'quantity': abs(pa),
                    'opened_by': 'user',
                    'sl_order_id': None,
                    'tp_order_ids': []
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
                        'positions_info_message': "Позицію відкрито вручну."
                    }
                ))

            elif pa == 0 and existing:
                # Позиція закрита на біржі
                del self.open_positions[position_key]

                logger.info(f"Position closed (detected via account update): {symbol} {position_side}")

                # Оновлюємо статус в БД
                try:
                    self.db.update_position_status(
                        order_id=existing['order_id'],
                        status='CLOSED',
                        closed_at=datetime.utcnow()
                    )
                except Exception as e:
                    logger.error(f"Failed to update manual position status in DB: {e}", exc_info=True)

                # Публікуємо подію POSITION_CLOSED
                await self.event_bus.publish(Event(
                    type=EventType.POSITION_CLOSED,
                    data={
                        'symbol': symbol,
                        'side': position_side,
                        'close_price': 0.0,
                        'realized_pnl': float(pos.get('cr', 0)),  # 'cr' = closed realized PnL
                        'closed_by': existing.get('opened_by', 'user')
                    }
                ))