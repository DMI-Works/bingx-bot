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

@register_strategy('SimpleMovingAverageStrategy')
class SimpleMovingAverageStrategy(BaseStrategy):
    def __init__(self, event_bus: EventBus, config: dict):
        super().__init__("SimpleMovingAverageStrategy", event_bus, config)

        # --- свечи ---
        self.timeframe_seconds = config.get('timeframe_seconds', 60)

        # --- SMA / сигнал ---
        self.sma_period = config.get('sma_period', 20)
        self.threshold_percent = config.get('threshold_percent', 0.3)
        self.confirmation_candles = config.get('confirmation_candles', 2)

        # --- ATR ---
        self.atr_period = config.get('atr_period', 14)
        self.use_atr_risk = config.get('use_atr_risk', True)
        self.atr_stop_multiplier = config.get('atr_stop_multiplier', 1.5)
        self.atr_tp_multipliers = config.get('atr_tp_multipliers', [2.0, 3.5])  # R-множители по уровням
        self.tp_close_percents = config.get('tp_close_percents', [50, 50])      # % закрытия на каждом уровне

        # --- fallback: фиксированные проценты, если ATR выключен ---
        self.stop_loss_percent = config.get('stop_loss_percent', 2.0)
        self.take_profit_levels_config = config.get(
            'take_profit_levels', [{'percent': 3.0, 'close_percent': 100}]
        )

        # --- риск / размер позиции ---
        self.position_size = config.get('position_size', 100)
        self.leverage = config.get('leverage', 10)

        # --- кулдаун ---
        self.cooldown_seconds = config.get('cooldown_seconds', 300)

        # --- состояние per symbol ---
        self.candles: Dict[str, List[Candle]] = {}
        self.current_candle: Dict[str, Candle] = {}
        self.side_history: Dict[str, List[str]] = {}
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
            # свеча ещё не закрылась — это нормальное поведение, не спамим лог
            return None

        candles = self.candles.get(symbol, [])
        min_needed = max(self.sma_period, self.atr_period + 1)
        if len(candles) < min_needed:
            logger.info(
                f"[{symbol}] SKIP: not enough candles yet "
                f"({len(candles)}/{min_needed} needed)"
            )
            return None

        sma = sum(c.close for c in candles[-self.sma_period:]) / self.sma_period
        deviation_percent = (closed_candle.close - sma) / sma * 100

        side = 'above' if deviation_percent > 0 else 'below'
        history = self.side_history.setdefault(symbol, [])
        history.append(side)
        if len(history) > self.confirmation_candles:
            history.pop(0)

        logger.info(
            f"[{symbol}] candle closed: close={closed_candle.close:.6f}, sma={sma:.6f}, "
            f"deviation={deviation_percent:+.4f}% (threshold={self.threshold_percent}%), "
            f"side={side}, side_history={history}"
        )

        last_trade = self.last_trade_time.get(symbol, 0)
        time_since_last = now - last_trade
        if time_since_last < self.cooldown_seconds:
            logger.info(
                f"[{symbol}] SKIP: cooldown active "
                f"({time_since_last:.1f}s / {self.cooldown_seconds}s)"
            )
            return None

        confirmed = (
            len(history) == self.confirmation_candles and
            len(set(history)) == 1
        )
        if not confirmed:
            logger.info(
                f"[{symbol}] SKIP: not confirmed yet "
                f"(need {self.confirmation_candles} matching candles, history={history})"
            )
            return None

        atr = self._calculate_atr(candles)
        if self.use_atr_risk and (atr is None or atr <= 0):
            # нет валидного ATR — пропускаем сигнал, чтобы не открыть позицию без адекватного стопа
            logger.info(f"[{symbol}] SKIP: invalid ATR (atr={atr})")
            return None

        if side == 'above' and deviation_percent > self.threshold_percent:
            logger.info(f"[{symbol}] SIGNAL: LONG (deviation {deviation_percent:+.4f}% > {self.threshold_percent}%)")
            signal = self._build_signal(symbol, 'LONG', price, sma, deviation_percent, atr)
            self.last_trade_time[symbol] = now
            return signal

        elif side == 'below' and deviation_percent < -self.threshold_percent:
            logger.info(f"[{symbol}] SIGNAL: SHORT (deviation {deviation_percent:+.4f}% < -{self.threshold_percent}%)")
            signal = self._build_signal(symbol, 'SHORT', price, sma, deviation_percent, atr)
            self.last_trade_time[symbol] = now
            return signal

        logger.info(
            f"[{symbol}] SKIP: deviation {deviation_percent:+.4f}% did not cross threshold "
            f"±{self.threshold_percent}% (side={side})"
        )
        return None

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
        max_needed = max(self.sma_period, self.atr_period + 1) * 2
        if len(history) > max_needed:
            history.pop(0)

        self.current_candle[symbol] = Candle(price, now)
        return closed

    def _calculate_atr(self, candles: List[Candle]) -> Optional[float]:
        """Average True Range по последним atr_period свечам (метод простого среднего)."""
        if len(candles) < self.atr_period + 1:
            return None

        relevant = candles[-(self.atr_period + 1):]
        true_ranges = []

        for i in range(1, len(relevant)):
            current = relevant[i]
            prev_close = relevant[i - 1].close

            tr = max(
                current.high - current.low,
                abs(current.high - prev_close),
                abs(current.low - prev_close)
            )
            true_ranges.append(tr)

        if not true_ranges:
            return None

        return sum(true_ranges) / len(true_ranges)

    def _build_signal(
        self, symbol: str, side: str, price: float, sma: float,
        deviation_percent: float, atr: Optional[float]
    ) -> dict:
        is_long = side == 'LONG'

        if self.use_atr_risk and atr:
            stop_distance = atr * self.atr_stop_multiplier
            stop_loss_price = price - stop_distance if is_long else price + stop_distance

            take_profit_levels = [
                {
                    'price': (
                        price + atr * mult if is_long
                        else price - atr * mult
                    ),
                    'close_percent': self.tp_close_percents[i] if i < len(self.tp_close_percents) else 100
                }
                for i, mult in enumerate(self.atr_tp_multipliers)
            ]
            risk_desc = f'ATR={atr:.6f}, stop_dist={stop_distance:.6f}'
        else:
            stop_loss_price = (
                price * (1 - self.stop_loss_percent / 100) if is_long
                else price * (1 + self.stop_loss_percent / 100)
            )
            take_profit_levels = [
                {
                    'price': (
                        price * (1 + lvl['percent'] / 100) if is_long
                        else price * (1 - lvl['percent'] / 100)
                    ),
                    'close_percent': lvl['close_percent']
                }
                for lvl in self.take_profit_levels_config
            ]
            risk_desc = 'fixed percent risk'

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
                f'Ціна {price:.6f} {"вище" if is_long else "нижче"} SMA {sma:.6f} '
                f'({deviation_percent:+.2f}%), підтверджено {self.confirmation_candles} свічками, {risk_desc}'
            )
        }