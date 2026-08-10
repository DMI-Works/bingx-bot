# strategies/base_strategy.py
from abc import ABC, abstractmethod
from typing import Optional

from ..events import EventBus, Event, EventType


class BaseStrategy(ABC):
    def __init__(self, name: str, event_bus: EventBus, config: dict):
        self.name = name
        self.event_bus = event_bus
        self.config = config
        self.enabled = False
        self.event_bus.subscribe(EventType.PRICE_UPDATED, self._on_price_update)

    @classmethod
    @abstractmethod
    def build_config(cls, app_config) -> dict:
        """Собирает strategy_config из общего конфига приложения."""
        raise NotImplementedError

    @abstractmethod
    async def analyze(self, symbol: str, price: float) -> Optional[dict]:
        pass

    async def _on_price_update(self, event: Event) -> None:
        if not self.enabled:
            return
        data = event.data[0]
        symbol = data.get('s')
        price = float(data.get('p', 0))
        if symbol and price:
            signal = await self.analyze(symbol, price)
            if signal:
                await self.event_bus.publish(Event(
                    type=EventType.SIGNAL_GENERATED,
                    data=signal,
                    source=self.name
                ))

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False

    def is_enabled(self) -> bool:
        return self.enabled

    def update_config(self, new_config: dict) -> None:
        """
        Викликається StrategyManager, коли параметри стратегії змінюються
        в рантаймі через Telegram-меню (без рестарту бота).

        За замовчуванням просто підміняє self.config цілком. Якщо конкретна
        стратегія кешує окремі параметри в атрибутах (наприклад
        self.period = config['period'] в __init__), ПЕРЕВИЗНАЧТЕ цей метод
        у підкласі, щоб оновити ці атрибути теж — інакше зміна параметра
        через меню не подіє, поки config читається лише опосередковано
        через self.config всередині analyze().
        """
        self.config = new_config