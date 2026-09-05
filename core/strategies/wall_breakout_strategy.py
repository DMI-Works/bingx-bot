"""
WallBreakoutStrategy — перетворює подію WALL_BREAKOUT від OrderBookAnalyzer
на реальний торговий сигнал (SIGNAL_GENERATED), який підхоплює SimpleTrader
і відкриває позицію.

Раніше WALL_BREAKOUT вів лише в TelegramBot._on_wall_breakout (сповіщення,
без угоди). Ця стратегія — окрема гілка, яка замикає той самий сигнал на
торгівлю, за тим самим принципом, що й SimpleMovingAverageStrategy /
RejectionBlockStrategy: формує TradeSignal (side/symbol/reference_price/
stop_loss/take_profit) і публікує SIGNAL_GENERATED.

Важлива відмінність від SMA/RejectionBlock: ті аналізують КОЖЕН тік ціни
(PRICE_UPDATED) через analyze(). Ця стратегія натомість реагує виключно на
вже готовий детект пробою від OrderBookAnalyzer — тому analyze() тут
свідомо завжди повертає None, а вся логіка сигналу — в _on_wall_breakout(),
підписаному напряму на WALL_BREAKOUT.

Напрямок угоди:
  - пробій ASK-стіни (опору) вгору   -> LONG
  - пробій BID-стіни (підтримки) вниз -> SHORT

За замовчуванням, як і всі інші стратегії в реєстрі, ця вимкнена
(enabled=False), доки її явно не увімкнули через Telegram /settings —
детект стін варто спершу перевірити на адекватність (спам/пропуски) через
WALL_DETECTED/WALL_BREAKOUT сповіщення, перш ніж довіряти йому реальні
гроші.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

from .registry import register_strategy
from .base_strategy import BaseStrategy
from ..events import EventBus, Event, EventType

logger = logging.getLogger(__name__)


@register_strategy('WallBreakoutStrategy')
class WallBreakoutStrategy(BaseStrategy):

    def __init__(self, event_bus: EventBus, config: dict):
        super().__init__("WallBreakoutStrategy", event_bus, config)

        self.position_size: float = config.get('position_size', 100)
        self.leverage: int = config.get('leverage', 20)
        self.stop_loss_percent: float = config.get('stop_loss_percent', 1.0)
        self.take_profit_levels_config = config.get(
            'take_profit_levels', [{'percent': 30.0, 'close_percent': 100}]
        )
        self.cooldown_seconds: float = config.get('cooldown_seconds', 300)

        # той самий кулдаун-принцип, що й у SMA-стратегії — per symbol,
        # щоб не відкривати кілька угод підряд на серії пробоїв одного й
        # того ж рівня (шум/повторні detect)
        self.last_trade_time: Dict[str, float] = {}

        # BaseStrategy.__init__ вже підписав нас на PRICE_UPDATED, але
        # analyze() з нього нічого не робить (див. докстрінг модуля) —
        # реальний тригер сигналу тут:
        self.event_bus.subscribe(EventType.WALL_BREAKOUT, self._on_wall_breakout)

    @classmethod
    def build_config(cls, app_config) -> dict:
        return {
            'position_size': app_config.get('trading.position_size.value', 100),
            'leverage': app_config.get('trading.leverage', 20),
            'stop_loss_percent': app_config.get('trading.wall_breakout.stop_loss_percent', 1.0),
            'take_profit_levels': app_config.get(
                'trading.wall_breakout.take_profit_levels',
                [{'percent': 2.0, 'close_percent': 100}]
            ),
            'cooldown_seconds': app_config.get('trading.wall_breakout.cooldown_seconds', 300),
        }

    async def analyze(self, symbol: str, price: float) -> Optional[dict]:
        # Ця стратегія не реагує на прості оновлення ціни — сигнал
        # народжується виключно в _on_wall_breakout().
        return None

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

        # пробій ASK (опору) вгору -> LONG; пробій BID (підтримки) вниз -> SHORT
        side = 'LONG' if wall_side == 'ask' else 'SHORT'
        signal = self._build_signal(symbol, side, current_price, wall_price, wall_side, consumed_pct)

        self.last_trade_time[symbol] = now
        logger.info(
            f"[{symbol}] WallBreakout SIGNAL: {side} "
            f"(wall={wall_side} @ {wall_price}, consumed={consumed_pct:.0f}%, price={current_price:.6f})"
        )
        await self.event_bus.publish(Event(
            type=EventType.SIGNAL_GENERATED,
            data=signal,
            source=self.name
        ))

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

        wall_price_str = f"{wall_price:.6f}" if wall_price else "N/A"

        return {
            'action': 'OPEN',
            'symbol': symbol,
            'side': side,
            'quantity': self.position_size / price,
            'leverage': self.leverage,
            'stop_loss_price': stop_loss_price,
            'take_profit_levels': take_profit_levels,
            'strategy': self.name,
            'reference_price': price,
            'reason': (
                f'Пробій {wall_side.upper()}-стіни @ {wall_price_str}, '
                f"з'їдено {consumed_pct:.0f}% обсягу, ціна {price:.6f}"
            )
        }