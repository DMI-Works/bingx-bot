import logging
from typing import Optional
from datetime import datetime, timedelta

from ..database import Database
from ..events import EventBus, Event, EventType


logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, db: Database, event_bus: EventBus, config: dict):
        self.db = db
        self.event_bus = event_bus
        self.config = config

        self.max_open_positions = config.get('max_open_positions', 3)
        self.max_positions_per_symbol = config.get('max_positions_per_symbol', 1)
        self.max_total_risk_percent = config.get('max_total_risk_percent', 5.0)
        self.max_consecutive_losses = config.get('max_consecutive_losses', 3)
        self.cooldown_after_trade_seconds = config.get('cooldown_after_trade_seconds', 60)

        self.consecutive_losses = 0
        self.last_trade_time: Optional[datetime] = None
        self.current_open_positions = 0
        self.open_positions_by_symbol: dict[str, int] = {}

        # Автоматично тримаємо стан у синхроні з реальними подіями відкриття/закриття позицій
        self.event_bus.subscribe(EventType.POSITION_OPENED, self._on_position_opened_event)
        self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_closed_event)

        logger.info("RiskManager initialized")

    def can_open_position(self, symbol: str, risk_amount: float = 0.0) -> tuple[bool, Optional[str]]:
        if self.current_open_positions >= self.max_open_positions:
            reason = f"Max open positions reached: {self.max_open_positions}"
            logger.warning(reason)
            return False, reason

        if self.open_positions_by_symbol.get(symbol, 0) >= self.max_positions_per_symbol:
            reason = f"Max positions per symbol reached for {symbol}: {self.max_positions_per_symbol}"
            logger.warning(reason)
            return False, reason

        if self.consecutive_losses >= self.max_consecutive_losses:
            reason = f"Max consecutive losses reached: {self.max_consecutive_losses}"
            logger.warning(reason)
            return False, reason

        if self.last_trade_time:
            time_since_last_trade = (datetime.utcnow() - self.last_trade_time).total_seconds()
            if time_since_last_trade < self.cooldown_after_trade_seconds:
                reason = f"Cooldown active: {self.cooldown_after_trade_seconds - int(time_since_last_trade)}s remaining"
                logger.warning(reason)
                return False, reason

        return True, None

    def position_opened(self, symbol: Optional[str] = None) -> None:
        self.current_open_positions += 1
        self.last_trade_time = datetime.utcnow()
        if symbol:
            self.open_positions_by_symbol[symbol] = self.open_positions_by_symbol.get(symbol, 0) + 1
        logger.info(f"Position opened. Current open positions: {self.current_open_positions}")

    def position_closed(self, pnl: float, symbol: Optional[str] = None) -> None:
        self.current_open_positions = max(0, self.current_open_positions - 1)
        self.last_trade_time = datetime.utcnow()

        if symbol and symbol in self.open_positions_by_symbol:
            self.open_positions_by_symbol[symbol] = max(0, self.open_positions_by_symbol[symbol] - 1)

        if pnl < 0:
            self.consecutive_losses += 1
            logger.info(f"Loss recorded. Consecutive losses: {self.consecutive_losses}")
        else:
            self.consecutive_losses = 0
            logger.info("Win recorded. Consecutive losses reset to 0")

        logger.info(f"Position closed. Current open positions: {self.current_open_positions}")

    async def _on_position_opened_event(self, event: Event) -> None:
        self.position_opened(symbol=event.data.get('symbol'))

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

        logger.info("Risk config updated")

    def get_status(self) -> dict:
        return {
            'current_open_positions': self.current_open_positions,
            'max_open_positions': self.max_open_positions,
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