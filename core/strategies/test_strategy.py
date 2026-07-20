import logging
from typing import Optional

from .registry import register_strategy
from .base_strategy import BaseStrategy
from ..events import EventBus, Event, EventType


logger = logging.getLogger(__name__)

@register_strategy('TestStrategy')
class TestStrategy(BaseStrategy):
    """
    Тестовая стратегия для проверки пайплайна сигналов и уведомлений.

    Не реагирует на реальные обновления цены — analyze() всегда возвращает None.
    Вместо этого имеет публичный метод trigger(), который можно вызвать
    напрямую (например, по команде из Telegram-бота), чтобы сгенерировать
    тестовый сигнал и убедиться, что он доходит до подписчиков SIGNAL_GENERATED
    (в частности — до кода, который шлёт уведомление в Telegram).
    """

    def __init__(self, event_bus: EventBus, config: dict):
        super().__init__("TestStrategy", event_bus, config)

        self.default_symbol = config.get('default_symbol', 'BTCUSDT')
        self.default_price = config.get('default_price', 100.0)
        self.position_size = config.get('position_size', 100)
        self.leverage = config.get('leverage', 10)

    @classmethod
    def build_config(cls, app_config) -> dict:
        return {
            'default_symbol': app_config.get('trading.test_strategy.default_symbol', 'BTCUSDT'),
            'default_price': app_config.get('trading.test_strategy.default_price', 100.0),
            'position_size': app_config.get('trading.position_size.value', 100),
            'leverage': app_config.get('trading.leverage', 10),
        }
    async def analyze(self, symbol: str, price: float) -> Optional[dict]:
        # Тестовая стратегия не торгует по рыночным данным — только вручную через trigger()
        return None

    async def trigger(
        self,
        symbol: Optional[str] = None,
        price: Optional[float] = None,
        side: str = "LONG",
    ) -> dict:
        """
        Принудительно генерирует и публикует тестовый сигнал.
        Вызывать напрямую из обработчика команды Telegram-бота.
        """
        symbol = symbol or self.default_symbol
        price = price or self.default_price
        is_long = side.upper() == "LONG"

        stop_loss_price = price * (0.98 if is_long else 1.02)
        self.take_profit_levels = config.get(
            'take_profit_levels', [{'percent': 3.0, 'close_percent': 100}]
        )

        signal = {
            'action': 'OPEN',
            'symbol': symbol,
            'side': side.upper(),
            'quantity': self.position_size / price,
            'leverage': self.leverage,
            'stop_loss_price': stop_loss_price,
            'take_profit_levels': take_profit_levels,
            'reason': f'[ТЕСТ] Ручной тестовый сигнал ({side.upper()}), симуляция для проверки уведомлений',
        }

        logger.info(f"[TEST] Публикую тестовый сигнал: {signal}")

        await self.event_bus.publish(Event(
            type=EventType.SIGNAL_GENERATED,
            data=signal,
            source=self.name,
        ))

        return signal