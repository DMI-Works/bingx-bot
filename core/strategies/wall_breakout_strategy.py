"""
WallBreakoutStrategy — перетворює подію WALL_BREAKOUT від OrderBookAnalyzer
на торговий сигнал (SIGNAL_GENERATED), який підхоплює SimpleTrader і
відкриває позицію.

На відміну від стратегій, що аналізують КОЖЕН тік ціни (PRICE_UPDATED)
через analyze(), ця реагує виключно на вже готовий детект пробою від
OrderBookAnalyzer — тому analyze() тут свідомо завжди повертає None, а вся
логіка сигналу — в _on_wall_breakout(), підписаному напряму на WALL_BREAKOUT.

Напрямок угоди:
  - пробій ASK-стіни (опору) вгору   -> LONG
  - пробій BID-стіни (підтримки) вниз -> SHORT

За замовчуванням, як і всі стратегії в реєстрі, вимкнена (enabled=False),
доки її не увімкнули в мініаппі — детект стін варто спершу перевірити на
адекватність (спам/пропуски), перш ніж довіряти йому реальні гроші.
"""

from __future__ import annotations

import copy
import logging
import time
from typing import Dict, Optional

from .registry import register_strategy
from .base_strategy import BaseStrategy
from ..events import EventBus, Event, EventType

logger = logging.getLogger(__name__)


