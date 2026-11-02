"""
TsMomentumStrategy — дневной time-series momentum (TSMOM).

Академически устойчивый эффект (сильнее кросс-секционного момента). На нашей
вселенной (scripts/tsmom_backtest.py, 1d, ~2.7 года) окно 60 дней / long-short /
vol-target дало +16%/год, Sharpe 0.64, плюс во всех трёх третях истории. Это
риск-премия с УМЕРЕННЫМ ожиданием, не «грааль»: Sharpe скромный, доходность по
третям затухает (компрессия трендов с 2024).

Логика (раз в сутки на закрытии дневной свечи, только по закрытым свечам):
  1. Сигнал = знак доходности за lookback_days: mom = close[-1]/close[-1-L]-1.
       desired = LONG если mom>0, SHORT если mom<0 (long_short), либо FLAT вместо
       SHORT (long_flat).
  2. Удержание ДО РАЗВОРОТА сигнала (не по ценовому стопу — суть TSMOM). Ежедневно
     сверяем desired с текущей позицией по символу:
       нет позиции + сигнал есть -> OPEN (нужная сторона);
       позиция есть, знак сменился -> CLOSE по рынку (close_callback); противоположную
         откроем на следующей сверке (без гонки close+open в одном тике);
       знак совпал -> держим.
  3. Сайзинг = vol-target через риск-сайзинг: quantity=None + risk_percent +
     stop_loss_price = entry ∓ disaster_stop_atr*ATR. При стопе ∝ ATR размер
     ∝ 1/волатильность = vol-target. Дизастер-стоп широкий — только страховка от
     гэпа, не рабочий выход. TP нет.

Как и TrendSupertrendStrategy: analyze() возвращает None, а работа идёт в фоновой
задаче (REST за свечами нельзя делать синхронно в обработчике шины). За
символами ходим только по СВОЕМУ стабильному пулу (set_symbol_pool_provider) —
пул НЕ должен ротироваться, иначе символ уедет из подписки и позицией нельзя
управлять. По умолчанию стратегия выключена (enabled=False).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Callable, Dict, Optional, Set

from .registry import register_strategy
from .base_strategy import BaseStrategy
from .indicators import atr, interval_to_ms, pct_return
from ..common.position_math import position_key, side_sign
from ..events import EventBus, Event, EventType

logger = logging.getLogger(__name__)

# свечей тянем с запасом: lookback + разгон ATR + буфер
_KLINES_BUFFER = 60

_MAX_NOT_READY_ATTEMPTS = 5
_NOT_READY_RETRY_SECONDS = 3.0
_ERROR_RETRY_SECONDS = 10.0
_TICKERS_TTL_SECONDS = 900.0


@register_strategy('TsMomentumStrategy')
class TsMomentumStrategy(BaseStrategy):
    DEFAULT_PARAMS: Dict[str, object] = {
        'leverage': 3,                       # потолок плеча (vol-target ограничен им)
        'risk_per_trade_percent': 1.0,       # % equity на сделку (vol-target)
        'timeframe': '1d',
        'lookback_days': 60,                 # окно моментума (лучшее по бэктесту)
        'mode': 'long_short',                # 'long_short' | 'long_flat'
        'atr_period': 14,
        # дизастер-стоп (× ATR) — страховка от гэпа, НЕ рабочий выход; он же
        # задаёт дистанцию для риск-сайзинга, т.е. vol-target размер позиции
        'disaster_stop_atr': 4.0,
        # фильтры монеты (как в TrendSupertrendStrategy)
        'min_quote_volume_24h_usdt': 20_000_000,
        'excluded_symbol_prefixes': 'NCFX,NCCO,NCSK,NCSI',
        # сигнал живёт N секунд после закрытия дневной свечи
        'max_signal_age_seconds': 3600,
    }

    def __init__(self, event_bus: EventBus, config: dict, bingx_client=None):
        super().__init__("TsMomentumStrategy", event_bus, config)
        self.bingx_client = bingx_client

        try:
            self._params = self._parse_params(config)
        except ValueError as e:
            logger.error(
                f"TsMomentumStrategy: invalid params in config ({e}) — falling back to DEFAULT_PARAMS"
            )
            self._params = self._parse_params(self.DEFAULT_PARAMS)

        self._last_bucket: Dict[str, int] = {}
        self._last_price: Dict[str, float] = {}
        self._inflight: Set[str] = set()
        self._retry_after: Dict[str, float] = {}
        self._not_ready_attempts: Dict[str, int] = {}
        self._volume_cache: tuple = (0.0, {})
        self._volume_lock = asyncio.Lock()
        self._liquidity_logged: Set[str] = set()
        self._excluded_prefixes: tuple = ()
        self._refresh_derived_params()
        self._tasks: Set[asyncio.Task] = set()
        self._klines_semaphore = asyncio.Semaphore(3)
        self._warned_no_client = False

        # Набор символов СВОЕГО стабильного пула (см. main.py).
        self._symbol_pool_provider: Optional[Callable[[], Set[str]]] = None
        # Текущая позиция по (symbol, side) — обычно trader.open_positions.get(...).
        self._position_provider: Optional[Callable[[str, str], Optional[dict]]] = None
        # Закрытие позиции по флипу сигнала — обычно trader.close_position.
        self._close_callback: Optional[Callable[[str, str, str], object]] = None

        logger.info(
            f"TsMomentumStrategy: tf={self._params['timeframe']}, lookback={self._params['lookback_days']}d, "
            f"mode={self._params['mode']}, disaster_stop={self._params['disaster_stop_atr']}*ATR, "
            f"risk={self._params['risk_per_trade_percent']}%/trade, leverage<= {self._params['leverage']}x"
        )

    @classmethod
    def build_config(cls, app_config) -> dict:
        return copy.deepcopy(cls.DEFAULT_PARAMS)

    @classmethod
    def _parse_params(cls, raw: dict) -> dict:
        d = cls.DEFAULT_PARAMS
        p = {key: raw.get(key, default) for key, default in d.items()}

        interval_to_ms(p['timeframe'])  # ValueError на мусорный таймфрейм

        positive_ints = ('lookback_days', 'atr_period', 'leverage')
        for key in positive_ints:
            if isinstance(p[key], bool) or not isinstance(p[key], (int, float)) or int(p[key]) < 1:
                raise ValueError(f"{key} must be >= 1, got {p[key]!r}")
            p[key] = int(p[key])

        positive_floats = ('risk_per_trade_percent', 'disaster_stop_atr', 'max_signal_age_seconds')
        for key in positive_floats:
            if isinstance(p[key], bool) or not isinstance(p[key], (int, float)) or float(p[key]) <= 0:
                raise ValueError(f"{key} must be > 0, got {p[key]!r}")
            p[key] = float(p[key])

        if p['risk_per_trade_percent'] > 10:
            raise ValueError(f"risk_per_trade_percent={p['risk_per_trade_percent']} looks unsafe (> 10)")

        if p['mode'] not in ('long_short', 'long_flat'):
            raise ValueError(f"mode must be 'long_short' or 'long_flat', got {p['mode']!r}")

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
            logger.error(f"TsMomentumStrategy.update_config: rejected invalid params ({e}) — keeping old")
            return
        self.config = new_config
        self._params = params
        self._refresh_derived_params()

    def _refresh_derived_params(self) -> None:
        self._excluded_prefixes = tuple(
            x.strip().upper() for x in str(self._params['excluded_symbol_prefixes']).split(',') if x.strip()
        )

    # ---------- подключение к живым объектам бота (main.py) ----------

    def set_symbol_pool_provider(self, provider: Optional[Callable[[], Set[str]]]) -> None:
        self._symbol_pool_provider = provider

    def set_position_provider(self, provider: Optional[Callable[[str, str], Optional[dict]]]) -> None:
        self._position_provider = provider

    def set_close_callback(self, callback: Optional[Callable[[str, str, str], object]]) -> None:
        self._close_callback = callback

    # ---------- триггер: новая дневная свеча ----------

    async def analyze(self, symbol: str, price: float) -> Optional[dict]:
        self._last_price[symbol] = price

        if self._symbol_pool_provider is not None:
            try:
                if symbol not in self._symbol_pool_provider():
                    return None
            except Exception:
                logger.error("TsMomentum: symbol_pool_provider failed", exc_info=True)
                return None

        if self._excluded_prefixes and symbol.upper().startswith(self._excluded_prefixes):
            return None

        if self.bingx_client is None:
            if not self._warned_no_client:
                logger.warning("TsMomentumStrategy: bingx_client не передан — стратегия не может получить свечи")
                self._warned_no_client = True
            return None

        tf_ms = interval_to_ms(self._params['timeframe'])
        bucket = int(time.time() * 1000) // tf_ms
        previous_bucket = self._last_bucket.get(symbol)

        if bucket == previous_bucket or symbol in self._inflight:
            return None
        if time.time() < self._retry_after.get(symbol, 0.0):
            return None

        self._last_bucket[symbol] = bucket
        if previous_bucket is None:
            # первый тик после старта/ротации: только запоминаем текущую свечу,
            # сверку делаем начиная со следующего дневного бакета
            return None

        self._inflight.add(symbol)
        task = asyncio.create_task(self._evaluate(symbol, bucket, previous_bucket, tf_ms))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return None

    async def _evaluate(self, symbol: str, bucket: int, previous_bucket: int, tf_ms: int) -> None:
        retry_seconds: Optional[float] = None
        try:
            if self._signal_age_exceeded(bucket, tf_ms):
                logger.info(f"[{symbol}] TsMomentum: сигнал устарел ещё до проверки — пропускаю")
                return

            async with self._klines_semaphore:
                result = await self._reconcile(symbol, bucket, tf_ms)

            if isinstance(result, dict) and self._signal_age_exceeded(bucket, tf_ms):
                logger.warning(
                    f"[{symbol}] TsMomentum: сигнал {result['side']} отброшен — старше "
                    f"{self._params['max_signal_age_seconds']:g}с от закрытия свечи"
                )
                return

            if result == 'not_ready':
                attempts = self._not_ready_attempts.get(symbol, 0) + 1
                if attempts < _MAX_NOT_READY_ATTEMPTS:
                    self._not_ready_attempts[symbol] = attempts
                    retry_seconds = _NOT_READY_RETRY_SECONDS
                else:
                    logger.info(f"[{symbol}] TsMomentum: дневная свеча так и не появилась — пропускаю")
                    self._not_ready_attempts.pop(symbol, None)
            else:
                self._not_ready_attempts.pop(symbol, None)
                if isinstance(result, dict):
                    await self.event_bus.publish(Event(
                        type=EventType.SIGNAL_GENERATED, data=result, source=self.name,
                    ))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[{symbol}] TsMomentum: evaluation failed: {e}", exc_info=True)
            retry_seconds = _ERROR_RETRY_SECONDS
        finally:
            if retry_seconds is not None:
                if self._signal_age_exceeded(bucket, tf_ms):
                    logger.info(f"[{symbol}] TsMomentum: повтор отменён — сигнал устарел")
                else:
                    self._last_bucket[symbol] = previous_bucket
                    self._retry_after[symbol] = time.time() + retry_seconds
            self._inflight.discard(symbol)

    def _signal_age_exceeded(self, bucket: int, tf_ms: int) -> bool:
        age_seconds = time.time() - (bucket * tf_ms) / 1000.0
        return age_seconds > self._params['max_signal_age_seconds']

    # ---------- сверка сигнала с позицией ----------

    def _current_side(self, symbol: str) -> Optional[str]:
        """'LONG'/'SHORT' — если у стратегии есть открытая позиция по символу, иначе None.
        Считаем только свои (strategy==self.name) позиции, чтобы не трогать чужие."""
        if self._position_provider is None:
            return None
        for side in ('LONG', 'SHORT'):
            pos = None
            try:
                pos = self._position_provider(symbol, side)
            except Exception:
                logger.error("TsMomentum: position_provider failed", exc_info=True)
                return None
            if pos and pos.get('strategy') == self.name:
                return side
        return None

    async def _reconcile(self, symbol: str, bucket: int, tf_ms: int):
        """Возвращает dict-сигнал OPEN, None или 'not_ready'. Закрытие по флипу
        делает сразу через close_callback (не через возвращаемое значение)."""
        p = self._params
        bucket_start_ms = bucket * tf_ms

        if not await self._passes_liquidity_filter(symbol):
            return None

        limit = p['lookback_days'] + max(p['atr_period'], p['lookback_days']) + _KLINES_BUFFER
        klines = await self.bingx_client.get_klines(symbol, p['timeframe'], limit=limit)
        closed = [k for k in klines if int(k['time']) < bucket_start_ms]
        if len(closed) < p['lookback_days'] + 2 or int(closed[-1]['time']) != bucket_start_ms - tf_ms:
            return 'not_ready'

        highs = [float(k['high']) for k in closed]
        lows = [float(k['low']) for k in closed]
        closes = [float(k['close']) for k in closed]

        mom = pct_return(closes, p['lookback_days'])
        if mom is None:
            return None
        if mom > 0:
            desired = 'LONG'
        elif mom < 0:
            desired = 'SHORT'
        else:
            desired = None
        if desired == 'SHORT' and p['mode'] == 'long_flat':
            desired = None  # в long_flat шортов нет — остаёмся/выходим в кэш

        current = self._current_side(symbol)

        # 1) разворот сигнала или уход в кэш -> закрыть текущую позицию
        if current is not None and current != desired:
            if self._close_callback is not None:
                logger.info(
                    f"[{symbol}] TsMomentum CLOSE {current}: сигнал сменился "
                    f"(mom({p['lookback_days']}d)={mom*100:+.2f}% -> desired={desired})"
                )
                try:
                    await self._close_callback(symbol, current, 'tsmom_flip')
                except Exception:
                    logger.error(f"[{symbol}] TsMomentum: close_callback failed", exc_info=True)
            return None  # противоположную откроем на следующей дневной сверке

        # 2) уже в нужной стороне или нет сигнала -> ничего
        if desired is None or current == desired:
            logger.info(
                f"[{symbol}] TsMomentum HOLD: mom({p['lookback_days']}d)={mom*100:+.2f}%, "
                f"desired={desired}, current={current}"
            )
            return None

        # 3) позиции нет, есть сигнал -> OPEN с vol-target сайзингом
        atr_value = atr(highs, lows, closes, p['atr_period'])[-1]
        if atr_value is None or atr_value <= 0:
            logger.info(f"[{symbol}] TsMomentum: недостаточно истории для ATR — пропускаю")
            return None

        price = self._last_price.get(symbol) or closes[-1]
        stop_distance = p['disaster_stop_atr'] * atr_value
        stop_loss_price = price - stop_distance if desired == 'LONG' else price + stop_distance
        if stop_loss_price <= 0:
            logger.warning(f"[{symbol}] TsMomentum SKIP {desired}: disaster-SL <= 0 (price={price}, ATR={atr_value})")
            return None

        logger.info(
            f"[{symbol}] TsMomentum SIGNAL: {desired} price={price:.6f}, "
            f"mom({p['lookback_days']}d)={mom*100:+.2f}%, ATR={atr_value:.6f}, "
            f"disaster_SL={stop_loss_price:.6f}"
        )

        analytics_context = {
            'signal_candle_open_ms': int(closed[-1]['time']),
            'timeframe': p['timeframe'],
            'lookback_days': p['lookback_days'],
            'momentum_pct': mom * 100.0,
            'close': closes[-1],
            'atr': atr_value,
            'atr_pct': atr_value / closes[-1] * 100.0,
            'disaster_stop_distance_pct': stop_distance / price * 100.0,
            'mode': p['mode'],
        }

        return {
            'action': 'OPEN',
            'symbol': symbol,
            'side': desired,
            'analytics_context': analytics_context,
            'quantity': None,  # размер считает SimpleTrader по risk_percent (vol-target)
            'risk_percent': p['risk_per_trade_percent'],
            'leverage': p['leverage'],
            'stop_loss_price': stop_loss_price,
            # take_profit_levels НЕТ: выход — по развороту сигнала (close_callback),
            # дизастер-стоп лишь страхует от гэпа
            'strategy': self.name,
            'reference_price': price,
            'reason': (
                f"TSMOM {desired}: доходность за {p['lookback_days']}d = {mom*100:+.2f}% "
                f"(mode={p['mode']}), размер по vol-target, дизастер-SL {p['disaster_stop_atr']:g}*ATR"
            ),
        }

    async def _passes_liquidity_filter(self, symbol: str) -> bool:
        min_volume = self._params['min_quote_volume_24h_usdt']
        if min_volume <= 0:
            return True

        async with self._volume_lock:
            fetched_at, volumes = self._volume_cache
            if time.time() - fetched_at > _TICKERS_TTL_SECONDS:
                tickers = await self.bingx_client.get_all_tickers()
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
                f"[{symbol}] TsMomentum: монета отсеяна по ликвидности — 24h оборот "
                f"{'неизвестен' if volume is None else f'{volume:,.0f}'} USDT < {min_volume:,.0f}"
            )
        return False
