import logging
import time
from datetime import datetime
from typing import Callable, Optional

from ..common.market_data import extract_order_id, to_float
from ..common.position_math import (
    calc_margin_usdt,
    calc_net_pnl,
    calc_roe_percent,
    entry_order_side,
    close_order_side,
    position_key,
    rescale_to_fill,
)
from ..common.position_metadata import dump_metadata, persist_position_metadata
from ..database import Database
from ..events import EventBus, Event, EventType, publish_error
from ..exchange import BingXClient
from ..exchange.errors import API_ORDERS_DISABLED_CODE, BingXAPIError
from ..risk import RiskManager
from .fill_resolver import resolve_entry_fill
from .position_records import new_bot_position, new_manual_position, position_from_db_row
from .protective_orders import ProtectiveOrderPlacer


logger = logging.getLogger(__name__)

# тип закриваючого ордера -> close_reason у trade_analytics
_CLOSE_REASON_BY_ORDER_TYPE = {
    'STOP_MARKET': 'stop_loss',
    'TAKE_PROFIT_MARKET': 'take_profit',
}


class SimpleTrader:
    """Єдине місце, яке відкриває/закриває позиції та веде їхній облік."""

    def __init__(
        self,
        exchange: BingXClient,
        event_bus: EventBus,
        db: Database,
        risk_manager: Optional[RiskManager] = None,
        settings_manager=None,
        strategy_settings=None,
    ):
        self.exchange = exchange
        self.event_bus = event_bus
        self.db = db
        self.risk_manager = risk_manager
        # Глобальний kill-switch (trading.enabled) з вкладки «Настройки» мініаппу.
        # Не передали (старі тести) — сигнали виконуються завжди.
        self.settings_manager = settings_manager
        # StrategySettingsStore — ЛИШЕ для знімку параметрів стратегії в
        # trade_analytics на момент відкриття (на торгівлю не впливає).
        self.strategy_settings = strategy_settings
        # symbol -> dict | None: 24h-знімок ринку монети для trade_analytics
        # (SymbolSelector.market_context; підключається в main.py)
        self.market_context_provider: Optional[Callable[[str], Optional[dict]]] = None
        self.orders = ProtectiveOrderPlacer(exchange, event_bus)

        self.open_positions = {}

        self.event_bus.subscribe(EventType.SIGNAL_GENERATED, self._handle_signal)
        self.event_bus.subscribe(EventType.ORDER_FILLED, self._handle_order_update)
        self.event_bus.subscribe(EventType.HANDLE_ACCOUNT_INFO_UPDATE, self._handle_account_update)

        # Відновлюємо активні позиції з БД при старті (переживають рестарт бота)
        self._restore_open_positions()

    def _restore_open_positions(self) -> None:
        """Підтягуємо з БД позиції, які залишились OPEN з попереднього запуску"""
        try:
            rows = self.db.get_active_positions()
            for row in rows:
                self.open_positions[position_key(row['symbol'], row['side'])] = position_from_db_row(row)
            if rows:
                logger.info(f"Restored {len(rows)} open positions from DB")
        except Exception as e:
            logger.error(f"Failed to restore open positions from DB: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # допоміжне
    # ------------------------------------------------------------------

    def _recent_equity(self, max_age_seconds: float = 120.0) -> Optional[float]:
        """equity, який RiskManager отримав під час сайзингу цієї ж угоди (без
        додаткового запиту до біржі). Старіше max_age_seconds — не віримо (None):
        краще порожнє поле в аналітиці, ніж чужий equity."""
        rm = self.risk_manager
        last_equity = getattr(rm, 'last_equity', None) if rm else None
        if last_equity and time.time() - getattr(rm, 'last_equity_at', 0.0) <= max_age_seconds:
            return last_equity
        return None

    def _strategy_params_snapshot(self, strategy: Optional[str]) -> Optional[dict]:
        """Знімок активних параметрів стратегії станом на ЦЮ МИТЬ — пишеться в
        trade_analytics одноразово при відкритті і більше не змінюється, навіть
        якщо параметри потім поміняють у мініаппі."""
        if not strategy or not self.strategy_settings:
            return None
        try:
            return self.strategy_settings.get_params(strategy)
        except Exception as e:
            logger.warning(f"Failed to snapshot strategy params for {strategy}: {e}")
            return None

    async def _notify_error(self, error: str, context: str, critical: bool = False) -> None:
        await publish_error(self.event_bus, context=context, error=error, critical=critical, source="SimpleTrader")

    def _insert_position_record(self, position: dict) -> None:
        try:
            self.db.insert_position(
                order_id=position['order_id'],
                symbol=position['symbol'],
                side=position['side'],
                status='OPEN',
                metadata=dump_metadata(position),
            )
        except Exception as e:
            logger.error(f"Failed to save position to DB: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # відкриття
    # ------------------------------------------------------------------

    async def _handle_signal(self, event: Event) -> None:
        """Обробка сигналу від стратегії"""
        signal = event.data
        if signal.get('action') != 'OPEN':
            return

        # Kill-switch гейтить ЛИШЕ відкриття нових позицій — супровід уже
        # відкритих (SL/TP/trailing) від нього не залежить.
        if self.settings_manager is not None and not self.settings_manager.get_trading_enabled():
            logger.info(
                f"Signal for {signal.get('symbol')} ({signal.get('strategy')}) ignored — "
                f"trading is globally disabled (mini app toggle)"
            )
            return

        await self.open_position(
            symbol=signal['symbol'],
            side=signal['side'],
            quantity=signal['quantity'],
            risk_percent=signal.get('risk_percent'),
            trail_meta=signal.get('trail_meta'),
            signal_context=signal.get('analytics_context'),
            leverage=signal.get('leverage', 10),
            stop_loss_price=signal.get('stop_loss_price'),
            take_profit_levels=signal.get('take_profit_levels'),
            strategy=signal.get('strategy'),
            # ціна, від якої стратегія рахувала SL/TP — щоб перерахувати рівні
            # під фактичну ціну виконання ринкового ордера
            reference_price=signal.get('reference_price')
        )

    async def _resolve_quantity(
        self,
        symbol: str,
        side: str,
        quantity: Optional[float],
        leverage: int,
        stop_loss_price: Optional[float],
        reference_price: Optional[float],
        risk_percent: Optional[float],
    ) -> Optional[float]:
        """Розмір позиції. None — позицію відкривати НЕ можна."""
        if risk_percent:
            # Стратегія ЯВНО запросила ризик на угоду — працює незалежно від
            # глобального use_risk_based_sizing і БЕЗ fallback на quantity
            # стратегії (вона тут None): відкрити "якимось" розміром означало б
            # обійти саме той ліміт ризику, заради якого ця гілка існує.
            risk_quantity = None
            if self.risk_manager and stop_loss_price and reference_price:
                risk_quantity = await self.risk_manager.compute_risk_based_quantity(
                    entry_price=reference_price,
                    stop_loss_price=stop_loss_price,
                    risk_percent=risk_percent,
                    leverage=leverage,  # вмикає комісію в ризику і стелю маржі
                )
            if not risk_quantity or risk_quantity <= 0:
                logger.warning(
                    f"Risk-based sizing ({risk_percent}%) failed for {symbol} {side} — "
                    f"position NOT opened (no safe quantity)"
                )
                return None
            logger.info(f"Risk-based sizing for {symbol} {side}: {risk_percent}% risk -> quantity={risk_quantity}")
            return risk_quantity

        # Глобальний use_risk_based_sizing: при невдачі тихо лишаємось на
        # quantity стратегії — цей перерахунок ніколи не блокує відкриття.
        if (
            self.risk_manager
            and getattr(self.risk_manager, 'use_risk_based_sizing', False)
            and stop_loss_price
            and reference_price
        ):
            risk_quantity = await self.risk_manager.compute_risk_based_quantity(
                entry_price=reference_price,
                stop_loss_price=stop_loss_price,
            )
            if risk_quantity and risk_quantity > 0:
                logger.info(
                    f"Risk-based sizing for {symbol} {side}: "
                    f"strategy quantity={quantity} -> risk-based quantity={risk_quantity}"
                )
                return risk_quantity
            logger.warning(
                f"Risk-based sizing failed for {symbol} {side} — "
                f"falling back to strategy quantity={quantity}"
            )
        return quantity

    async def _place_entry_order(self, symbol: str, side: str, quantity: float, leverage: int):
        """Плече + MARKET-ордер. Повертає (order_id, response) або (None, None)."""
        try:
            await self.exchange.set_leverage(symbol, leverage, side=side)
        except BingXAPIError as e:
            logger.warning(
                f"set_leverage returned an error for {symbol} (leverage={leverage}, side={side}): "
                f"{e.code} {e.msg}. Continuing with order placement anyway."
            )

        try:
            response = await self.exchange.create_order(
                symbol=symbol,
                side=entry_order_side(side),
                order_type='MARKET',
                quantity=quantity
            )
        except BingXAPIError as e:
            if e.code == API_ORDERS_DISABLED_CODE and 'temporarily disabled' in e.msg:
                # обмеження біржі через волатильність, а не баг — лога достатньо
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
            return None, None

        logger.info(f"Exchange order response: {response}")
        order_id = extract_order_id(response)
        if not order_id:
            logger.error(f"Order accepted but no orderId found in response: {response}")
            return None, None
        return str(order_id), response

    async def open_position(
        self,
        symbol: str,
        side: str,
        quantity: Optional[float],
        leverage: int = 10,
        stop_loss_price: Optional[float] = None,
        take_profit_levels: Optional[list] = None,
        strategy: Optional[str] = None,
        reference_price: Optional[float] = None,
        risk_percent: Optional[float] = None,
        trail_meta: Optional[dict] = None,
        signal_context: Optional[dict] = None,
    ) -> bool:
        try:
            positions_info_message = "Позицію відкрито."
            if self.risk_manager:
                can_open, reason = await self.risk_manager.can_open_position(symbol)
                if not can_open:
                    logger.warning(f"Risk manager blocked {symbol} {side}: {reason}")
                    return False
                positions_info_message = reason or positions_info_message
            if strategy:
                positions_info_message = f"{positions_info_message} | Стратегія: {strategy}"

            quantity = await self._resolve_quantity(
                symbol, side, quantity, leverage, stop_loss_price, reference_price, risk_percent
            )
            if risk_percent and quantity is None:
                return False

            logger.info(f"Opening position: {symbol} {side} {quantity} (strategy={strategy})")

            order_id, response = await self._place_entry_order(symbol, side, quantity, leverage)
            if not order_id:
                return False

            entry_price, executed_qty = await resolve_entry_fill(
                self.exchange, symbol, side, order_id, response, quantity
            )
            if executed_qty != quantity:
                logger.info(
                    f"Using executedQty={executed_qty} instead of requested quantity={quantity} "
                    f"for {symbol} {side} SL/TP sizing"
                )

            stop_loss_price, take_profit_levels, scale = rescale_to_fill(
                stop_loss_price, take_profit_levels, reference_price, entry_price
            )
            if scale is not None:
                logger.info(
                    f"Rescaled SL/TP for {symbol} {side}: reference_price={reference_price}, "
                    f"entry_price={entry_price}, scale={scale:.6f}"
                )

            position = new_bot_position(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=executed_qty,
                entry_price=entry_price,
                leverage=leverage,
                stop_loss_price=stop_loss_price,
                take_profit_levels=take_profit_levels,
                strategy=strategy,
                trail_meta=trail_meta,
            )
            self.open_positions[position_key(symbol, side)] = position
            logger.info(f"Position tracked: orderId={order_id}, {symbol} {side}, strategy={strategy}")

            self._insert_position_record(position)
            self._insert_open_analytics(position, risk_percent, reference_price, signal_context)

            # SL/TP — на реально виконаний обсяг (executed_qty), а не запитаний
            if stop_loss_price:
                sl_order_id, sl_client_order_id = await self.orders.create_stop_loss(
                    symbol, side, executed_qty, stop_loss_price, entry_price
                )
                position['sl_order_id'] = sl_order_id
                position['sl_client_order_id'] = sl_client_order_id

            if take_profit_levels:
                tp_results = await self.orders.create_take_profit_orders(
                    symbol, side, executed_qty, take_profit_levels
                )
                position['tp_order_ids'] = [r['order_id'] for r in tp_results if r.get('order_id')]
                position['tp_client_order_ids'] = [
                    r['client_order_id'] for r in tp_results if r.get('client_order_id')
                ]

            persist_position_metadata(self.db, position, context="sl/tp order ids")

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
                    'margin_usdt': calc_margin_usdt(position),
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

    def _market_context(self, symbol: str) -> Optional[dict]:
        if self.market_context_provider is None:
            return None
        try:
            return self.market_context_provider(symbol)
        except Exception as e:
            logger.warning(f"market_context for {symbol} unavailable: {e}")
            return None

    def _insert_open_analytics(
        self,
        position: dict,
        risk_percent: Optional[float],
        reference_price: Optional[float],
        signal_context: Optional[dict],
    ) -> None:
        strategy = position['strategy']
        try:
            self.db.insert_trade_analytics(
                order_id=position['order_id'],
                symbol=position['symbol'],
                side=position['side'],
                opened_by='bot',
                entry_price=position['entry_price'],
                quantity=position['quantity'],
                leverage=position['leverage'],
                margin_usdt=calc_margin_usdt(position),
                strategy=strategy,
                strategy_params=self._strategy_params_snapshot(strategy),
                stop_loss_price_initial=position['stop_loss_price'],
                take_profit_levels_initial=position['take_profit_levels'],
                trail_meta=position['trail_meta'],
                risk_percent=risk_percent,
                reference_price=reference_price,
                equity_at_entry=self._recent_equity(),
                concurrent_positions=max(0, len(self.open_positions) - 1),
                signal_context=signal_context,
                market_context=self._market_context(position['symbol']),
            )
        except Exception as e:
            logger.error(
                f"Failed to insert trade_analytics for {position['symbol']} {position['side']}: {e}",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # виконання ордерів (WS ORDER_TRADE_UPDATE)
    # ------------------------------------------------------------------

    async def _handle_order_update(self, event: Event) -> None:
        logger.info(f"[DEBUG] Close ORDER_TRADE_UPDATE event: {event.data}")

        order_data = event.data.get('o', {})
        exchange_order_id = str(order_data.get('i'))
        client_order_id = order_data.get('c')  # clientOrderId, який ми самі задали для SL/TP
        order_type = order_data.get('o')
        symbol = order_data.get('s')
        position_side = order_data.get('ps')

        if order_data.get('X') != 'FILLED' or order_type not in ('STOP_MARKET', 'TAKE_PROFIT_MARKET', 'MARKET'):
            return

        key = position_key(symbol, position_side)
        position = self.open_positions.get(key)
        if not position:
            logger.debug(f"No open position tracked for {symbol} {position_side}, skipping")
            return

        # Виконання вхідного MARKET-ордера (того самого order_id, яким бот
        # відкривав позицію) — ЄДИНЕ місце, де видно комісію відкриття:
        # REST create_order/get_order її не повертає.
        if exchange_order_id == position.get('order_id'):
            self._capture_opening_commission(position, order_data, exchange_order_id)
            return

        if order_data.get('S') != close_order_side(position_side):
            return

        closed_by = 'bot' if self._is_bot_order(position, exchange_order_id, client_order_id) else 'user'
        trade_id = order_data.get('t')
        filled_qty = to_float(order_data.get('l', 0) or order_data.get('q', 0))
        trade_realized_pnl = to_float(order_data.get('rp', 0))
        commission = to_float(order_data.get('n', 0))

        if order_type == 'TAKE_PROFIT_MARKET':
            try:
                self.db.append_tp_fill(
                    order_id=position['order_id'],
                    tp_order_id=exchange_order_id,
                    client_order_id=client_order_id,
                    price=to_float(order_data.get('ap', 0)),
                    quantity=filled_qty,
                    realized_pnl=trade_realized_pnl,
                    commission=commission,
                    trade_id=trade_id,
                )
            except Exception as e:
                logger.error(f"Failed to append tp_fill to trade_analytics for {symbol} {position_side}: {e}", exc_info=True)

        # накопичуємо ID закриваючих угод — тільки ID, жодних цін/PnL
        if trade_id is not None:
            position.setdefault('closing_trade_ids', []).append(trade_id)
        position.setdefault('closing_orders', []).append({
            'order_id': exchange_order_id,
            'client_order_id': client_order_id,
            'closed_by': closed_by
        })

        position['realized_pnl_accum'] = position.get('realized_pnl_accum', 0.0) + trade_realized_pnl
        position['commission_close_accum'] = position.get('commission_close_accum', 0.0) + commission
        if order_data.get('N'):
            position['commission_asset'] = order_data.get('N')

        remaining_quantity = position.get('remaining_quantity')
        if remaining_quantity is None:
            remaining_quantity = position.get('quantity', 0)
        position['remaining_quantity'] = max(0.0, remaining_quantity - filled_qty)

        logger.info(
            f"Partial/full close fill: {symbol} {position_side}, order={exchange_order_id}, "
            f"client_order_id={client_order_id}, trade_id={trade_id}, filled_qty={filled_qty}, "
            f"trade_pnl={trade_realized_pnl}, commission={commission}, "
            f"remaining={position['remaining_quantity']:.8f}, closed_by={closed_by}"
        )

        persist_position_metadata(self.db, position, context="partial close")

        is_full_close = order_type == 'STOP_MARKET' or position['remaining_quantity'] <= 1e-8
        if not is_full_close:
            # позиція ще частково відкрита — НЕ видаляємо, НЕ закриваємо в БД
            return

        del self.open_positions[key]
        logger.info(f"Position fully closed: {symbol} {position_side}, closed_by={closed_by}")

        await self._finalize_close(
            position,
            position_side=position_side,
            close_price=to_float(order_data.get('ap', 0)),
            realized_pnl=position.get('realized_pnl_accum', 0.0),
            closed_by=closed_by,
            close_reason=_CLOSE_REASON_BY_ORDER_TYPE.get(order_type, 'manual'),
            close_order_type=order_type,
        )

    @staticmethod
    def _is_bot_order(position: dict, exchange_order_id: str, client_order_id: Optional[str]) -> bool:
        known_client_ids = set(position.get('tp_client_order_ids', []) or [])
        known_order_ids = set(position.get('tp_order_ids', []) or [])
        if position.get('sl_client_order_id'):
            known_client_ids.add(position['sl_client_order_id'])
        if position.get('sl_order_id'):
            known_order_ids.add(position['sl_order_id'])
        return (bool(client_order_id) and client_order_id in known_client_ids) or exchange_order_id in known_order_ids

    def _capture_opening_commission(self, position: dict, order_data: dict, exchange_order_id: str) -> None:
        commission_open = to_float(order_data.get('n', 0))
        position['commission_open_accum'] = position.get('commission_open_accum', 0.0) + commission_open
        if order_data.get('N'):
            position['commission_asset'] = order_data.get('N')
        persist_position_metadata(self.db, position, context="opening commission")
        logger.info(
            f"Captured opening commission for {position['symbol']} {position['side']}: "
            f"{commission_open} (order={exchange_order_id})"
        )

    # ------------------------------------------------------------------
    # ACCOUNT_UPDATE — safety net для ручних дій на біржі
    # ------------------------------------------------------------------

    async def _handle_account_update(self, event: Event) -> None:
        """Ловить ручні дії на біржі, які не пройшли через ORDER_TRADE_UPDATE обробник"""
        positions_data = event.data.get('a', {}).get('P', [])
        logger.info(f"[WS] Publishing HANDLE_ACCOUNT_INFO_UPDATE event: {event}")
        logger.info(f"[WS] Publishing HANDLE_ACCOUNT_INFO_UPDATE positions_data: {positions_data}")

        for pos in positions_data:
            symbol = pos.get('s')
            position_side = pos.get('ps')
            amount = to_float(pos.get('pa', 0))
            if not symbol or not position_side:
                continue

            key = position_key(symbol, position_side)
            existing = self.open_positions.get(key)

            if amount != 0 and not existing:
                await self._track_manual_position(symbol, position_side, to_float(pos.get('ep', 0)), abs(amount))

            elif amount == 0 and existing:
                if existing.get('opened_by') != 'user':
                    # позиція бота — закриття вже (або вот-вот) обробить
                    # _handle_order_update по ORDER_TRADE_UPDATE, не дублюємо
                    logger.debug(
                        f"Skipping account-update close for bot position {symbol} {position_side} "
                        f"— expecting ORDER_TRADE_UPDATE to handle it"
                    )
                    return

                del self.open_positions[key]
                logger.info(f"Position closed (detected via account update): {symbol} {position_side}")

                # До PnL, вже накопиченого з часткових закриттів через
                # ORDER_TRADE_UPDATE, додаємо 'cr' — PnL САМЕ цього (останнього)
                # закриття, яке через ORDER_TRADE_UPDATE не пройшло.
                realized_pnl = existing.get('realized_pnl_accum', 0.0) + to_float(pos.get('cr', 0))
                await self._finalize_close(
                    existing,
                    position_side=position_side,
                    close_price=await self._derive_close_price(existing, position_side, realized_pnl),
                    realized_pnl=realized_pnl,
                    closed_by=existing.get('opened_by', 'user'),
                    close_reason='manual',
                    close_order_type=None,
                )

    async def _track_manual_position(self, symbol: str, side: str, entry_price: float, quantity: float) -> None:
        """Позиція відкрита вручну на біржі — бот про неї не знав."""
        position = new_manual_position(
            order_id=f"manual-{symbol}-{side}-{int(time.time())}",
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            quantity=quantity,
        )
        self.open_positions[position_key(symbol, side)] = position
        logger.info(f"Manual position detected and tracked: {symbol} {side}")

        self._insert_position_record(position)
        margin_usdt = calc_margin_usdt(position)

        try:
            self.db.insert_trade_analytics(
                order_id=position['order_id'],
                symbol=symbol,
                side=side,
                opened_by='user',
                entry_price=entry_price,
                quantity=quantity,
                margin_usdt=margin_usdt,
            )
        except Exception as e:
            logger.error(f"Failed to insert trade_analytics for manual {symbol} {side}: {e}", exc_info=True)

        await self.event_bus.publish(Event(
            type=EventType.POSITION_OPENED,
            data={
                'symbol': symbol,
                'side': side,
                'entry_price': entry_price,
                'quantity': quantity,
                'leverage': 0,
                'stop_loss_price': None,
                'margin_usdt': margin_usdt,
                'positions_info_message': "Позицію відкрито вручну."
            }
        ))

    async def _derive_close_price(self, position: dict, side: str, realized_pnl: float) -> float:
        """close_price виводимо з entry_price/quantity/сукупного realized_pnl, а
        не з поточного тікера (він уже може відрізнятись від фактичної ціни
        виконання закриваючого ордера). Тікер — лише якщо даних немає."""
        entry_price = position.get('entry_price')
        quantity = position.get('quantity')
        if entry_price and quantity and quantity > 0:
            per_unit = realized_pnl / quantity
            return entry_price + per_unit if side == 'LONG' else entry_price - per_unit
        try:
            return await self.exchange.get_ticker_price(position['symbol'])
        except Exception as e:
            logger.warning(f"Failed to fetch close_price ticker for {position['symbol']}: {e}")
            return 0.0

    # ------------------------------------------------------------------
    # фінал закриття — спільний для ORDER_TRADE_UPDATE і ACCOUNT_UPDATE
    # ------------------------------------------------------------------

    async def _finalize_close(
        self,
        position: dict,
        *,
        position_side: str,
        close_price: float,
        realized_pnl: float,
        closed_by: str,
        close_reason: str,
        close_order_type: Optional[str],
    ) -> None:
        symbol = position['symbol']
        commission_open = position.get('commission_open_accum', 0.0)
        commission_close = position.get('commission_close_accum', position.get('commission_accum', 0.0))
        funding_fee = position.get('funding_fee_accum', 0.0)
        commission_total = commission_open + commission_close
        net_pnl = calc_net_pnl(realized_pnl, commission_open, commission_close, funding_fee)
        margin_usdt = calc_margin_usdt(position)
        roe_percent = calc_roe_percent(position, realized_pnl)

        try:
            self.db.update_position_status(
                order_id=position['order_id'],
                status='CLOSED',
                closed_at=datetime.utcnow(),
                close_price=close_price,
                realized_pnl=realized_pnl,
                roe_percent=roe_percent,
                commission_usdt=commission_total,
                commission_open=commission_open,
                commission_close=commission_close,
                funding_fee=funding_fee,
                net_pnl=net_pnl,
                margin_usdt=margin_usdt,
            )
        except Exception as e:
            logger.error(f"Failed to update position status in DB: {e}", exc_info=True)

        try:
            self.db.close_trade_analytics(
                order_id=position['order_id'],
                close_price=close_price,
                realized_pnl=realized_pnl,
                commission_open=commission_open,
                commission_close=commission_close,
                funding_fee=funding_fee,
                net_pnl=net_pnl,
                roe_percent=roe_percent,
                margin_usdt=margin_usdt,
                closed_by=closed_by,
                close_reason=close_reason,
                quantity=position.get('quantity'),
            )
        except Exception as e:
            logger.error(f"Failed to close trade_analytics for {symbol} {position_side}: {e}", exc_info=True)

        strategy = position.get('strategy')
        await self.event_bus.publish(Event(
            type=EventType.POSITION_CLOSED,
            data={
                'symbol': symbol,
                'side': position_side,
                'close_price': close_price,
                'realized_pnl': realized_pnl,
                'commission_usdt': commission_total,
                'commission_open': commission_open,
                'commission_close': commission_close,
                'funding_fee': funding_fee,
                'net_pnl': net_pnl,
                'margin_usdt': margin_usdt,
                'roe_percent': roe_percent,
                'closed_by': closed_by,
                # 'STOP_MARKET' | 'TAKE_PROFIT_MARKET' | 'MARKET' | None (ручне
                # закриття з ACCOUNT_UPDATE) — по ньому RiskManager рахує серію стопів
                'close_order_type': close_order_type,
                'order_id': position.get('order_id'),
                'entry_price': position.get('entry_price'),
                'quantity': position.get('quantity'),
                'leverage': position.get('leverage'),
                'strategy': strategy,
                'positions_info_message': f"Стратегія: {strategy}" if strategy else None,
            }
        ))
