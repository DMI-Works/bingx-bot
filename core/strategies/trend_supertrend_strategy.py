"""
TrendSupertrendStrategy — трендова мульти-таймфреймова стратегія.

Логіка входу (рахується ОДИН раз на закриття свічки робочого ТФ, лише по
ЗАКРИТИХ свічках — без ремалювання):
  1. Глобальний тренд (старший ТФ, за замовчуванням 1h): EMA(200).
       LONG  дозволений, лише якщо close > EMA200
       SHORT дозволений, лише якщо close < EMA200
  2. Сигнал (робочий ТФ, за замовчуванням 15m):
       Supertrend(10, 3) РОЗВЕРНУВСЯ у бік угоди (на останній закритій
       свічці напрям змінився) І volume свічки > SMA(volume, 20).

Стоп і розмір:
  - початковий SL = ціна входу ∓ 1.5 * ATR(14) робочого ТФ;
  - quantity тут НЕ рахується (стратегія не має доступу до балансу): у
    сигналі передається risk_percent, а SimpleTrader.open_position бере
    quantity з RiskManager.compute_risk_based_quantity (= equity * risk% /
    дистанція до SL) — той самий код, що вже є для use_risk_based_sizing;
  - тейк-профіту НЕМАЄ: сигнал без take_profit_levels, вихід — лише по
    стопу. Супровід стопу (3 кроки) робить TrailingStopManager у режимі
    atr_3step — параметри передаються в сигналі в trail_meta і зберігаються
    разом із позицією (metadata в БД).

За аналогією з WallBreakoutStrategy analyze() тут завжди повертає None, а
сигнал публікується з фонової задачі: EventBus обробляє події ПОСЛІДОВНО в
одній черзі, тому REST-запит за свічками всередині обробника PRICE_UPDATED
заблокував би всю шину (включно з ORDER_FILLED) на час запиту.

Як і всі стратегії в реєстрі, за замовчуванням вимкнена (enabled=False),
доки її не ввімкнули в мініаппі.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Dict, List, Optional, Set

from .registry import register_strategy
from .base_strategy import BaseStrategy
from .indicators import atr, ema, interval_to_ms, sma, supertrend
from ..events import EventBus, Event, EventType

logger = logging.getLogger(__name__)

# скільки свічок тягнемо: ATR/Supertrend/EMA — рекурсивні, їм потрібен "розгін"
# понад мінімальний період, інакше значення ще не зійшлися до біржових
_ENTRY_KLINES_LIMIT = 200
_TREND_KLINES_LIMIT = 500

# свічка може з'явитись у відповіді біржі з невеликою затримкою після
# закриття — скільки разів перепитуємо, перш ніж здатись до наступної свічки
_MAX_NOT_READY_ATTEMPTS = 5
_NOT_READY_RETRY_SECONDS = 3.0
_ERROR_RETRY_SECONDS = 10.0

# 24h-об'єми по всіх контрактах приходять одним запитом — кешуємо
_TICKERS_TTL_SECONDS = 900.0


@register_strategy('TrendSupertrendStrategy')
class TrendSupertrendStrategy(BaseStrategy):
    DEFAULT_PARAMS: Dict[str, object] = {
        'leverage': 10,
        # % від equity, який ризикуємо на угоду (SimpleTrader -> RiskManager)
        'risk_per_trade_percent': 1.0,
        'trend_timeframe': '1h',
        'entry_timeframe': '15m',
        'ema_period': 200,
        'supertrend_period': 10,
        'supertrend_multiplier': 3.0,
        'volume_sma_period': 20,
        'atr_period': 14,
        'atr_stop_multiplier': 1.5,
        # --- захист від збиткових входів (розбір тестнет-угод 2026-10-08) ---
        # Мінімальний стоп у кратних комісії round-trip (2 * taker * ціна):
        # коли стоп порівнянний з комісією, будь-який збиток подвоюється, а
        # позиція (при сайзингу за ризиком) роздувається. 5 => стоп >= ~0.5%.
        'min_stop_fee_multiple': 5.0,
        'taker_fee_rate': 0.0005,
        # Фільтр монет саме для цієї стратегії (SymbolSelector спільний із
        # WallBreakoutStrategy і підбирає під "стіни", а не під тренд):
        # мінімальний 24h-оборот в USDT (0 = вимкнено) ...
        'min_quote_volume_24h_usdt': 20_000_000,
        # ... і префікси не-крипто контрактів BingX через кому
        # (NCFX — форекс, NCCO — сировина, NCSK — акції, NCSI — індекси)
        'excluded_symbol_prefixes': 'NCFX,NCCO,NCSK,NCSI',
        # Сигнал живе лише N секунд після закриття свічки: повтори після
        # помилок REST не повинні виконати вхід за вже застарілою ціною
        'max_signal_age_seconds': 90,
        # --- параметри супроводу стопу (читає TrailingStopManager з trail_meta
        # позиції; всі кратні ATR, виміряному в момент входу) ---
        # крок 1: профіт >= N*ATR -> SL на ціну входу + комісії
        'trail_breakeven_atr': 1.0,
        # крок 2: профіт >= N*ATR -> SL на ціну входу ± trail_lock_offset_atr*ATR
        'trail_lock_trigger_atr': 2.0,
        'trail_lock_offset_atr': 1.0,
        # крок 3: SL за екстремумом останніх N закритих свічок із зазором N*ATR
        'trail_gap_atr': 0.5,
        'trail_lookback_candles': 3,
    }

    def __init__(self, event_bus: EventBus, config: dict, bingx_client=None):
        super().__init__("TrendSupertrendStrategy", event_bus, config)
        self.bingx_client = bingx_client

        try:
            self._params = self._parse_params(config)
        except ValueError as e:
            # некоректні значення в БД не повинні валити старт усього бота
            logger.error(
                f"TrendSupertrendStrategy: invalid params in config ({e}) — "
                f"falling back to DEFAULT_PARAMS"
            )
            self._params = self._parse_params(self.DEFAULT_PARAMS)

        self._last_bucket: Dict[str, int] = {}
        self._last_price: Dict[str, float] = {}
        self._inflight: Set[str] = set()
        self._retry_after: Dict[str, float] = {}
        self._not_ready_attempts: Dict[str, int] = {}
        # symbol -> (номер годинної свічки, EMA200) — старший ТФ міняється
        # рідко, тому не ходимо за ним на кожну свічку робочого ТФ
        self._trend_cache: Dict[str, tuple] = {}
        # (час завантаження, {symbol: 24h quoteVolume}) + антидубль логу відсіву
        self._volume_cache: tuple = (0.0, {})
        self._volume_lock = asyncio.Lock()
        self._liquidity_logged: Set[str] = set()
        self._excluded_prefixes: tuple = ()
        self._refresh_derived_params()
        self._tasks: Set[asyncio.Task] = set()
        self._klines_semaphore = asyncio.Semaphore(3)
        self._warned_no_client = False

        logger.info(
            f"TrendSupertrendStrategy: entry_tf={self._params['entry_timeframe']}, "
            f"trend_tf={self._params['trend_timeframe']}, "
            f"EMA{self._params['ema_period']}, "
            f"Supertrend({self._params['supertrend_period']}, {self._params['supertrend_multiplier']}), "
            f"SL={self._params['atr_stop_multiplier']}*ATR({self._params['atr_period']}), "
            f"risk={self._params['risk_per_trade_percent']}%/trade, leverage={self._params['leverage']}x"
        )

    @classmethod
    def build_config(cls, app_config) -> dict:
        return copy.deepcopy(cls.DEFAULT_PARAMS)

    @classmethod
    def _parse_params(cls, raw: dict) -> dict:
        """Збирає параметри з дефолтами і перевіряє їх. ValueError — на
        будь-яке некоректне значення (напр. таймфрейм, введений в мініаппі
        як довільний текст)."""
        d = cls.DEFAULT_PARAMS
        p = {key: raw.get(key, default) for key, default in d.items()}

        for key in ('entry_timeframe', 'trend_timeframe'):
            interval_to_ms(p[key])  # кидає ValueError

        positive_ints = ('ema_period', 'supertrend_period', 'volume_sma_period',
                         'atr_period', 'trail_lookback_candles', 'leverage')
        for key in positive_ints:
            if isinstance(p[key], bool) or not isinstance(p[key], (int, float)) or int(p[key]) < 1:
                raise ValueError(f"{key} must be >= 1, got {p[key]!r}")
            p[key] = int(p[key])

        positive_floats = ('supertrend_multiplier', 'atr_stop_multiplier', 'risk_per_trade_percent',
                           'trail_breakeven_atr', 'trail_lock_trigger_atr',
                           'trail_lock_offset_atr', 'trail_gap_atr',
                           'min_stop_fee_multiple', 'taker_fee_rate', 'max_signal_age_seconds')
        for key in positive_floats:
            if isinstance(p[key], bool) or not isinstance(p[key], (int, float)) or float(p[key]) <= 0:
                raise ValueError(f"{key} must be > 0, got {p[key]!r}")
            p[key] = float(p[key])

        if p['risk_per_trade_percent'] > 10:
            raise ValueError(f"risk_per_trade_percent={p['risk_per_trade_percent']} looks unsafe (> 10)")

        volume = p['min_quote_volume_24h_usdt']
        if isinstance(volume, bool) or not isinstance(volume, (int, float)) or float(volume) < 0:
            raise ValueError(f"min_quote_volume_24h_usdt must be >= 0, got {volume!r}")
        p['min_quote_volume_24h_usdt'] = float(volume)

        if not isinstance(p['excluded_symbol_prefixes'], str):
            raise ValueError(f"excluded_symbol_prefixes must be a comma-separated string, got {p['excluded_symbol_prefixes']!r}")

        return p

    def update_config(self, new_config: dict) -> None:
        try:
            params = self._parse_params(new_config)
        except ValueError as e:
            logger.error(f"TrendSupertrendStrategy.update_config: rejected invalid params ({e}) — keeping old")
            return
        self.config = new_config
        self._params = params
        self._refresh_derived_params()
        # кеш тренду міг бути пораховано зі старих параметрів
        self._trend_cache.clear()

    def _refresh_derived_params(self) -> None:
        self._excluded_prefixes = tuple(
            x.strip().upper() for x in str(self._params['excluded_symbol_prefixes']).split(',') if x.strip()
        )

    # ---------- тригер: нова свічка робочого ТФ ----------

    async def analyze(self, symbol: str, price: float) -> Optional[dict]:
        self._last_price[symbol] = price

        if self._excluded_prefixes and symbol.upper().startswith(self._excluded_prefixes):
            return None  # не-крипто контракт (форекс/сировина/акції/індекси) — не торгуємо

        if self.bingx_client is None:
            if not self._warned_no_client:
                logger.warning("TrendSupertrendStrategy: bingx_client не передано — стратегія не може отримати свічки")
                self._warned_no_client = True
            return None

        entry_ms = interval_to_ms(self._params['entry_timeframe'])
        bucket = int(time.time() * 1000) // entry_ms
        previous_bucket = self._last_bucket.get(symbol)

        if bucket == previous_bucket or symbol in self._inflight:
            return None
        if time.time() < self._retry_after.get(symbol, 0.0):
            return None

        self._last_bucket[symbol] = bucket
        if previous_bucket is None:
            # перший тік по символу після старту/ротації: лише запам'ятовуємо
            # поточну свічку. Сигнал ловимо тільки на ЖИВОМУ переході між
            # свічками — інакше після кожного рестарту бот міг би зайти за
            # розворотом, що відбувся до 1 ТФ тому.
            return None

        self._inflight.add(symbol)
        task = asyncio.create_task(self._evaluate(symbol, bucket, previous_bucket, entry_ms))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return None

    async def _evaluate(self, symbol: str, bucket: int, previous_bucket: int, entry_ms: int) -> None:
        retry_seconds: Optional[float] = None
        try:
            if self._signal_age_exceeded(bucket, entry_ms):
                logger.info(f"[{symbol}] TrendSupertrend: сигнал застарів ще до перевірки — пропускаю")
                return

            async with self._klines_semaphore:
                result = await self._check_signal(symbol, bucket, entry_ms)

            if isinstance(result, dict) and self._signal_age_exceeded(bucket, entry_ms):
                logger.warning(
                    f"[{symbol}] TrendSupertrend: сигнал {result['side']} відкинуто — старший за "
                    f"{self._params['max_signal_age_seconds']:g}с від закриття свічки (вхід був би за застарілою ціною)"
                )
                return

            if result == 'not_ready':
                attempts = self._not_ready_attempts.get(symbol, 0) + 1
                if attempts < _MAX_NOT_READY_ATTEMPTS:
                    self._not_ready_attempts[symbol] = attempts
                    retry_seconds = _NOT_READY_RETRY_SECONDS
                else:
                    logger.info(f"[{symbol}] TrendSupertrend: закрита свічка так і не з'явилась у відповіді біржі — пропускаю")
                    self._not_ready_attempts.pop(symbol, None)
            else:
                self._not_ready_attempts.pop(symbol, None)
                if result:
                    await self.event_bus.publish(Event(
                        type=EventType.SIGNAL_GENERATED,
                        data=result,
                        source=self.name,
                    ))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{symbol}] TrendSupertrend: evaluation failed: {e}", exc_info=True)
            retry_seconds = _ERROR_RETRY_SECONDS
        finally:
            if retry_seconds is not None:
                if self._signal_age_exceeded(bucket, entry_ms):
                    # повторювати вже пізно: сигнал свічки мертвий, чекаємо наступну
                    logger.info(f"[{symbol}] TrendSupertrend: повтор скасовано — сигнал свічки застарів")
                else:
                    # відкочуємо номер свічки, щоб наступний тік повторив перевірку
                    self._last_bucket[symbol] = previous_bucket
                    self._retry_after[symbol] = time.time() + retry_seconds
            self._inflight.discard(symbol)

    def _signal_age_exceeded(self, bucket: int, entry_ms: int) -> bool:
        """Вік сигналу рахується від ЗАКРИТТЯ сигнальної свічки (= початок bucket)."""
        age_seconds = time.time() - (bucket * entry_ms) / 1000.0
        return age_seconds > self._params['max_signal_age_seconds']

    # ---------- розрахунок сигналу ----------

    async def _check_signal(self, symbol: str, bucket: int, entry_ms: int):
        """Повертає dict-сигнал, None (немає сигналу) або 'not_ready'."""
        p = self._params
        bucket_start_ms = bucket * entry_ms

        # дешевий відсів ДО запиту свічок (оборот кешується на 15 хв)
        if not await self._passes_liquidity_filter(symbol):
            return None

        klines = await self.bingx_client.get_klines(symbol, p['entry_timeframe'], limit=_ENTRY_KLINES_LIMIT)
        closed = [k for k in klines if int(k['time']) < bucket_start_ms]

        # остання закрита свічка МАЄ бути саме попередньою до поточної —
        # інакше біржа ще не віддала її (або по символу не було угод)
        if len(closed) < 2 or int(closed[-1]['time']) != bucket_start_ms - entry_ms:
            return 'not_ready'

        highs = [float(k['high']) for k in closed]
        lows = [float(k['low']) for k in closed]
        closes = [float(k['close']) for k in closed]
        volumes = [float(k['volume']) for k in closed]

        direction, _ = supertrend(highs, lows, closes, p['supertrend_period'], p['supertrend_multiplier'])
        current_dir, previous_dir = direction[-1], direction[-2]
        if current_dir is None or previous_dir is None or current_dir == previous_dir:
            return None  # розвороту на останній закритій свічці немає

        side = 'LONG' if current_dir == 1 else 'SHORT'

        atr_value = atr(highs, lows, closes, p['atr_period'])[-1]
        volume_sma = sma(volumes, p['volume_sma_period'])[-1]
        if atr_value is None or atr_value <= 0 or volume_sma is None:
            logger.info(f"[{symbol}] TrendSupertrend: недостатньо історії для ATR/SMA(volume) — пропускаю")
            return None

        if volumes[-1] <= volume_sma:
            logger.info(
                f"[{symbol}] TrendSupertrend SKIP {side}: розворот Supertrend є, але об'єм "
                f"{volumes[-1]:.4f} <= SMA({p['volume_sma_period']}) {volume_sma:.4f}"
            )
            return None

        trend_ema = await self._get_trend_ema(symbol)
        if trend_ema is None:
            return None

        close = closes[-1]
        if (side == 'LONG' and not close > trend_ema) or (side == 'SHORT' and not close < trend_ema):
            logger.info(
                f"[{symbol}] TrendSupertrend SKIP {side}: проти глобального тренду "
                f"(close={close:.6f}, EMA{p['ema_period']}@{p['trend_timeframe']}={trend_ema:.6f})"
            )
            return None

        price = self._last_price.get(symbol) or close
        stop_distance = p['atr_stop_multiplier'] * atr_value
        stop_loss_price = price - stop_distance if side == 'LONG' else price + stop_distance
        if stop_loss_price <= 0:
            logger.warning(f"[{symbol}] TrendSupertrend SKIP {side}: SL <= 0 (price={price}, ATR={atr_value})")
            return None

        round_trip_fee = 2.0 * p['taker_fee_rate'] * price
        if stop_distance < p['min_stop_fee_multiple'] * round_trip_fee:
            logger.info(
                f"[{symbol}] TrendSupertrend SKIP {side}: стоп {stop_distance / price * 100:.3f}% "
                f"< {p['min_stop_fee_multiple']:g}× комісії round-trip ({round_trip_fee / price * 100:.3f}%) — "
                f"волатильність монети замала для цієї стратегії"
            )
            return None

        logger.info(
            f"[{symbol}] TrendSupertrend SIGNAL: {side} price={price:.6f}, ATR={atr_value:.6f}, "
            f"SL={stop_loss_price:.6f}, EMA{p['ema_period']}={trend_ema:.6f}, "
            f"vol={volumes[-1]:.4f} > SMA={volume_sma:.4f}"
        )

        return {
            'action': 'OPEN',
            'symbol': symbol,
            'side': side,
            # quantity считает SimpleTrader по risk_percent (см. докстринг модуля)
            'quantity': None,
            'risk_percent': p['risk_per_trade_percent'],
            'leverage': p['leverage'],
            'stop_loss_price': stop_loss_price,
            'strategy': self.name,
            'reference_price': price,
            'trail_meta': {
                'mode': 'atr_3step',
                'atr': atr_value,
                'interval': p['entry_timeframe'],
                'breakeven_atr': p['trail_breakeven_atr'],
                'lock_trigger_atr': p['trail_lock_trigger_atr'],
                'lock_offset_atr': p['trail_lock_offset_atr'],
                'gap_atr': p['trail_gap_atr'],
                'lookback': p['trail_lookback_candles'],
            },
            'reason': (
                f"Supertrend розвернувся у {side}, ціна {'вище' if side == 'LONG' else 'нижче'} "
                f"EMA{p['ema_period']} ({p['trend_timeframe']}), об'єм вище SMA({p['volume_sma_period']}), "
                f"ATR={atr_value:.6f}"
            ),
        }

    async def _passes_liquidity_filter(self, symbol: str) -> bool:
        min_volume = self._params['min_quote_volume_24h_usdt']
        if min_volume <= 0:
            return True

        async with self._volume_lock:
            fetched_at, volumes = self._volume_cache
            if time.time() - fetched_at > _TICKERS_TTL_SECONDS:
                tickers = await self.bingx_client.get_all_tickers()  # помилка -> повтор через _evaluate
                volumes = {}
                for ticker in tickers:
                    try:
                        volumes[ticker['symbol']] = float(ticker.get('quoteVolume', 0))
                    except (KeyError, TypeError, ValueError):
                        continue
                self._volume_cache = (time.time(), volumes)
                self._liquidity_logged.clear()

        volume = volumes.get(symbol)
        if volume is not None and volume >= min_volume:
            return True

        if symbol not in self._liquidity_logged:
            self._liquidity_logged.add(symbol)
            logger.info(
                f"[{symbol}] TrendSupertrend: монета відсіяна за ліквідністю — 24h оборот "
                f"{'невідомий' if volume is None else f'{volume:,.0f}'} USDT < {min_volume:,.0f}"
            )
        return False

    async def _get_trend_ema(self, symbol: str) -> Optional[float]:
        p = self._params
        trend_ms = interval_to_ms(p['trend_timeframe'])
        now_ms = int(time.time() * 1000)
        trend_bucket = now_ms // trend_ms

        cached = self._trend_cache.get(symbol)
        if cached and cached[0] == trend_bucket:
            return cached[1]

        klines = await self.bingx_client.get_klines(symbol, p['trend_timeframe'], limit=_TREND_KLINES_LIMIT)
        # лише закриті свічки старшого ТФ
        closes: List[float] = [float(k['close']) for k in klines if int(k['time']) + trend_ms <= now_ms]

        # EMA(200) потребує сід + розгін, інакше значення неточне
        min_candles = p['ema_period'] + 50
        if len(closes) < min_candles:
            logger.info(
                f"[{symbol}] TrendSupertrend: мало історії {p['trend_timeframe']} для EMA{p['ema_period']} "
                f"({len(closes)} < {min_candles}) — монета пропускається"
            )
            return None

        value = ema(closes, p['ema_period'])[-1]
        if value is None:
            return None
        self._trend_cache[symbol] = (trend_bucket, value)
        return value
