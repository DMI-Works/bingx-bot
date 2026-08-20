import logging
from typing import Dict, List, Optional

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


class CandleWarmupMixin:
    """
    Спільна логіка прогріву історії свічок з біржі. Підключається до
    будь-якої стратегії через множинне наслідування:

        class MyStrategy(CandleWarmupMixin, BaseStrategy):
            def __init__(self, event_bus, config, bingx_client=None):
                super().__init__(...)
                self.bingx_client = bingx_client   
                ...

            async def analyze(self, symbol, price):
                await self._ensure_warmup(symbol)   # <- один рядок, першим у методі
                ...

    bingx_client передається явно через конструктор стратегії (як exchange
    в SimpleTrader / SymbolSelector) — StrategyManager підставляє його
    автоматично лише тим класам, які його очікують (див. StrategyManager).

    Якщо bingx_client не передано (None) або прогрів впаде з помилкою —
    стратегія просто накопичує свічки наживо, як і раніше. Нічого не
    ламається.

    Стратегія повинна мати:
        self.candles: Dict[str, List[Candle]]
        self.timeframe_seconds: int
        self.bingx_client

    За потреби стратегія може перевизначити:
        _min_candles_needed()  -> скільки закритих свічок їй треба (за замовч. 2)
        _max_candles_buffer()  -> скільки свічок тримати в буфері (за замовч. 100)
    """

    _INTERVAL_MAP = {
        60: '1m', 180: '3m', 300: '5m', 900: '15m',
        1800: '30m', 3600: '1h', 14400: '4h', 86400: '1d',
    }

    def _min_candles_needed(self) -> int:
        return 2

    def _max_candles_buffer(self) -> int:
        return 100

    async def _ensure_warmup(self, symbol: str) -> None:
        if not hasattr(self, '_warmup_done'):
            self._warmup_done: Dict[str, bool] = {}

        # Прапорець перевіряємо і виставляємо ДО будь-яких інших дій —
        # і для випадку "клієнта немає", і для реального прогріву.
        # Інакше попередження про відсутність клієнта сипалось би на
        # КОЖЕН тік для КОЖНОГО символу нескінченно.
        if self._warmup_done.get(symbol, False):
            return

        bingx_client = getattr(self, 'bingx_client', None)
        if bingx_client is None:
            self._warmup_done[symbol] = True
            logger.warning(
                f"[{symbol}] warmup: bingx_client не передано в конструктор стратегії, "
                f"прогрів пропущено — стратегія накопичить свічки наживо, як і раніше "
                f"(це повідомлення більше не повториться для цього символу)"
            )
            return

        self._warmup_done[symbol] = True
        try:
            await self.warmup_from_exchange(bingx_client, symbol)
        except Exception as e:
            logger.error(f"[{symbol}] warmup: неочікувана помилка під час автопрогріву: {e}")

    async def warmup_from_exchange(self, bingx_client, symbol: str) -> bool:
        """
        Підвантажує історичні свічки з біржі, щоб стратегія була готова
        одразу, без очікування накопичення живих тіків.
        """
        if not hasattr(self, 'candles'):
            self.candles: Dict[str, List[Candle]] = {}

        if symbol in self.candles and self.candles[symbol]:
            logger.info(f"[{symbol}] warmup: історія вже є ({len(self.candles[symbol])} свічок), пропускаю")
            return True

        interval = self._INTERVAL_MAP.get(self.timeframe_seconds)
        if interval is None:
            logger.warning(
                f"[{symbol}] warmup: немає мапінгу BingX-інтервалу для "
                f"timeframe_seconds={self.timeframe_seconds}, прогрів пропущено — "
                f"стратегія накопичить свічки наживо, як і раніше"
            )
            return False

        min_needed = self._min_candles_needed()
        limit = min_needed + 5

        logger.info(
            f"[{symbol}] warmup: запит {limit} свічок interval={interval} "
            f"(потрібно мінімум {min_needed})"
        )

        try:
            raw = await bingx_client.get_klines(symbol=symbol, interval=interval, limit=limit)
        except Exception as e:
            logger.error(f"[{symbol}] warmup: не вдалось отримати klines: {e}")
            return False

        if len(raw) < min_needed:
            logger.warning(
                f"[{symbol}] warmup: біржа дала лише {len(raw)}/{min_needed} свічок — "
                f"решту стратегія дочекається наживо, як і раніше"
            )

        # останню (ще не закриту) свічку не беремо — вона стане current_candle
        # і буде доростати живими тіками, як завжди
        closed = raw[:-1] if len(raw) > 1 else []

        candles: List[Candle] = []
        for k in closed:
            c = Candle(float(k['open']), float(k['time']) / 1000)
            c.high = float(k['high'])
            c.low = float(k['low'])
            c.close = float(k['close'])
            candles.append(c)

        max_buffer = self._max_candles_buffer()
        self.candles[symbol] = candles[-max_buffer:]

        ready = len(self.candles[symbol]) >= min_needed
        closes = [c.close for c in self.candles[symbol]]

        if closes:
            logger.info(
                f"[{symbol}] warmup DONE: {len(self.candles[symbol])} свічок завантажено. "
                f"close: min={min(closes):.6f} max={max(closes):.6f} last={closes[-1]:.6f}. "
                f"Статус: {'ГОТОВА видавати сигнали одразу' if ready else 'ще потрібно донакопичити наживо'}"
            )
        else:
            logger.warning(f"[{symbol}] warmup: після обробки не залишилось жодної закритої свічки")

        return ready