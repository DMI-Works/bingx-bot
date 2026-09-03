import logging
from typing import Optional
from datetime import datetime

from ..database import Database
from ..events import EventBus, Event, EventType


logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, db: Database, event_bus: EventBus, exchange, config: dict):
        self.db = db
        self.event_bus = event_bus
        self.exchange = exchange  # BingXClient — нужен для получения реальных позиций
        self.config = config

        self.max_open_positions = config.get('max_open_positions', 3)
        self.max_positions_per_symbol = config.get('max_positions_per_symbol', 1)
        self.max_total_risk_percent = config.get('max_total_risk_percent', 5.0)
        self.max_consecutive_losses = config.get('max_consecutive_losses', 3)
        self.cooldown_after_trade_seconds = config.get('cooldown_after_trade_seconds', 60)

        # --- risk-based position sizing (заменяет fixed position_size из стратегии) ---
        # По умолчанию ВЫКЛЮЧЕНО (use_risk_based_sizing=False) — включать
        # осознанно в config.yaml после того, как проверишь risk_per_trade_percent
        # на своём реальном балансе, иначе бот молча начнёт торговать другими
        # объёмами, чем раньше.
        self.use_risk_based_sizing = config.get('use_risk_based_sizing', False)
        self.risk_per_trade_percent = config.get('risk_per_trade_percent', 0.5)

        self.consecutive_losses = 0
        self.last_trade_time: Optional[datetime] = None

        # события всё ещё нужны для cooldown/consecutive_losses, но НЕ для счёта открытых позиций
        self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_closed_event)

        logger.info(
            "RiskManager initialized (open positions count comes live from exchange); "
            f"risk_based_sizing={'ON' if self.use_risk_based_sizing else 'OFF'} "
            f"({self.risk_per_trade_percent}% equity/trade if ON)"
        )

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

        return equity

    async def compute_risk_based_quantity(
        self, entry_price: float, stop_loss_price: float, risk_percent: Optional[float] = None
    ) -> Optional[float]:
        """
        quantity = (equity * risk_percent%) / |entry_price - stop_loss_price|

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
        quantity = risk_usdt / distance

        logger.info(
            f"RiskManager: risk-based sizing: equity={equity:.2f}, risk={pct}% -> "
            f"risk_usdt={risk_usdt:.4f}, sl_distance={distance:.6f} -> quantity={quantity:.8f}"
        )
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

    async def can_open_position(self, symbol: str, risk_amount: float = 0.0) -> tuple[bool, Optional[str]]:
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

        if self.max_consecutive_losses > 0 and self.consecutive_losses >= self.max_consecutive_losses:
            reason = f"Max consecutive losses reached: {self.consecutive_losses}/{self.max_consecutive_losses}"
            logger.warning(reason)
            return False, reason

        if self.last_trade_time:
            time_since_last_trade = (datetime.utcnow() - self.last_trade_time).total_seconds()
            if time_since_last_trade < self.cooldown_after_trade_seconds:
                reason = f"Cooldown active: {self.cooldown_after_trade_seconds - int(time_since_last_trade)}s remaining"
                logger.warning(reason)
                return False, reason

        return True, None

    def position_closed(self, pnl: float, symbol: Optional[str] = None) -> None:
        """Считаем win/loss серию и cooldown по времени — это НЕ связано с количеством позиций,
        поэтому оставляем как событийную логику."""
        self.last_trade_time = datetime.utcnow()

        if pnl < 0:
            self.consecutive_losses += 1
            logger.info(f"Loss recorded. Consecutive losses: {self.consecutive_losses}")
        else:
            self.consecutive_losses = 0
            logger.info("Win recorded. Consecutive losses reset to 0")

    async def _on_position_closed_event(self, event: Event) -> None:
        pnl = event.data.get('realized_pnl', 0.0)
        symbol = event.data.get('symbol')
        self.position_closed(pnl=pnl, symbol=symbol)

    def reset_consecutive_losses(self) -> None:
        self.consecutive_losses = 0
        logger.info("Consecutive losses manually reset")

    def update_config(self, config: dict) -> None:
        self.max_open_positions = config.get('max_open_positions', self.max_open_positions)
        self.max_positions_per_symbol = config.get('max_positions_per_symbol', self.max_positions_per_symbol)
        self.max_total_risk_percent = config.get('max_total_risk_percent', self.max_total_risk_percent)
        self.max_consecutive_losses = config.get('max_consecutive_losses', self.max_consecutive_losses)
        self.cooldown_after_trade_seconds = config.get('cooldown_after_trade_seconds', self.cooldown_after_trade_seconds)
        self.use_risk_based_sizing = config.get('use_risk_based_sizing', self.use_risk_based_sizing)
        self.risk_per_trade_percent = config.get('risk_per_trade_percent', self.risk_per_trade_percent)
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
            'consecutive_losses': self.consecutive_losses,
            'max_consecutive_losses': self.max_consecutive_losses,
            'cooldown_active': self._is_cooldown_active(),
            'last_trade_time': self.last_trade_time.isoformat() if self.last_trade_time else None
        }

    def _is_cooldown_active(self) -> bool:
        if not self.last_trade_time:
            return False
        time_since_last_trade = (datetime.utcnow() - self.last_trade_time).total_seconds()
        return time_since_last_trade < self.cooldown_after_trade_seconds