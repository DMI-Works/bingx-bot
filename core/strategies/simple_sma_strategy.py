from typing import Optional
import logging

from .base_strategy import BaseStrategy
from ..events import EventBus


logger = logging.getLogger(__name__)


class SimpleMovingAverageStrategy(BaseStrategy):
    def __init__(self, event_bus: EventBus, config: dict):
        super().__init__("SimpleMovingAverageStrategy", event_bus, config)

        self.price_history = {}
        self.sma_period = config.get('sma_period', 20)
        self.position_size = config.get('position_size', 100)
        self.stop_loss_percent = config.get('stop_loss_percent', 2.0)
        self.take_profit_percent = config.get('take_profit_percent', 3.0)

        logger.info(f"SimpleMovingAverageStrategy initialized with period={self.sma_period}")

    async def analyze(self, symbol: str, price: float) -> Optional[dict]:
        if symbol not in self.price_history:
            self.price_history[symbol] = []

        self.price_history[symbol].append(price)

        if len(self.price_history[symbol]) > self.sma_period * 2:
            self.price_history[symbol].pop(0)

        if len(self.price_history[symbol]) < self.sma_period:
            return None

        sma = sum(self.price_history[symbol][-self.sma_period:]) / self.sma_period

        if price > sma * 1.01:
            stop_loss_price = price * (1 - self.stop_loss_percent / 100)
            take_profit_price = price * (1 + self.take_profit_percent / 100)

            return {
                'action': 'OPEN',
                'symbol': symbol,
                'side': 'LONG',
                'quantity': self.position_size / price,
                'leverage': 10,
                'stop_loss_price': stop_loss_price,
                'take_profit_levels': [
                    {'price': take_profit_price, 'close_percent': 100}
                ],
                'reason': f'Price {price} > SMA {sma:.2f}'
            }

        elif price < sma * 0.99:
            stop_loss_price = price * (1 + self.stop_loss_percent / 100)
            take_profit_price = price * (1 - self.take_profit_percent / 100)

            return {
                'action': 'OPEN',
                'symbol': symbol,
                'side': 'SHORT',
                'quantity': self.position_size / price,
                'leverage': 10,
                'stop_loss_price': stop_loss_price,
                'take_profit_levels': [
                    {'price': take_profit_price, 'close_percent': 100}
                ],
                'reason': f'Price {price} < SMA {sma:.2f}'
            }

        return None
