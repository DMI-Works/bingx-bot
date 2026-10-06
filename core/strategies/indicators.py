"""
Чисті (без I/O і без стану) функції індикаторів для стратегій і
TrailingStopManager. Усі приймають списки float у ХРОНОЛОГІЧНОМУ порядку
(старі -> нові) — саме так їх віддає BingXClient.get_klines().

На "прогрітих" позиціях (де індикатор ще не можна порахувати) повертають
None, а не 0 — щоб нуль ніколи не міг випадково пройти як валідне значення.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

_INTERVAL_UNIT_MS = {
    'm': 60_000,
    'h': 3_600_000,
    'd': 86_400_000,
    'w': 604_800_000,
}


def interval_to_ms(interval: str) -> int:
    """'15m' -> 900000, '1h' -> 3600000. ValueError на невідомий формат —
    так некоректний інтервал із мініаппу відсікається ДО першого запиту."""
    if not isinstance(interval, str) or len(interval) < 2:
        raise ValueError(f"invalid interval: {interval!r}")
    unit = interval[-1]
    try:
        count = int(interval[:-1])
    except ValueError:
        raise ValueError(f"invalid interval: {interval!r}") from None
    if unit not in _INTERVAL_UNIT_MS or count <= 0:
        raise ValueError(f"invalid interval: {interval!r}")
    return count * _INTERVAL_UNIT_MS[unit]


def sma(values: List[float], period: int) -> List[Optional[float]]:
    result: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    window_sum = sum(values[:period])
    result[period - 1] = window_sum / period
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        result[i] = window_sum / period
    return result


def ema(values: List[float], period: int) -> List[Optional[float]]:
    """EMA з сідом SMA(period) — так само, як у TradingView (ta.ema)."""
    result: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    alpha = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    result[period - 1] = prev
    for i in range(period, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        result[i] = prev
    return result


def true_range(highs: List[float], lows: List[float], closes: List[float]) -> List[float]:
    tr: List[float] = []
    for i in range(len(closes)):
        if i == 0:
            tr.append(highs[i] - lows[i])
        else:
            prev_close = closes[i - 1]
            tr.append(max(
                highs[i] - lows[i],
                abs(highs[i] - prev_close),
                abs(lows[i] - prev_close),
            ))
    return tr


def atr(
    highs: List[float], lows: List[float], closes: List[float], period: int = 14
) -> List[Optional[float]]:
    """ATR зі згладжуванням Вайлдера (RMA), сід — SMA перших `period` TR."""
    result: List[Optional[float]] = [None] * len(closes)
    if period <= 0 or len(closes) < period:
        return result
    tr = true_range(highs, lows, closes)
    prev = sum(tr[:period]) / period
    result[period - 1] = prev
    for i in range(period, len(closes)):
        prev = (prev * (period - 1) + tr[i]) / period
        result[i] = prev
    return result


def supertrend(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 10,
    multiplier: float = 3.0,
) -> Tuple[List[Optional[int]], List[Optional[float]]]:
    """
    Supertrend (класична версія з фіналізованими смугами).

    Повертає (direction, line):
      direction[i] = +1 (бичачий, лінія під ціною) | -1 (ведмежий, над ціною)
                     | None, поки не прогрітий ATR;
      line[i]      — значення лінії Supertrend.

    Розворот на свічці i — це direction[i] != direction[i-1] (обидва не None).
    """
    n = len(closes)
    direction: List[Optional[int]] = [None] * n
    line: List[Optional[float]] = [None] * n
    atr_values = atr(highs, lows, closes, period)

    final_upper: List[Optional[float]] = [None] * n
    final_lower: List[Optional[float]] = [None] * n

    for i in range(n):
        a = atr_values[i]
        if a is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2.0
        basic_upper = hl2 + multiplier * a
        basic_lower = hl2 - multiplier * a

        is_first_warm = i == 0 or final_upper[i - 1] is None
        if is_first_warm:
            # перша прогріта свічка: смуги = базові, напрям за положенням close
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower
            direction[i] = 1 if closes[i] >= hl2 else -1
        else:
            prev_upper = final_upper[i - 1]
            prev_lower = final_lower[i - 1]
            prev_close = closes[i - 1]

            final_upper[i] = (
                basic_upper if (basic_upper < prev_upper or prev_close > prev_upper) else prev_upper
            )
            final_lower[i] = (
                basic_lower if (basic_lower > prev_lower or prev_close < prev_lower) else prev_lower
            )

            if direction[i - 1] == -1:
                direction[i] = 1 if closes[i] > final_upper[i] else -1
            else:
                direction[i] = -1 if closes[i] < final_lower[i] else 1

        line[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    return direction, line