@register_strategy('WallBreakoutStrategy')
class WallBreakoutStrategy(BaseStrategy):
    DEFAULT_PARAMS: Dict[str, object] = {
        'position_size': 100,
        'leverage': 20,
        'stop_loss_percent': 20,  
        'cooldown_seconds': 10,
        # Пауза на новий вхід у ту саму монету після ЗАКРИТТЯ угоди по ній.
        # Тестнет 09-14.10: повторні входи <15 хв після попередньої угоди —
        # 28 з 63 угод і -27.8 з -37.4 USDT збитку (пила на KAIA, W, GTC);
        # "свіжі" входи — WR 51%, середній ROE +0.2%. 0 = вимкнено.
        'reentry_cooldown_seconds': 900,
        # Пороги трейлинг-стопа в % ROI. Сами стратегией не используются —
        # их читает TrailingStopManager (см. StrategyManager.subscribe_params
        # и main.py). Единственный источник значения: здесь + БД/мини-апп.
        'trail_levels_percent': [10, 16, 32, 64, 120, 200, 250, 300, 350, 400],
    }

    def __init__(self, event_bus: EventBus, config: dict):
        super().__init__("WallBreakoutStrategy", event_bus, config)

        d = self.DEFAULT_PARAMS
        
        self.position_size: float = config.get('position_size', d['position_size'])
        self.leverage: int = config.get('leverage', d['leverage'])

        sl_roi_percent = config.get('stop_loss_percent', d['stop_loss_percent'])
        self.stop_loss_percent: float = sl_roi_percent  
        self._stop_loss_price_percent: float = sl_roi_percent / self.leverage

        self.cooldown_seconds: float = config.get('cooldown_seconds', d['cooldown_seconds'])
        self.reentry_cooldown_seconds: float = config.get(
            'reentry_cooldown_seconds', d['reentry_cooldown_seconds']
        )

        logger.info(
            f"WallBreakoutStrategy risk config: leverage={self.leverage}x, "
            f"SL={sl_roi_percent}% ROI -> {self._stop_loss_price_percent:.8f}% price"
        )

        # кулдаун per symbol, щоб не відкривати кілька угод підряд на серії
        # пробоїв одного й того ж рівня (шум/повторні detect)
        self.last_trade_time: Dict[str, float] = {}
        # symbol -> час закриття останньої угоди цієї стратегії по монеті
        self.last_close_time: Dict[str, float] = {}
        # symbol -> (side, час) останнього сигналу — контекст для аналітики
        self._last_signal: Dict[str, tuple] = {}

        # BaseStrategy.__init__ вже підписав нас на PRICE_UPDATED, але
        # analyze() з нього нічого не робить (див. докстрінг модуля) —
        # реальний тригер сигналу тут:
        self.event_bus.subscribe(EventType.WALL_BREAKOUT, self._on_wall_breakout)
        self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_closed)

    @classmethod
    def build_config(cls, app_config) -> dict:
        # Копия ВСЕГО DEFAULT_PARAMS: новое поле достаточно добавить в словарь
        # выше — оно само попадёт в БД (seed_defaults) и в мини-апп.
        return copy.deepcopy(cls.DEFAULT_PARAMS)

    def update_config(self, new_config: dict) -> None:
        self.config = new_config
        self.position_size = new_config.get('position_size', self.position_size)
        self.leverage = new_config.get('leverage', self.leverage)

        sl_roi_percent = new_config.get('stop_loss_percent', self.stop_loss_percent)
        self.stop_loss_percent = sl_roi_percent
        self._stop_loss_price_percent = sl_roi_percent / self.leverage

        self.cooldown_seconds = new_config.get('cooldown_seconds', self.cooldown_seconds)
        self.reentry_cooldown_seconds = new_config.get('reentry_cooldown_seconds', self.reentry_cooldown_seconds)

    async def analyze(self, symbol: str, price: float) -> Optional[dict]:
        # Ця стратегія не реагує на прості оновлення ціни — сигнал
        # народжується виключно в _on_wall_breakout().
        return None

    async def _on_position_closed(self, event: Event) -> None:
        data = event.data or {}
        symbol = data.get('symbol')
        if symbol and data.get('strategy') == self.name:
            self.last_close_time[symbol] = time.time()

    async def _on_wall_breakout(self, event: Event) -> None:
        if not self.enabled:
            return

        data = event.data or {}
        symbol = data.get('symbol')
        wall_side = data.get('side')  # 'bid' | 'ask'
        wall_price = data.get('wall_price')
        current_price = data.get('current_price')
        consumed_pct = data.get('consumed_pct', 0.0)

        if not symbol or wall_side not in ('bid', 'ask') or not current_price:
            logger.warning(
                f"WallBreakoutStrategy: неповні дані в WALL_BREAKOUT event, ігнорую: {data}"
            )
            return

        now = time.time()
        last_trade = self.last_trade_time.get(symbol, 0)
        time_since_last = now - last_trade
        if time_since_last < self.cooldown_seconds:
            logger.info(
                f"[{symbol}] WallBreakout SKIP: cooldown active "
                f"({time_since_last:.1f}s / {self.cooldown_seconds}s)"
            )
            return

        since_close = now - self.last_close_time.get(symbol, 0)
        if since_close < self.reentry_cooldown_seconds:
            logger.info(
                f"[{symbol}] WallBreakout SKIP: re-entry cooldown after close "
                f"({since_close:.0f}s / {self.reentry_cooldown_seconds:g}s)"
            )
            return

        # пробій ASK (опору) вгору -> LONG; пробій BID (підтримки) вниз -> SHORT
        side = 'LONG' if wall_side == 'ask' else 'SHORT'
        signal = self._build_signal(symbol, side, current_price, wall_price, wall_side, consumed_pct)
        signal['analytics_context'] = self._analytics_context(symbol, side, now, consumed_pct, data)

        self.last_trade_time[symbol] = now
        self._last_signal[symbol] = (side, now)
        logger.info(
            f"[{symbol}] WallBreakout SIGNAL: {side} "
            f"(wall={wall_side} @ {wall_price}, consumed={consumed_pct:.0f}%, price={current_price:.6f})"
        )
        await self.event_bus.publish(Event(
            type=EventType.SIGNAL_GENERATED,
            data=signal,
            source=self.name
        ))

    def _analytics_context(self, symbol: str, side: str, now: float, consumed_pct: float, data: dict) -> dict:
        """Знімок пробою для trade_analytics.signal_context: параметри стіни й
        книги від OrderBookAnalyzer + історія сигналів по монеті."""
        context = dict(data.get('context') or {})
        context['consumed_pct'] = consumed_pct
        prev = self._last_signal.get(symbol)
        if prev:
            context['prev_signal_side'] = prev[0]
            context['prev_signal_same_side'] = prev[0] == side
            context['seconds_since_prev_signal'] = now - prev[1]
        last_close = self.last_close_time.get(symbol)
        if last_close:
            context['seconds_since_prev_close'] = now - last_close
        return context

    def _build_signal(
        self,
        symbol: str,
        side: str,
        price: float,
        wall_price: Optional[float],
        wall_side: str,
        consumed_pct: float,
    ) -> dict:
        is_long = side == 'LONG'

        stop_loss_price = (
            price * (1 - self._stop_loss_price_percent / 100) if is_long
            else price * (1 + self._stop_loss_price_percent / 100)
        )

        wall_price_str = f"{wall_price:.6f}" if wall_price else "N/A"

        return {
            'action': 'OPEN',
            'symbol': symbol,
            'side': side,
            'quantity': self.position_size / price,
            'leverage': self.leverage,
            'stop_loss_price': stop_loss_price,
            'strategy': self.name,
            'reference_price': price,
            'reason': (
                f'Пробій {wall_side.upper()}-стіни @ {wall_price_str}, '
                f"з'їдено {consumed_pct:.0f}% обсягу, ціна {price:.6f}"
            )
        }