import logging
import time
from typing import Optional

from ..database import Database
from ..events import EventBus, Event, EventType


logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, db: Database, event_bus: EventBus, exchange, config: dict, settings_manager=None):
        self.db = db
        self.event_bus = event_bus
        self.exchange = exchange  # BingXClient — нужен для получения реальных позиций
        # SettingsManager — через него монета, набравшая max_consecutive_losses
        # убытков подряд, добавляется в чёрный список (хранится в БД, тот же
        # список, что и в мини-аппе → вкладка «Монеты»)
        self.settings_manager = settings_manager
        self.config = config

        self.max_open_positions = config.get('max_open_positions', 3)
        self.max_positions_per_symbol = config.get('max_positions_per_symbol', 1)
        self.max_total_risk_percent = config.get('max_total_risk_percent', 5.0)
        self.max_consecutive_losses = config.get('max_consecutive_losses', 3)

        # --- risk-based position sizing (заменяет fixed position_size из стратегии) ---
        # По умолчанию ВЫКЛЮЧЕНО (use_risk_based_sizing=False) — включать
        # осознанно в config.yaml после того, как проверишь risk_per_trade_percent
        # на своём реальном балансе, иначе бот молча начнёт торговать другими
        # объёмами, чем раньше.
        self.use_risk_based_sizing = config.get('use_risk_based_sizing', False)
        self.risk_per_trade_percent = config.get('risk_per_trade_percent', 0.5)

        # Счётчики серий убытков по монетам. Сохраняются в БД (settings-ключ
        # CONSECUTIVE_LOSSES_KEY), поэтому переживают рестарт бота.
        self.consecutive_losses: dict[str, int] = self._load_consecutive_losses()

        # --- предохранитель "серия стоп-лоссов": N стопов В МИНУС подряд за
        # window часов -> блок НОВЫХ входов на block часов. Глобальный (по
        # аккаунту, не по монете): в отличие от max_consecutive_losses выше,
        # который выкидывает из торговли одну монету. 0 = выключено.
        # Стеля маржі на одну угоду (% від equity) і комісія taker однієї
        # сторони — див. compute_risk_based_quantity. 0 = стелю вимкнено.
        self.max_margin_percent_per_trade = config.get('max_margin_percent_per_trade', 5.0)
        self.taker_fee_rate = config.get('taker_fee_rate', 0.0005)

        # Останній успішно отриманий equity і коли — SimpleTrader кладе його в
        # trade_analytics (equity_at_entry), щоб не робити зайвий запит до біржі.
        self.last_equity: Optional[float] = None
        self.last_equity_at: float = 0.0

        self.stop_loss_streak_limit = config.get('stop_loss_streak_limit', 3)
        self.stop_loss_streak_window_hours = config.get('stop_loss_streak_window_hours', 24)
        self.stop_loss_block_hours = config.get('stop_loss_block_hours', 8)
        self._sl_streak: list[float] = []
        self._entry_blocked_until: float = 0.0
        self._load_stop_loss_state()

        # события нужны для серий убытков по монетам, но НЕ для счёта открытых позиций
        self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_closed_event)

        logger.info(
            "RiskManager initialized (open positions count comes live from exchange); "
            f"risk_based_sizing={'ON' if self.use_risk_based_sizing else 'OFF'} "
            f"({self.risk_per_trade_percent}% equity/trade if ON)"
        )

    CONSECUTIVE_LOSSES_KEY = 'risk.consecutive_losses'
    STOP_LOSS_STREAK_KEY = 'risk.stop_loss_streak'

    def _load_stop_loss_state(self) -> None:
        if self.settings_manager is None:
            return
        try:
            raw = dict(self.settings_manager.get(self.STOP_LOSS_STREAK_KEY, {}) or {})
            self._sl_streak = [float(t) for t in raw.get('streak', [])]
            self._entry_blocked_until = float(raw.get('blocked_until', 0.0))
        except Exception as e:
            logger.error(f"Failed to load stop-loss streak state from DB, starting from zero: {e}", exc_info=True)
            self._sl_streak = []
            self._entry_blocked_until = 0.0
            return
        if self._entry_blocked_until > time.time():
            logger.warning(
                f"Restored entry block from DB: {(self._entry_blocked_until - time.time()) / 3600:.1f}h left"
            )

    async def _save_stop_loss_state(self) -> None:
        if self.settings_manager is None:
            return
        try:
            await self.settings_manager.set(self.STOP_LOSS_STREAK_KEY, {
                'streak': list(self._sl_streak),
                'blocked_until': self._entry_blocked_until,
            })
        except Exception as e:
            logger.error(f"Failed to persist stop-loss streak state: {e}", exc_info=True)

    def get_entry_block_remaining(self) -> float:
        """Сколько секунд ещё действует блок входов (0 — не заблокировано)."""
        return max(0.0, self._entry_blocked_until - time.time())

    async def _register_closed_trade(self, pnl: float, close_order_type: Optional[str]) -> None:
        """
        Серия = подряд идущие закрытия СТОПОМ В МИНУС (STOP_MARKET и net_pnl < 0).
        Любое другое закрытие (стоп в плюс/безубыток, TP, ручное, MARKET)
        серию обрывает. Старше window часов отсекаются.
        """
        if self.stop_loss_streak_limit <= 0:
            return

        is_stop_loss = close_order_type == 'STOP_MARKET' and pnl < 0
        if not is_stop_loss:
            if self._sl_streak:
                self._sl_streak.clear()
                await self._save_stop_loss_state()
            return

        now = time.time()
        window = self.stop_loss_streak_window_hours * 3600
        self._sl_streak = [t for t in self._sl_streak if now - t <= window]
        self._sl_streak.append(now)
        logger.info(
            f"Stop-loss recorded (net pnl {pnl:.6f}). Streak: "
            f"{len(self._sl_streak)}/{self.stop_loss_streak_limit} within {self.stop_loss_streak_window_hours}h"
        )

        if len(self._sl_streak) >= self.stop_loss_streak_limit:
            streak_len = len(self._sl_streak)
            self._entry_blocked_until = now + self.stop_loss_block_hours * 3600
            self._sl_streak.clear()
            await self._save_stop_loss_state()
            logger.warning(
                f"{streak_len} stop-losses in a row within {self.stop_loss_streak_window_hours}h — "
                f"NEW ENTRIES BLOCKED for {self.stop_loss_block_hours}h"
            )
            await self.event_bus.publish(Event(
                type=EventType.ERROR,
                data={
                    'context': (
                        f"🛑 {streak_len} стоп-лоси підряд за {self.stop_loss_streak_window_hours}г — "
                        f"нові входи заблоковано на {self.stop_loss_block_hours}г. "
                        f"Відкриті позиції продовжують супроводжуватись."
                    ),
                    'error': '',
                },
                source='RiskManager',
            ))
        else:
            await self._save_stop_loss_state()

    def _load_consecutive_losses(self) -> dict[str, int]:
        if self.settings_manager is None:
            return {}
        try:
            raw = self.settings_manager.get(self.CONSECUTIVE_LOSSES_KEY, {})
            loaded = {str(sym): int(n) for sym, n in dict(raw).items() if int(n) > 0}
        except Exception as e:
            logger.error(f"Failed to load consecutive losses from DB, starting from zero: {e}", exc_info=True)
            return {}
        if loaded:
            logger.info(f"Restored consecutive losses from DB: {loaded}")
        return loaded

    async def _save_consecutive_losses(self) -> None:
        if self.settings_manager is None:
            return
        try:
            await self.settings_manager.set(self.CONSECUTIVE_LOSSES_KEY, dict(self.consecutive_losses))
        except Exception as e:
            # не роняем обработку закрытия позиции из-за сбоя записи в БД
            logger.error(f"Failed to persist consecutive losses: {e}", exc_info=True)

    async def get_equity(self) -> Optional[float]:
        """Текущий капитал (equity) аккаунта в USDT, нужен для risk-based sizing.
        Возвращает None, если получить не удалось (fail-safe — вызывающий код
        должен в этом случае НЕ открывать позицию risk-based методом)."""
        try:
            balance_data = await self.exchange.get_account_balance()
        except Exception as e:
            logger.error(f"RiskManager: failed to fetch account balance for sizing: {e}", exc_info=True)
            return None

        if not balance_data or balance_data.get('code') != 0 or 'data' not in balance_data:
            logger.error(f"RiskManager: unexpected balance response for sizing: {balance_data}")
            return None

        balance = balance_data['data'].get('balance', {})
        try:
            equity = float(balance.get('equity', 0))
        except (TypeError, ValueError):
            equity = 0.0

        if equity <= 0:
            logger.error(f"RiskManager: got non-positive equity ({equity}) — cannot size position")
            return None

        self.last_equity = equity
        self.last_equity_at = time.time()
        return equity

    async def compute_risk_based_quantity(
        self, entry_price: float, stop_loss_price: float, risk_percent: Optional[float] = None,
        leverage: Optional[float] = None,
    ) -> Optional[float]:
        """
        quantity = (equity * risk_percent%) / (|entry_price - stop_loss_price| + комісія round-trip на 1 шт.)

        Два захисти (працюють, лише коли передано leverage — тобто для
        ЯВНОГО risk_percent зі стратегії; старий шлях use_risk_based_sizing
        викликає без leverage і поводиться як раніше):
          1) комісія входить у ризик: при тісному стопі (порівнянному з
             комісією) без цього реальний збиток — у рази більший за заплановані
             risk_percent. Комісія = 2 * taker_fee_rate * entry_price на одиницю;
          2) стеля розміру: маржа позиції <= max_margin_percent_per_trade % від
             equity. Без неї тісний стоп роздуває позицію до майже всього
             депозиту (так на тестнеті NCFXGBP2USD взяла 94% equity під 1% ризику).

        entry_price здесь — reference_price сигнала (цена, от которой стратегия
        считала SL/TP), т.к. для MARKET-ордера реальная entry_price появится
        только ПОСЛЕ отправки ордера, а quantity нужен ДО. Это единственная
        точка компромисса: сайзинг чуть менее точен при проскальзывании, но
        уже строго ограничен фильтром max_spread_percent на выбор символов.

        Возвращает None при любой невозможности посчитать риск честно —
        вызывающий код обязан в этом случае НЕ открывать позицию risk-based
        методом (упасть обратно на старый quantity — небезопасно тихо
        менять смысл того, что запросила стратегия).
        """
        if entry_price is None or stop_loss_price is None or entry_price <= 0:
            logger.warning(
                f"RiskManager: cannot compute risk-based quantity — "
                f"invalid entry_price={entry_price} or stop_loss_price={stop_loss_price}"
            )
            return None

        distance = abs(entry_price - stop_loss_price)
        if distance <= 0:
            logger.warning(
                f"RiskManager: cannot compute risk-based quantity — "
                f"stop_loss_price equals entry_price (distance=0)"
            )
            return None

        equity = await self.get_equity()
        if equity is None:
            return None

        pct = risk_percent if risk_percent is not None else self.risk_per_trade_percent
        risk_usdt = equity * (pct / 100.0)

        fee_per_unit = 2.0 * self.taker_fee_rate * entry_price if leverage else 0.0
        quantity = risk_usdt / (distance + fee_per_unit)

        logger.info(
            f"RiskManager: risk-based sizing: equity={equity:.2f}, risk={pct}% -> "
            f"risk_usdt={risk_usdt:.4f}, sl_distance={distance:.6f}, fee_per_unit={fee_per_unit:.6f} "
            f"-> quantity={quantity:.8f}"
        )

        if leverage and self.max_margin_percent_per_trade > 0:
            max_margin = equity * (self.max_margin_percent_per_trade / 100.0)
            max_quantity = max_margin * leverage / entry_price
            if quantity > max_quantity:
                logger.warning(
                    f"RiskManager: quantity {quantity:.8f} capped to {max_quantity:.8f} — margin would be "
                    f"{quantity * entry_price / leverage:.2f} USDT "
                    f"(> {self.max_margin_percent_per_trade}% of equity = {max_margin:.2f} USDT); "
                    f"actual risk is lower than requested {pct}%"
                )
                quantity = max_quantity
        return quantity

    async def _get_real_open_positions(self) -> list[dict]:
        """
        Запрашивает реальные открытые позиции напрямую с биржи через
        BingXClient.get_positions() (/openApi/swap/v2/user/positions).
        Позиция считается открытой, если positionAmt != 0.
        """
        try:
            raw_positions = await self.exchange.get_positions()
        except Exception as e:
            logger.error(f"Failed to fetch open positions from exchange: {e}", exc_info=True)
            # Fail-safe: если биржа недоступна — лучше НЕ разрешать открытие новых позиций,
            # чем открыть их вслепую при рассинхроне
            raise

        open_positions = []
        for pos in raw_positions:
            try:
                amt = float(pos.get('positionAmt', 0))
            except (TypeError, ValueError):
                amt = 0.0
            if amt != 0:
                open_positions.append(pos)

        return open_positions

    def _is_blacklisted(self, symbol: str) -> bool:
        if self.settings_manager is None:
            return False
        return symbol in self.settings_manager.get_blacklist_symbols()

    async def can_open_position(self, symbol: str, risk_amount: float = 0.0) -> tuple[bool, Optional[str]]:

        if self._is_blacklisted(symbol):
            reason = f"{symbol} is blacklisted — removed from trading"
            logger.warning(reason)
            return False, reason

        block_remaining = self.get_entry_block_remaining()
        if block_remaining > 0:
            reason = (
                f"New entries blocked by stop-loss streak breaker: "
                f"{block_remaining / 3600:.1f}h left"
            )
            logger.warning(reason)
            return False, reason

        try:
            real_positions = await self._get_real_open_positions()
        except Exception as e:
            reason = f"Cannot verify open positions via exchange API: {e}"
            logger.warning(reason)
            return False, reason

        current_open_positions = len(real_positions)
        open_positions_by_symbol: dict[str, int] = {}
        for pos in real_positions:
            pos_symbol = pos.get('symbol')
            if pos_symbol:
                open_positions_by_symbol[pos_symbol] = open_positions_by_symbol.get(pos_symbol, 0) + 1

        logger.info(
            f"Live positions from exchange: {current_open_positions} total, "
            f"by symbol: {open_positions_by_symbol}"
        )

        if current_open_positions >= self.max_open_positions:
            reason = f"Max open positions reached: {self.max_open_positions}"
            logger.warning(reason)
            return False, reason

        if open_positions_by_symbol.get(symbol, 0) >= self.max_positions_per_symbol:
            reason = f"Max positions per symbol reached for {symbol}: {self.max_positions_per_symbol}"
            logger.warning(reason)
            return False, reason

        return True, None

    async def position_closed(self, pnl: float, symbol: Optional[str] = None) -> None:
        """Считаем win/loss серию — ОТДЕЛЬНО по каждому символу. `pnl` — ЧИСТЫЙ
        результат сделки (после комиссии, net_pnl): сделка, которая в плюсе до
        комиссии, но в минусе после неё, считается убытком.

        Как только по монете набралось max_consecutive_losses убытков подряд —
        монета УБИРАЕТСЯ из торговли (чёрный список + отписка от WS), а не
        ставится на паузу по таймеру. Вернуть её можно только вручную
        (мини-апп → Монеты → чёрный список)."""
        if symbol is None:
            logger.warning("position_closed() called without symbol — skipping per-symbol loss tracking")
            return

        if pnl < 0:
            if self._is_blacklisted(symbol):
                logger.info(f"Loss on already blacklisted {symbol} — not counting")
                return

            self.consecutive_losses[symbol] = self.consecutive_losses.get(symbol, 0) + 1
            losses = self.consecutive_losses[symbol]
            logger.info(f"Loss recorded for {symbol} (net pnl {pnl:.6f}). Consecutive losses: {losses}")

            if self.max_consecutive_losses > 0 and losses >= self.max_consecutive_losses:
                await self._remove_symbol_from_trading(symbol, losses)
                return  # счётчик уже сохранён внутри

            await self._save_consecutive_losses()
        else:
            if self.consecutive_losses.pop(symbol, 0):
                logger.info(f"Win recorded for {symbol} (net pnl {pnl:.6f}). Consecutive losses reset to 0")
                await self._save_consecutive_losses()

    async def _remove_symbol_from_trading(self, symbol: str, losses: int) -> None:
        if self.settings_manager is None:
            logger.error(
                f"Consecutive losses limit reached for {symbol} ({losses}/{self.max_consecutive_losses}), "
                f"but RiskManager has no settings_manager — cannot blacklist the symbol"
            )
            return

        try:
            await self.settings_manager.add_blacklist_symbol(symbol)
        except Exception as e:
            # счётчик НЕ сбрасываем — при следующем убытке попробуем снова
            logger.error(f"Failed to blacklist {symbol} after {losses} consecutive losses: {e}", exc_info=True)
            return

        # Счётчик обнуляем: если монету потом вернут вручную, серия начнётся
        # с нуля, а не забанит её снова после первого же убытка.
        self.consecutive_losses.pop(symbol, None)
        await self._save_consecutive_losses()

        logger.warning(
            f"Consecutive losses limit reached for {symbol} ({losses}/{self.max_consecutive_losses}) "
            f"— {symbol} REMOVED from trading (blacklisted)"
        )
        # Слушают: SymbolSelector (отписаться от WS) и TelegramBot (уведомление)
        await self.event_bus.publish(Event(
            type=EventType.SYMBOL_BLACKLISTED,
            data={
                'symbol': symbol,
                'reason': 'consecutive_losses',
                'consecutive_losses': losses,
                'max_consecutive_losses': self.max_consecutive_losses,
            },
            source='RiskManager',
        ))

    async def _on_position_closed_event(self, event: Event) -> None:
        # Считаем по net_pnl (после комиссии). Если по какой-то причине его нет
        # в событии — откатываемся на realized_pnl.
        pnl = event.data.get('net_pnl')
        if pnl is None:
            pnl = event.data.get('realized_pnl', 0.0)
        symbol = event.data.get('symbol')
        await self._register_closed_trade(pnl, event.data.get('close_order_type'))
        await self.position_closed(pnl=pnl, symbol=symbol)

    async def reset_consecutive_losses(self, symbol: Optional[str] = None) -> None:
        """Без symbol — сброс счётчиков по всем монетам; с symbol — только по
        конкретной. Из чёрного списка монету это НЕ убирает (см. мини-апп)."""
        if symbol is None:
            self.consecutive_losses.clear()
            logger.info("Consecutive losses manually reset for all symbols")
        else:
            self.consecutive_losses.pop(symbol, None)
            logger.info(f"Consecutive losses manually reset for {symbol}")
        await self._save_consecutive_losses()

    def update_config(self, config: dict) -> None:
        self.max_open_positions = config.get('max_open_positions', self.max_open_positions)
        self.max_positions_per_symbol = config.get('max_positions_per_symbol', self.max_positions_per_symbol)
        self.max_total_risk_percent = config.get('max_total_risk_percent', self.max_total_risk_percent)
        self.max_consecutive_losses = config.get('max_consecutive_losses', self.max_consecutive_losses)
        self.use_risk_based_sizing = config.get('use_risk_based_sizing', self.use_risk_based_sizing)
        self.risk_per_trade_percent = config.get('risk_per_trade_percent', self.risk_per_trade_percent)
        self.max_margin_percent_per_trade = config.get(
            'max_margin_percent_per_trade', self.max_margin_percent_per_trade
        )
        self.taker_fee_rate = config.get('taker_fee_rate', self.taker_fee_rate)
        self.stop_loss_streak_limit = config.get('stop_loss_streak_limit', self.stop_loss_streak_limit)
        self.stop_loss_streak_window_hours = config.get(
            'stop_loss_streak_window_hours', self.stop_loss_streak_window_hours
        )
        self.stop_loss_block_hours = config.get('stop_loss_block_hours', self.stop_loss_block_hours)
        logger.info("Risk config updated")

    async def get_status(self) -> dict:
        try:
            real_positions = await self._get_real_open_positions()
            current_open_positions = len(real_positions)
            open_positions_by_symbol: dict[str, int] = {}
            for pos in real_positions:
                pos_symbol = pos.get('symbol')
                if pos_symbol:
                    open_positions_by_symbol[pos_symbol] = open_positions_by_symbol.get(pos_symbol, 0) + 1
        except Exception:
            current_open_positions = -1  # сигнал, что не удалось получить данные с биржи
            open_positions_by_symbol = {}

        return {
            'current_open_positions': current_open_positions,
            'max_open_positions': self.max_open_positions,
            'open_positions_by_symbol': open_positions_by_symbol,
            'max_consecutive_losses': self.max_consecutive_losses,
            # По каждому символу: сколько подряд убытков сейчас. Достигшие
            # лимита монеты не «на паузе», а убраны в чёрный список.
            'consecutive_losses_by_symbol': dict(self.consecutive_losses),
            'blacklisted_symbols': list(self.settings_manager.get_blacklist_symbols()) if self.settings_manager else [],
            'stop_loss_streak': len(self._sl_streak),
            'entry_block_remaining_seconds': self.get_entry_block_remaining(),
        }
