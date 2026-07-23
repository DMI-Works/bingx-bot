import time
from typing import Optional, List, Dict
import logging

from .registry import register_strategy
from .base_strategy import BaseStrategy
from ..events import EventBus


logger = logging.getLogger(__name__)


class Candle:
    __slots__ = ('open', 'high', 'low', 'close', 'start_time')

    def __init__(self, price: float, start_time: float):
        self.open = price
        self.high = price
        self.low = price
        self.close = price
        self.start_time = start_time

    def update(self, price: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def range(self) -> float:
        return self.high - self.low

@register_strategy('RejectionBlockStrategy')
class RejectionBlockStrategy(BaseStrategy):
    """
    Ищет "rejection block": свечу похожую на молот/пин-бар, чей экстремум
    (лоу для бычьего паттерна, хай для медвежьего) "закрывается" тенью
    предыдущей свечи — попадает в диапазон её тени. Идея: такая повторная
    реакция от уровня даёт высокую вероятность движения на 2-3% в сторону,
    противоположную доминирующей тени.

    ВАЖНО: это чистый price-action паттерн, вероятность отработки 2-3%
    заявлена эмпирически автором идеи. Параметры (wick_to_body_ratio,
    min_wick_ratio, overlap_tolerance_percent) нужно подобрать бэктестом
    под конкретный инструмент/таймфрейм перед реальным использованием.
    """

    def __init__(self, event_bus: EventBus, config: dict):
        super().__init__("RejectionBlockStrategy", event_bus, config)

        # --- свечи ---
        self.timeframe_seconds = config.get('timeframe_seconds', 60)

        # --- параметры паттерна ---
        # во сколько раз доминирующая тень должна быть больше тела свечи
        self.wick_to_body_ratio = config.get('wick_to_body_ratio', 2.0)
        # доминирующая тень должна занимать не менее этой доли от всего range свечи
        self.min_wick_ratio = config.get('min_wick_ratio', 0.6)
        # противоположная тень не должна быть больше этой доли от доминирующей тени
        self.opposite_wick_max_ratio = config.get('opposite_wick_max_ratio', 0.3)
        # допуск (в % от цены) на "перекрытие" тени текущей свечи тенью предыдущей
        self.overlap_tolerance_percent = config.get('overlap_tolerance_percent', 0.05)
        # минимальный размер тела свечи в % от цены (фильтр от свечей-игл на пустом объёме)
        self.min_body_percent = config.get('min_body_percent', 0.0)

        # --- риск/цели ---
        self.stop_loss_buffer_percent = config.get('stop_loss_buffer_percent', 0.2)
        self.take_profit_percents = config.get('take_profit_percents', [2.0, 3.0])
        self.tp_close_percents = config.get('tp_close_percents', [50, 50])

        # --- позиция ---
        self.position_size = config.get('position_size', 100)
        self.leverage = config.get('leverage', 10)

        # --- кулдаун ---
        self.cooldown_seconds = config.get('cooldown_seconds', 300)

        # --- состояние per symbol ---
        self.candles: Dict[str, List[Candle]] = {}
        self.current_candle: Dict[str, Candle] = {}
        self.last_trade_time: Dict[str, float] = {}

    @classmethod
    def build_config(cls, app_config) -> dict:
        use_atr_risk = app_config.get('trading.stop_loss.mode', 'fixed_percent') == 'atr'
        return {
            'timeframe_seconds': 60,
            'sma_period': app_config.get('trading.sma_period', 20),
            'threshold_percent': app_config.get('trading.threshold_percent', 0.3),
            'confirmation_candles': app_config.get('trading.confirmation_candles', 2),
            'cooldown_seconds': app_config.get('trading.cooldown_seconds', 300),
            'position_size': app_config.get('trading.position_size.value', 100),
            'leverage': app_config.get('trading.leverage', 10),

            'use_atr_risk': use_atr_risk,
            'atr_period': app_config.get('trading.stop_loss.atr.period', 14),
            'atr_stop_multiplier': app_config.get('trading.stop_loss.atr.multiplier', 1.5),
            'atr_tp_multipliers': app_config.get('trading.take_profit.atr.multipliers', [2.0, 3.5]),
            'tp_close_percents': app_config.get('trading.take_profit.atr.close_percents', [50, 50]),

            'stop_loss_percent': app_config.get('trading.stop_loss.value', 2.0),
            'take_profit_levels': app_config.get(
                'trading.take_profit.levels', [{'percent': 3.0, 'close_percent': 100}]
            ),
        }

    async def analyze(self, symbol: str, price: float) -> Optional[dict]:
        now = time.time()
        closed_candle = self._update_candle(symbol, price, now)

        if closed_candle is None:
            return None

        candles = self.candles.get(symbol, [])
        if len(candles) < 2:
            logger.info(f"[{symbol}] SKIP: not enough candles yet ({len(candles)}/2 needed)")
            return None

        last_trade = self.last_trade_time.get(symbol, 0)
        time_since_last = now - last_trade
        if time_since_last < self.cooldown_seconds:
            logger.info(
                f"[{symbol}] SKIP: cooldown active "
                f"({time_since_last:.1f}s / {self.cooldown_seconds}s)"
            )
            return None

        current = candles[-1]
        previous = candles[-2]

        pattern_side = self._detect_pattern(current, previous)
        if pattern_side is None:
            return None

        side = 'LONG' if pattern_side == 'bullish' else 'SHORT'
        logger.info(
            f"[{symbol}] SIGNAL: {side} rejection block detected "
            f"(current low={current.low:.6f} high={current.high:.6f}, "
            f"prev low={previous.low:.6f} high={previous.high:.6f})"
        )

        signal = self._build_signal(symbol, side, price, current)
        self.last_trade_time[symbol] = now
        return signal

    def _update_candle(self, symbol: str, price: float, now: float) -> Optional[Candle]:
        current = self.current_candle.get(symbol)

        if current is None:
            self.current_candle[symbol] = Candle(price, now)
            return None

        candle_end = current.start_time + self.timeframe_seconds

        if now < candle_end:
            current.update(price)
            return None

        closed = current
        history = self.candles.setdefault(symbol, [])
        history.append(closed)
        # для паттерна нужны всего 2 последние свечи, буфер небольшой
        if len(history) > 50:
            history.pop(0)

        self.current_candle[symbol] = Candle(price, now)
        return closed

    def _detect_pattern(self, current: Candle, previous: Candle) -> Optional[str]:
        """
        'bullish' — длинная нижняя тень, лоу закрыт тенью предыдущей свечи -> ожидаем рост.
        'bearish' — длинная верхняя тень, хай закрыт тенью предыдущей свечи -> ожидаем падение.
        """
        if current.range <= 0:
            return None

        tolerance = current.close * (self.overlap_tolerance_percent / 100)

        if self._is_hammer(current, current.lower_wick, current.upper_wick):
            prev_lower_wick_top = min(previous.open, previous.close)
            if (previous.low - tolerance) <= current.low <= (prev_lower_wick_top + tolerance):
                return 'bullish'

        if self._is_hammer(current, current.upper_wick, current.lower_wick):
            prev_upper_wick_bottom = max(previous.open, previous.close)
            if (prev_upper_wick_bottom - tolerance) <= current.high <= (previous.high + tolerance):
                return 'bearish'

        return None

    def _is_hammer(self, candle: Candle, dominant_wick: float, opposite_wick: float) -> bool:
        if dominant_wick <= 0 or candle.range <= 0:
            return False

        body = candle.body
        min_body = candle.close * (self.min_body_percent / 100)

        if body < min_body:
            return False
        if dominant_wick < self.wick_to_body_ratio * max(body, 1e-12):
            return False
        if dominant_wick / candle.range < self.min_wick_ratio:
            return False
        if opposite_wick > self.opposite_wick_max_ratio * dominant_wick:
            return False

        return True

    def _build_signal(self, symbol: str, side: str, price: float, pattern_candle: Candle) -> dict:
        is_long = side == 'LONG'

        if is_long:
            stop_loss_price = pattern_candle.low * (1 - self.stop_loss_buffer_percent / 100)
        else:
            stop_loss_price = pattern_candle.high * (1 + self.stop_loss_buffer_percent / 100)

        take_profit_levels = [
            {
                'price': (
                    price * (1 + pct / 100) if is_long
                    else price * (1 - pct / 100)
                ),
                'close_percent': self.tp_close_percents[i] if i < len(self.tp_close_percents) else 100
            }
            for i, pct in enumerate(self.take_profit_percents)
        ]

        logger.info(
            f"[{symbol}] BUILD SIGNAL: side={side}, entry={price:.6f}, "
            f"stop_loss={stop_loss_price:.6f}, take_profit_levels={take_profit_levels}"
        )

        strategy_name = getattr(self, 'name', self.__class__.__name__)

        return {
            'action': 'OPEN',
            'symbol': symbol,
            'side': side,
            'quantity': self.position_size / price,
            'leverage': self.leverage,
            'stop_loss_price': stop_loss_price,
            'take_profit_levels': take_profit_levels,
            'strategy': strategy_name,
            'reason': (
                f'Rejection block ({"бичачий" if is_long else "ведмежий"}): '
                f'мінімум патерну={pattern_candle.low:.6f}, максимум={pattern_candle.high:.6f}, '
                f'тінь перекрила тінь попередньої свічки'
            )
        }