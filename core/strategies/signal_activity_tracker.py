import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from ..events import Event, EventBus, EventType

logger = logging.getLogger(__name__)


class SignalActivityTracker:
    """
    Слухає SIGNAL_GENERATED і запам'ятовує час останнього сигналу по кожному
    символу. Потрібен SymbolSelector'у, щоб при ротації монет НЕ чіпати
    символи, які зараз "живі" (генерують сигнали), навіть якщо їхній 24h
    об'єм випав з топ-N — і, навпаки, вважати "тихі" (без сигналів за
    останню годину) символи кандидатами на заміну.

    Живе тільки в пам'яті процесу (rebuild з нуля при рестарті бота — це
    ОК: "тиша" одразу після рестарту не повинна штучно захищати символ,
    він просто на загальних підставах пройде звичайний volume-фільтр,
    поки не з'явиться перший сигнал).
    """

    def __init__(self, event_bus: EventBus):
        self._last_signal_at: Dict[str, datetime] = {}
        event_bus.subscribe(EventType.SIGNAL_GENERATED, self._on_signal)

    async def _on_signal(self, event: Event) -> None:
        data = event.data or {}
        symbol = data.get('symbol')
        if not symbol:
            return
        self._last_signal_at[symbol] = datetime.now(timezone.utc)

    def last_signal_at(self, symbol: str) -> Optional[datetime]:
        return self._last_signal_at.get(symbol)

    def had_signal_within(self, symbol: str, seconds: float) -> bool:
        """True, якщо по символу був хоч один сигнал за останні `seconds`.
        Символ, по якому взагалі ще не було жодного сигналу з моменту
        запуску бота, вважається "тихим" (False) — він не отримує
        незаслуженого захисту від ротації просто тому, що новий."""
        last = self._last_signal_at.get(symbol)
        if last is None:
            return False
        age = (datetime.now(timezone.utc) - last).total_seconds()
        return age <= seconds

    def get_quiet_symbols(self, symbols, seconds: float) -> set:
        """Повертає підмножину symbols, які НЕ давали сигналів за останні `seconds`."""
        return {s for s in symbols if not self.had_signal_within(s, seconds)}