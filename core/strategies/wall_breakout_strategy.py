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

Модель ризику (use_atr_risk=True, за замовчуванням):
  - стоп = atr_stop_multiplier * ATR(atr_period, atr_interval), обмежений
    [min_stop_price_percent, max_stop_price_percent] (у % ціни);
  - плече = floor(stop_loss_percent[%ROI] / стоп[%ціни]), не вище `leverage`.
    Тобто ризик на угоду залишається ~stop_loss_percent % ROI від маржі
    незалежно від монети, а не "фіксований 1% ціни на будь-якій волатильності";
  - маржа на угоду стала (position_size / leverage), тому номінал = маржа * плече.
Якщо свічки отримати не вдалось — відкат на стару фіксовану модель
(стоп = stop_loss_percent / leverage, плече = leverage).
"""

from __future__ import annotations

import logging
import math
import time
from typing import Dict, List, Optional, Tuple

from .registry import register_strategy
from .base_strategy import BaseStrategy
from ..events import EventBus, Event, EventType

logger = logging.getLogger(__name__)


@register_strategy('WallBreakoutStrategy')
class WallBreakoutStrategy(BaseStrategy):
    # УВАГА: параметри стратегій засіваються в Mongo ОДИН раз (seed_defaults),
    # тому для вже існуючої БД нові ключі відсутні в збережених params. Тому
    # кожен параметр читається як config.get(key, DEFAULT_PARAMS[key]).
    DEFAULT_PARAMS: Dict[str, object] = {
        'position_size': 100,       # номінал (USDT) при плечі `leverage` -> маржа = position_size / leverage
        'leverage': 20,             # МАКСИМАЛЬНЕ плече
        'stop_loss_percent': 20,    # ризик на угоду, %ROI від маржі
        'cooldown_seconds': 10,
        # --- ризик, адаптований до волатильності монети ---
        'use_atr_risk': True,
        'atr_period': 14,
        'atr_interval': '1m',
        'atr_stop_multiplier': 2.0,
        'min_stop_price_percent': 0.4,
        'max_stop_price_percent': 3.0,
    }

    ATR_CACHE_TTL_SECONDS = 60.0

    def __init__(self, event_bus: EventBus, config: dict, bingx_client=None):
        super().__init__("WallBreakoutStrategy", event_bus, config)

        self.bingx_client = bingx_client
        self._atr_cache: Dict[str, Tuple[float, float]] = {}  # symbol -> (ts, atr_percent)

        self._load_params(config)

        logger.info(
            f"WallBreakoutStrategy risk config: max leverage={self.leverage}x, "
            f"risk={self.stop_loss_percent}% ROI, use_atr_risk={self.use_atr_risk} "
            f"(ATR{self.atr_period}/{self.atr_interval} x{self.atr_stop_multiplier}, "
            f"stop clamp {self.min_stop_price_percent}-{self.max_stop_price_percent}% price); "
            f"fallback fixed stop={self._stop_loss_price_percent:.4f}% price"
        )

        # той самий кулдаун-принцип, що й у SMA-стратегії — per symbol,
        # щоб не відкривати кілька угод підряд на серії пробоїв одного й
        # того ж рівня (шум/повторні detect)
        self.last_trade_time: Dict[str, float] = {}

        # BaseStrategy.__init__ вже підписав нас на PRICE_UPDATED, але
        # analyze() з нього нічого не робить (див. докстрінг модуля) —
        # реальний тригер сигналу тут:
        self.event_bus.subscribe(EventType.WALL_BREAKOUT, self._on_wall_breakout)

    def _load_params(self, config: dict) -> None:
        d = self.DEFAULT_PARAMS

        self.position_size: float = config.get('position_size', d['position_size'])
        self.leverage: int = config.get('leverage', d['leverage'])
        self.stop_loss_percent: float = config.get('stop_loss_percent', d['stop_loss_percent'])
        self.cooldown_seconds: float = config.get('cooldown_seconds', d['cooldown_seconds'])

        self.use_atr_risk: bool = bool(config.get('use_atr_risk', d['use_atr_risk']))
        self.atr_period: int = int(config.get('atr_period', d['atr_period']))
        self.atr_interval: str = str(config.get('atr_interval', d['atr_interval']))
        self.atr_stop_multiplier: float = float(config.get('atr_stop_multiplier', d['atr_stop_multiplier']))
        self.min_stop_price_percent: float = float(config.get('min_stop_price_percent', d['min_stop_price_percent']))
        self.max_stop_price_percent: float = float(config.get('max_stop_price_percent', d['max_stop_price_percent']))

        # старе фіксоване значення (відкат, якщо ATR недоступний)
        self._stop_loss_price_percent: float = self.stop_loss_percent / self.leverage

    @classmethod
    def build_config(cls, app_config) -> dict:
        return dict(cls.DEFAULT_PARAMS)

    def update_config(self, new_config: dict) -> None:
        self.config = new_config
        self._load_params(new_config)
        self._atr_cache.clear()

    async def analyze(self, symbol: str, price: float) -> Optional[dict]:
        # Ця стратегія не реагує на прості оновлення ціни — сигнал
        # народжується виключно в _on_wall_breakout().
        return None

    # ---------- ризик, адаптований до волатильності ----------

    @staticmethod
    def _compute_atr(klines: List[dict], period: int) -> Optional[float]:
        """Простий ATR за закритими свічками. Остання (незакрита) свічка
        відкидається. Повертає None, якщо даних замало."""
        candles = []
        for k in klines or []:
            try:
                candles.append((float(k['high']), float(k['low']), float(k['close'])))
            except (KeyError, TypeError, ValueError):
                continue

        if len(candles) > period + 1:
            candles = candles[:-1]  # незакрита свічка
        if len(candles) < 2:
            return None

        true_ranges = []
        for i in range(1, len(candles)):
            high, low, _ = candles[i]
            prev_close = candles[i - 1][2]
            true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

        if len(true_ranges) < max(3, period // 2):
            return None

        window = true_ranges[-period:]
        return sum(window) / len(window)

    async def _get_atr_percent(self, symbol: str, price: float) -> Optional[float]:
        if price <= 0:
            return None

        now = time.time()
        cached = self._atr_cache.get(symbol)
        if cached and now - cached[0] < self.ATR_CACHE_TTL_SECONDS:
            return cached[1]

        if self.bingx_client is None:
            return None

        try:
            klines = await self.bingx_client.get_klines(
                symbol=symbol, interval=self.atr_interval, limit=self.atr_period + 2
            )
        except Exception as e:
            logger.warning(f"[{symbol}] WallBreakout: не вдалось отримати klines для ATR: {e}")
            return None

        atr = self._compute_atr(klines, self.atr_period)
        if atr is None or atr <= 0:
            return None

        atr_percent = atr / price * 100.0
        self._atr_cache[symbol] = (now, atr_percent)
        return atr_percent

    async def _compute_risk(self, symbol: str, price: float) -> Tuple[float, int, float, dict]:
        """
        Повертає (stop_price_percent, leverage, notional_usdt, meta).
        meta потрапляє в сигнал/лог для діагностики.
        """
        max_leverage = max(1, int(self.leverage))
        margin = self.position_size / max_leverage  # стала маржа на угоду

        atr_percent = await self._get_atr_percent(symbol, price) if self.use_atr_risk else None

        if atr_percent is None:
            meta = {'risk_model': 'fixed', 'atr_percent': None}
            return self._stop_loss_price_percent, max_leverage, self.position_size, meta

        stop_percent = self.atr_stop_multiplier * atr_percent
        stop_percent = min(max(stop_percent, self.min_stop_price_percent), self.max_stop_price_percent)

        leverage = int(math.floor(self.stop_loss_percent / stop_percent))
        leverage = min(max(leverage, 1), max_leverage)

        meta = {'risk_model': 'atr', 'atr_percent': round(atr_percent, 4)}
        return stop_percent, leverage, margin * leverage, meta

    # ---------- сигнал ----------

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

        # Кулдаун фіксуємо ДО await'ів (REST за свічками): інакше два швидкі
        # WALL_BREAKOUT по одній монеті можуть обидва пройти перевірку вище.
        self.last_trade_time[symbol] = now

        # пробій ASK (опору) вгору -> LONG; пробій BID (підтримки) вниз -> SHORT
        side = 'LONG' if wall_side == 'ask' else 'SHORT'
        signal = await self._build_signal(symbol, side, current_price, wall_price, wall_side, consumed_pct)

        logger.info(
            f"[{symbol}] WallBreakout SIGNAL: {side} "
            f"(wall={wall_side} @ {wall_price}, consumed={consumed_pct:.0f}%, price={current_price:.6f}, "
            f"risk={signal['risk_model']}, atr%={signal.get('atr_percent')}, "
            f"stop={signal['stop_price_percent']:.3f}% price, leverage={signal['leverage']}x)"
        )
        await self.event_bus.publish(Event(
            type=EventType.SIGNAL_GENERATED,
            data=signal,
            source=self.name
        ))

    async def _build_signal(
        self,
        symbol: str,
        side: str,
        price: float,
        wall_price: Optional[float],
        wall_side: str,
        consumed_pct: float,
    ) -> dict:
        is_long = side == 'LONG'

        stop_percent, leverage, notional, meta = await self._compute_risk(symbol, price)

        stop_loss_price = (
            price * (1 - stop_percent / 100) if is_long
            else price * (1 + stop_percent / 100)
        )

        wall_price_str = f"{wall_price:.6f}" if wall_price else "N/A"

        return {
            'action': 'OPEN',
            'symbol': symbol,
            'side': side,
            'quantity': notional / price,
            'leverage': leverage,
            'stop_loss_price': stop_loss_price,
            'strategy': self.name,
            'reference_price': price,
            'stop_price_percent': stop_percent,
            'risk_model': meta['risk_model'],
            'atr_percent': meta['atr_percent'],
            'reason': (
                f'Пробій {wall_side.upper()}-стіни @ {wall_price_str}, '
                f"з'їдено {consumed_pct:.0f}% обсягу, ціна {price:.6f}"
            )
        }
