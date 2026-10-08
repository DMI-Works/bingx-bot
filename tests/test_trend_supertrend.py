"""
Юніт-тести: індикатори, TrendSupertrendStrategy, режим atr_3step у
TrailingStopManager, предохранитель серії стоп-лоссів у RiskManager.

Запуск: python -m unittest tests.test_trend_supertrend -v
Усі зовнішні залежності (біржа, БД, налаштування) — фейки; мережа не
використовується. MONGO_URI потрібен лише для імпорту core.database
(див. примітку в tests/test_pnl_accounting.py).
"""
import os
import time
import unittest
import unittest.mock

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")

from core.events import EventBus, EventType  # noqa: E402
from core.exchange.bingx_client import BingXAPIError  # noqa: E402
from core.risk import RiskManager, TrailingStopManager  # noqa: E402
from core.strategies.indicators import (  # noqa: E402
    atr, ema, interval_to_ms, sma, supertrend,
)
from core.strategies.trend_supertrend_strategy import TrendSupertrendStrategy  # noqa: E402
from core.trading.simple_trader import SimpleTrader  # noqa: E402


# ---------------------------------------------------------------- індикатори

class TestIndicators(unittest.TestCase):
    def test_sma_and_ema_seed(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(sma(values, 3), [None, None, 2.0, 3.0, 4.0])
        e = ema(values, 3)
        self.assertEqual(e[:2], [None, None])
        self.assertAlmostEqual(e[2], 2.0)                 # сід = SMA(3)
        self.assertAlmostEqual(e[3], 0.5 * 4 + 0.5 * 2)   # alpha = 0.5

    def test_atr_wilder(self):
        highs = [10, 11, 12, 13]
        lows = [8, 9, 10, 11]
        closes = [9, 10, 11, 12]
        a = atr(highs, lows, closes, 2)
        self.assertIsNone(a[0])
        # TR: 2, 2, 2, 2 -> ATR = 2
        self.assertAlmostEqual(a[1], 2.0)
        self.assertAlmostEqual(a[3], 2.0)

    def test_interval_to_ms(self):
        self.assertEqual(interval_to_ms('15m'), 900_000)
        self.assertEqual(interval_to_ms('1h'), 3_600_000)
        for bad in ('', 'h', '0m', '15x', 'abc', None):
            with self.assertRaises(ValueError):
                interval_to_ms(bad)

    def test_supertrend_flips_on_reversal(self):
        highs, lows, closes = _v_shaped_series()
        direction, _ = supertrend(highs, lows, closes, 10, 3.0)
        self.assertEqual(direction[40], -1)
        self.assertEqual(direction[-1], 1)
        # перші свічки після прогріву ATR — довільний стартовий напрям, тому
        # розвороти рахуємо вже із 20-ї свічки
        flips = [i for i in range(20, len(direction)) if direction[i] != direction[i - 1]]
        self.assertEqual(len(flips), 1)
        self.assertGreater(flips[0], 60)  # розворот ПІСЛЯ дна (після ~60-ї свічки)


def _v_shaped_series(n_down=60, n_up=60):
    """Спадний тренд 100 -> 70, потім зростання 70 -> 130."""
    closes = [100 - 0.5 * i for i in range(n_down)]
    closes += [closes[-1] + 1.0 * (i + 1) for i in range(n_up)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    return highs, lows, closes


# ------------------------------------------------------------------ стратегія

class FakeKlineClient:
    def __init__(self, entry_klines, trend_klines, quote_volume=1_000_000_000.0):
        self.entry_klines = entry_klines
        self.trend_klines = trend_klines
        self.quote_volume = quote_volume
        self.calls = []

    async def get_all_tickers(self):
        return [{'symbol': 'BTC-USDT', 'quoteVolume': str(self.quote_volume)}]

    async def get_klines(self, symbol, interval, limit=500, **kwargs):
        self.calls.append((symbol, interval, limit))
        return self.entry_klines if interval == '15m' else self.trend_klines


def _build_klines(highs, lows, closes, volumes, last_open_ms, tf_ms):
    n = len(closes)
    return [
        {
            'time': last_open_ms - (n - 1 - i) * tf_ms,
            'open': closes[i], 'high': highs[i], 'low': lows[i],
            'close': closes[i], 'volume': volumes[i],
        }
        for i in range(n)
    ]


def _flip_scenario(trend_close):
    """Свічки до моменту розвороту Supertrend у LONG + відповідні HTF-свічки."""
    highs, lows, closes = _v_shaped_series()
    direction, _ = supertrend(highs, lows, closes, 10, 3.0)
    flip = next(i for i in range(61, len(direction)) if direction[i] == 1 and direction[i - 1] == -1)
    highs, lows, closes = highs[:flip + 1], lows[:flip + 1], closes[:flip + 1]

    entry_ms = interval_to_ms('15m')
    trend_ms = interval_to_ms('1h')
    now_ms = int(time.time() * 1000)
    bucket = now_ms // entry_ms
    bucket_start = bucket * entry_ms

    volumes = [1.0] * len(closes)
    volumes[-1] = 5.0  # об'єм сигнальної свічки >> SMA(20)

    entry_klines = _build_klines(highs, lows, closes, volumes, bucket_start - entry_ms, entry_ms)
    # + ще не закрита (формується) свічка поточного bucket — має відфільтруватись
    entry_klines.append({'time': bucket_start, 'open': closes[-1], 'high': closes[-1] + 1,
                         'low': closes[-1] - 1, 'close': closes[-1], 'volume': 0.1})

    last_closed_trend_open = (now_ms // trend_ms) * trend_ms - trend_ms
    trend_closes = [trend_close] * 300
    trend_klines = _build_klines(
        [c + 1 for c in trend_closes], [c - 1 for c in trend_closes],
        trend_closes, [1.0] * 300, last_closed_trend_open, trend_ms,
    )
    return entry_klines, trend_klines, bucket, entry_ms, closes[-1]


class TestStrategySignal(unittest.IsolatedAsyncioTestCase):
    def _make(self, entry_klines, trend_klines, quote_volume=1_000_000_000.0, **overrides):
        client = FakeKlineClient(entry_klines, trend_klines, quote_volume)
        config = dict(TrendSupertrendStrategy.build_config(None), **overrides)
        strategy = TrendSupertrendStrategy(EventBus(), config, bingx_client=client)
        return strategy, client

    async def test_long_signal_contract(self):
        entry, trend, bucket, entry_ms, last_close = _flip_scenario(trend_close=50.0)  # close >> EMA
        strategy, _ = self._make(entry, trend)
        strategy._last_price['BTC-USDT'] = last_close

        signal = await strategy._check_signal('BTC-USDT', bucket, entry_ms)

        self.assertIsInstance(signal, dict)
        self.assertEqual(signal['action'], 'OPEN')
        self.assertEqual(signal['side'], 'LONG')
        self.assertIsNone(signal['quantity'])                 # рахує SimpleTrader по risk_percent
        self.assertEqual(signal['risk_percent'], 1.0)
        self.assertNotIn('take_profit_levels', signal)         # БЕЗ тейк-профіту
        meta = signal['trail_meta']
        self.assertEqual(meta['mode'], 'atr_3step')
        self.assertGreater(meta['atr'], 0)
        # SL = ціна - 1.5 * ATR
        self.assertAlmostEqual(signal['stop_loss_price'], last_close - 1.5 * meta['atr'], places=9)
        self.assertLess(signal['stop_loss_price'], signal['reference_price'])

    async def test_short_blocked_by_trend_filter_for_bullish_flip(self):
        # бичачий розворот, але ціна НИЖЧЕ EMA200 старшого ТФ -> LONG заборонено
        entry, trend, bucket, entry_ms, _ = _flip_scenario(trend_close=500.0)
        strategy, _ = self._make(entry, trend)
        self.assertIsNone(await strategy._check_signal('BTC-USDT', bucket, entry_ms))

    async def test_low_volume_blocks_signal(self):
        entry, trend, bucket, entry_ms, _ = _flip_scenario(trend_close=50.0)
        entry[-2]['volume'] = 0.5  # остання ЗАКРИТА свічка (передостання в списку) — об'єм нижче середнього
        strategy, _ = self._make(entry, trend)
        self.assertIsNone(await strategy._check_signal('BTC-USDT', bucket, entry_ms))

    async def test_missing_closed_candle_is_not_ready(self):
        entry, trend, bucket, entry_ms, _ = _flip_scenario(trend_close=50.0)
        entry = [k for k in entry if k['time'] < bucket * entry_ms - entry_ms]  # прибрали щойно закриту
        strategy, _ = self._make(entry, trend)
        self.assertEqual(await strategy._check_signal('BTC-USDT', bucket, entry_ms), 'not_ready')

    async def test_first_tick_only_primes_bucket(self):
        entry, trend, _, _, _ = _flip_scenario(trend_close=50.0)
        strategy, client = self._make(entry, trend)
        self.assertIsNone(await strategy.analyze('BTC-USDT', 100.0))
        self.assertEqual(client.calls, [])  # жодних запитів за свічками на першому тіку

    def test_invalid_params_rejected_on_update(self):
        strategy = TrendSupertrendStrategy(EventBus(), TrendSupertrendStrategy.build_config(None))
        bad = dict(TrendSupertrendStrategy.DEFAULT_PARAMS, entry_timeframe='banana')
        strategy.update_config(bad)
        self.assertEqual(strategy._params['entry_timeframe'], '15m')  # старе значення збережено


# -------------------------------------------------------------- atr_3step трейлінг

class FakeExchange:
    def __init__(self, mark_price, klines=None):
        self.mark_price = mark_price
        self.klines = klines or []
        self.created = []
        self.cancelled = []
        self._order_seq = 100

    async def cancel_order(self, symbol, order_id):
        self.cancelled.append(order_id)
        return {'code': 0}

    async def get_mark_price(self, symbol):
        return self.mark_price

    async def create_order(self, **kwargs):
        self.created.append(kwargs)
        self._order_seq += 1
        return {'code': 0, 'data': {'order': {'orderId': str(self._order_seq)}}}

    async def get_klines(self, symbol, interval, limit=500, **kwargs):
        return self.klines


class FakeDb:
    def update_position_metadata(self, order_id, metadata):
        pass

    def __getattr__(self, name):
        # аналітика (insert/close/append_*_trade_analytics) у цих тестах не
        # перевіряється — достатньо no-op, щоб не засмічувати лог помилками
        if 'analytics' in name:
            return lambda *a, **k: None
        raise AttributeError(name)


class FakeTrader:
    def __init__(self, position):
        self.open_positions = {f"{position['symbol']}_{position['side']}": position}


def _atr_position(side='LONG', entry=100.0, atr_value=2.0, stop=None):
    sign = 1 if side == 'LONG' else -1
    return {
        'order_id': '1', 'symbol': 'BTC-USDT', 'side': side, 'quantity': 1.0,
        'remaining_quantity': 1.0, 'entry_price': entry, 'leverage': 10,
        'stop_loss_price': stop if stop is not None else entry - sign * 1.5 * atr_value,
        'initial_stop_loss_price': entry - sign * 1.5 * atr_value,
        'sl_order_id': '99', 'strategy': 'TrendSupertrendStrategy',
        'trail_meta': {
            'mode': 'atr_3step', 'atr': atr_value, 'interval': '15m',
            'breakeven_atr': 1.0, 'lock_trigger_atr': 2.0, 'lock_offset_atr': 1.0,
            'gap_atr': 0.5, 'lookback': 3,
        },
    }


class TestAtrThreeStepTrailing(unittest.IsolatedAsyncioTestCase):
    def _make(self, position, mark_price, klines=None):
        exchange = FakeExchange(mark_price, klines)
        manager = TrailingStopManager(EventBus(), exchange, FakeDb(), FakeTrader(position),
                                      {'enabled': True, 'taker_fee_rate': 0.0005})
        return manager, exchange

    async def _tick(self, manager, position, price):
        key = f"{position['symbol']}_{position['side']}"
        await manager._process_position(key, position, price)

    async def test_no_move_before_one_atr(self):
        pos = _atr_position()
        manager, exchange = self._make(pos, mark_price=101.0)
        await self._tick(manager, pos, 101.0)  # +0.5 ATR
        self.assertEqual(exchange.created, [])

    async def test_step1_breakeven_with_fees_and_mark_price(self):
        pos = _atr_position()
        manager, exchange = self._make(pos, mark_price=102.1)
        await self._tick(manager, pos, 102.1)  # +1.05 ATR
        self.assertEqual(len(exchange.created), 1)
        order = exchange.created[0]
        self.assertEqual(order['order_type'], 'STOP_MARKET')
        self.assertEqual(order['working_type'], 'MARK_PRICE')
        self.assertTrue(order['close_position'])
        self.assertAlmostEqual(order['stop_price'], 100.0 + 2 * 0.0005 * 100.0)  # entry + комісії
        self.assertEqual(exchange.cancelled, ['99'])

    async def test_step2_lock_profit_at_entry_plus_one_atr(self):
        pos = _atr_position()
        manager, exchange = self._make(pos, mark_price=104.5)
        await self._tick(manager, pos, 104.5)  # +2.25 ATR
        self.assertEqual(len(exchange.created), 1)
        self.assertAlmostEqual(exchange.created[0]['stop_price'], 102.0)  # entry + 1*ATR

    async def test_step3_trails_below_swing_low_with_half_atr_gap(self):
        pos = _atr_position(stop=102.0)  # крок 2 вже застосований
        interval_ms = interval_to_ms('15m')
        bucket_start = (int(time.time() * 1000) // interval_ms) * interval_ms
        klines = [
            {'time': bucket_start - (3 - i) * interval_ms, 'high': 110 + i, 'low': 106.0 + i,
             'open': 108, 'close': 109, 'volume': 1}
            for i in range(3)
        ]  # екстремум Low за 3 свічки = 106
        manager, exchange = self._make(pos, mark_price=112.0, klines=klines)

        await self._tick(manager, pos, 112.0)      # запускає фонове оновлення свінг-стопу
        await _drain(manager)
        await self._tick(manager, pos, 112.0)      # тепер кандидат крок 3 доступний

        self.assertEqual(len(exchange.created), 1)
        self.assertAlmostEqual(exchange.created[0]['stop_price'], 106.0 - 0.5 * 2.0)  # 105.0
        self.assertEqual(exchange.created[0]['working_type'], 'MARK_PRICE')

    async def test_stop_never_moves_backwards(self):
        pos = _atr_position(stop=105.0)
        manager, exchange = self._make(pos, mark_price=104.5)
        await self._tick(manager, pos, 104.5)  # крок 2 дав би 102.0 < поточного 105.0
        self.assertEqual(exchange.created, [])

    async def test_short_side_mirrored(self):
        pos = _atr_position(side='SHORT')  # entry 100, SL 103
        manager, exchange = self._make(pos, mark_price=95.5)
        await self._tick(manager, pos, 95.5)  # +2.25 ATR у шорт
        self.assertEqual(len(exchange.created), 1)
        self.assertAlmostEqual(exchange.created[0]['stop_price'], 98.0)  # entry - 1*ATR
        self.assertEqual(exchange.created[0]['side'], 'BUY')

    async def test_ladder_positions_untouched_without_levels(self):
        pos = _atr_position()
        pos.pop('trail_meta')
        manager, exchange = self._make(pos, mark_price=110.0)
        event = type('E', (), {'data': [{'s': 'BTC-USDT', 'p': '110'}]})()
        await manager._on_price_update(event)
        self.assertEqual(exchange.created, [])


async def _drain(manager):
    import asyncio
    while manager._bg_tasks:
        await asyncio.gather(*list(manager._bg_tasks))


# ------------------------------------------------------------- предохранитель

class FakeSettings:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    async def set(self, key, value):
        self.data[key] = value

    def get_blacklist_symbols(self):
        return []

    async def add_blacklist_symbol(self, symbol):
        pass


class FakeRiskExchange:
    async def get_positions(self):
        return []


class TestStopLossStreakBreaker(unittest.IsolatedAsyncioTestCase):
    def _make(self, settings=None):
        bus = EventBus()
        events = []

        async def collect(event):
            events.append(event)
        bus.subscribe(EventType.ERROR, collect)
        settings = settings or FakeSettings()
        rm = RiskManager(db=None, event_bus=bus, exchange=FakeRiskExchange(),
                         config={'max_consecutive_losses': 0}, settings_manager=settings)
        return rm, settings, bus, events

    async def test_three_stops_in_a_row_block_entries(self):
        rm, _, bus, _ = self._make()
        for _ in range(2):
            await rm._register_closed_trade(-1.0, 'STOP_MARKET')
        self.assertTrue((await rm.can_open_position('BTC-USDT'))[0])

        await rm._register_closed_trade(-1.0, 'STOP_MARKET')
        ok, reason = await rm.can_open_position('BTC-USDT')
        self.assertFalse(ok)
        self.assertIn('blocked', reason)
        self.assertAlmostEqual(rm.get_entry_block_remaining(), 8 * 3600, delta=5)
        self.assertEqual(bus.get_queue_size(), 1)  # уведомление ушло в шину

    async def test_winner_or_non_stop_close_breaks_the_streak(self):
        rm, *_ = self._make()
        await rm._register_closed_trade(-1.0, 'STOP_MARKET')
        await rm._register_closed_trade(-1.0, 'STOP_MARKET')
        await rm._register_closed_trade(+2.0, 'STOP_MARKET')   # стоп в плюс (трейлинг) — серия обрывается
        await rm._register_closed_trade(-1.0, 'STOP_MARKET')
        self.assertTrue((await rm.can_open_position('BTC-USDT'))[0])

    async def test_old_stops_outside_window_do_not_count(self):
        rm, *_ = self._make()
        old = time.time() - 25 * 3600
        rm._sl_streak = [old, old]
        await rm._register_closed_trade(-1.0, 'STOP_MARKET')
        self.assertTrue((await rm.can_open_position('BTC-USDT'))[0])

    async def test_block_expires_and_state_survives_restart(self):
        rm, settings, *_ = self._make()
        for _ in range(3):
            await rm._register_closed_trade(-1.0, 'STOP_MARKET')
        rm2, *_ = self._make(settings)                     # «рестарт»: те ж налаштування в БД
        self.assertFalse((await rm2.can_open_position('BTC-USDT'))[0])
        rm2._entry_blocked_until = time.time() - 1          # блок минув
        self.assertTrue((await rm2.can_open_position('BTC-USDT'))[0])

    async def test_disabled_when_limit_is_zero(self):
        rm, *_ = self._make()
        rm.stop_loss_streak_limit = 0
        for _ in range(5):
            await rm._register_closed_trade(-1.0, 'STOP_MARKET')
        self.assertTrue((await rm.can_open_position('BTC-USDT'))[0])


# ------------------------------------------------- SimpleTrader: risk_percent / trail_meta

class FakeTradeExchange:
    def __init__(self):
        self.orders = []

    async def set_leverage(self, *a, **k):
        return {}

    async def create_order(self, **kwargs):
        self.orders.append(kwargs)
        if kwargs['order_type'] == 'MARKET':
            return {'data': {'order': {'orderId': '1', 'status': 'FILLED',
                                       'avgPrice': '100', 'executedQty': str(kwargs['quantity'])}}}
        return {'data': {'order': {'orderId': '2'}}}


class FakeTradeDb:
    def __init__(self):
        self.metadata = None

    def get_active_positions(self):
        return []

    def insert_position(self, **kwargs):
        self.metadata = kwargs['metadata']

    def update_position_metadata(self, order_id, metadata):
        self.metadata = metadata

    def __getattr__(self, name):
        if 'analytics' in name:
            return lambda *a, **k: None
        raise AttributeError(name)


class FakeSizingRisk:
    def __init__(self, quantity):
        self.quantity = quantity
        self.calls = []

    async def can_open_position(self, symbol, risk_amount=0.0):
        return True, None

    async def compute_risk_based_quantity(self, entry_price, stop_loss_price, risk_percent=None, leverage=None):
        self.calls.append((entry_price, stop_loss_price, risk_percent, leverage))
        return self.quantity


class TestSimpleTraderRiskPercent(unittest.IsolatedAsyncioTestCase):
    async def _open(self, risk_quantity):
        exchange, db, risk = FakeTradeExchange(), FakeTradeDb(), FakeSizingRisk(risk_quantity)
        trader = SimpleTrader(exchange, EventBus(), db, risk_manager=risk)
        meta = {'mode': 'atr_3step', 'atr': 2.0, 'interval': '15m'}
        ok = await trader.open_position(
            symbol='BTC-USDT', side='LONG', quantity=None, leverage=10,
            stop_loss_price=97.0, strategy='TrendSupertrendStrategy',
            reference_price=100.0, risk_percent=1.0, trail_meta=meta,
        )
        return ok, exchange, db, risk, trader

    async def test_risk_percent_sizing_no_tp_and_trail_meta_persisted(self):
        import json
        ok, exchange, db, risk, trader = await self._open(0.5)
        self.assertTrue(ok)
        self.assertEqual(risk.calls, [(100.0, 97.0, 1.0, 10)])  # leverage -> комісія в ризику + стеля маржі
        types = [o['order_type'] for o in exchange.orders]
        self.assertEqual(types, ['MARKET', 'STOP_MARKET'])          # НИКАКОГО TAKE_PROFIT_MARKET
        self.assertEqual(exchange.orders[0]['quantity'], 0.5)
        position = trader.open_positions['BTC-USDT_LONG']
        self.assertEqual(position['trail_meta']['mode'], 'atr_3step')
        self.assertEqual(json.loads(db.metadata)['trail_meta']['mode'], 'atr_3step')  # переживёт рестарт

    async def test_no_position_when_sizing_impossible(self):
        ok, exchange, *_ = await self._open(None)
        self.assertFalse(ok)
        self.assertEqual(exchange.orders, [])  # без безопасного quantity — НЕ открываем "каким-нибудь" размером


# ------------------------------------------- захист від збиткових входів (тестнет 2026-10-08)

class TestLossProtections(unittest.IsolatedAsyncioTestCase):
    def _make(self, **overrides):
        entry, trend, bucket, entry_ms, last_close = _flip_scenario(trend_close=50.0)
        client = FakeKlineClient(entry, trend, overrides.pop('quote_volume', 1_000_000_000.0))
        config = dict(TrendSupertrendStrategy.build_config(None), **overrides)
        strategy = TrendSupertrendStrategy(EventBus(), config, bingx_client=client)
        strategy._last_price['BTC-USDT'] = last_close
        return strategy, client, bucket, entry_ms

    async def test_non_crypto_prefix_is_ignored_without_any_work(self):
        strategy, client, _, _ = self._make()
        self.assertIsNone(await strategy.analyze('NCFXGBP2USD-USDT', 1.32))
        self.assertEqual(strategy._last_bucket, {})
        self.assertEqual(client.calls, [])

    async def test_illiquid_symbol_filtered_before_klines(self):
        strategy, client, bucket, entry_ms = self._make(quote_volume=1_000_000.0)  # < 20M
        self.assertIsNone(await strategy._check_signal('BTC-USDT', bucket, entry_ms))
        self.assertEqual(client.calls, [])  # до запиту свічок не дійшло

    async def test_liquidity_filter_can_be_disabled(self):
        strategy, _, bucket, entry_ms = self._make(quote_volume=1.0, min_quote_volume_24h_usdt=0)
        self.assertIsInstance(await strategy._check_signal('BTC-USDT', bucket, entry_ms), dict)

    async def test_stop_tighter_than_fee_multiple_is_skipped(self):
        strategy, _, bucket, entry_ms = self._make(min_stop_fee_multiple=1000.0)
        self.assertIsNone(await strategy._check_signal('BTC-USDT', bucket, entry_ms))

    async def test_stale_signal_is_dropped_and_fresh_one_published(self):
        strategy, _, bucket, entry_ms = self._make()
        bucket_start_s = bucket * entry_ms / 1000.0

        with unittest.mock.patch.object(time, 'time', return_value=bucket_start_s + 30):
            await strategy._evaluate('BTC-USDT', bucket, bucket - 1, entry_ms)
        self.assertEqual(strategy.event_bus.get_queue_size(), 1)  # свіжий (30с) — опубліковано

        strategy.event_bus._queue = type(strategy.event_bus._queue)()  # очистили чергу
        with unittest.mock.patch.object(time, 'time', return_value=bucket_start_s + 120):
            await strategy._evaluate('BTC-USDT', bucket, bucket - 1, entry_ms)
        self.assertEqual(strategy.event_bus.get_queue_size(), 0)      # застарілий (120с > 90с)

    async def test_retry_after_error_is_cancelled_when_signal_is_stale(self):
        strategy, client, bucket, entry_ms = self._make()

        async def boom(*a, **k):
            raise RuntimeError("REST timeout")
        client.get_klines = boom
        bucket_start_s = bucket * entry_ms / 1000.0

        with unittest.mock.patch.object(time, 'time', return_value=bucket_start_s + 30):
            strategy._last_bucket['BTC-USDT'] = bucket
            await strategy._evaluate('BTC-USDT', bucket, bucket - 1, entry_ms)
        self.assertEqual(strategy._last_bucket['BTC-USDT'], bucket - 1)   # свіжий — повтор дозволено

        with unittest.mock.patch.object(time, 'time', return_value=bucket_start_s + 120):
            strategy._last_bucket['BTC-USDT'] = bucket
            await strategy._evaluate('BTC-USDT', bucket, bucket - 1, entry_ms)
        self.assertEqual(strategy._last_bucket['BTC-USDT'], bucket)       # пізно — повтору немає


class TestRiskSizingProtections(unittest.IsolatedAsyncioTestCase):
    def _make(self, equity=100_000.0):
        rm = RiskManager(db=None, event_bus=EventBus(), exchange=FakeRiskExchange(),
                         config={'max_consecutive_losses': 0}, settings_manager=FakeSettings())

        async def fake_equity():
            return equity
        rm.get_equity = fake_equity
        return rm

    async def test_tight_stop_is_capped_by_margin_limit(self):
        # NCFXGBP2USD-сценарій: стоп 0.1% від ціни, equity 100k, ризик 1%, плече 10
        rm = self._make()
        qty = await rm.compute_risk_based_quantity(100.0, 99.9, risk_percent=1.0, leverage=10)
        margin = qty * 100.0 / 10
        self.assertAlmostEqual(margin, 5_000.0, places=6)      # рівно 5% equity, а не 50%+
        self.assertLess(qty * 0.1, 1_000.0)                    # ризик нижчий за 1%

    async def test_fee_is_included_in_risk_for_wide_stop(self):
        rm = self._make()
        qty = await rm.compute_risk_based_quantity(100.0, 95.0, risk_percent=1.0, leverage=10)
        # 1000 / (5 + 2*0.0005*100) = 1000 / 5.1
        self.assertAlmostEqual(qty, 1000.0 / 5.1, places=6)
        self.assertLess(qty * 100.0 / 10, 5_000.0)             # стеля не спрацьовує

    async def test_legacy_call_without_leverage_is_unchanged(self):
        rm = self._make()
        qty = await rm.compute_risk_based_quantity(100.0, 99.9, risk_percent=1.0)
        self.assertAlmostEqual(qty, 1000.0 / 0.1, places=6)    # без leverage — стара формула, без стелі

    async def test_margin_cap_can_be_disabled(self):
        rm = self._make()
        rm.max_margin_percent_per_trade = 0
        qty = await rm.compute_risk_based_quantity(100.0, 99.9, risk_percent=1.0, leverage=10)
        self.assertAlmostEqual(qty, 1000.0 / (0.1 + 0.1), places=6)


if __name__ == '__main__':
    unittest.main()
