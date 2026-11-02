"""
Юніт-тести TsMomentumStrategy (дневной time-series momentum) і програмного
закриття позиції SimpleTrader.close_position.

Мережа не використовується — усі залежності фейкові. MONGO_URI/TELEGRAM_BOT_TOKEN
потрібні лише для імпорту core.* (див. tests/test_pnl_accounting.py).

Запуск: python -m unittest tests.test_ts_momentum_strategy -v
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

from core.events import EventBus  # noqa: E402
from core.strategies.indicators import pct_return, realized_vol  # noqa: E402
from core.strategies.ts_momentum_strategy import TsMomentumStrategy  # noqa: E402


def _klines(closes, interval_ms, last_open_ms):
    """Будує список свічок (як get_klines): time = open-час бара, close задано,
    high/low навколо close, щоб ATR був > 0."""
    out = []
    t = last_open_ms - interval_ms * (len(closes) - 1)
    for c in closes:
        out.append({'time': t, 'open': c, 'high': c * 1.01, 'low': c * 0.99,
                    'close': c, 'volume': 1000.0})
        t += interval_ms
    return out


class FakeExchange:
    def __init__(self, closes, tickers=None):
        self._closes = closes
        self._tickers = tickers or [{'symbol': 'BTC-USDT', 'quoteVolume': 1e9}]
        self.klines_calls = 0

    async def get_klines(self, symbol, interval, limit=200):
        self.klines_calls += 1
        from core.strategies.indicators import interval_to_ms
        ms = interval_to_ms(interval)
        # остання ЗАКРИТА свічка має бути рівно попередньою до поточного бакета
        import time
        bucket = int(time.time() * 1000) // ms
        last_open = bucket * ms - ms
        return _klines(self._closes, ms, last_open)

    async def get_all_tickers(self):
        return self._tickers


DAY = 86_400_000


class TestIndicators(unittest.TestCase):
    def test_pct_return(self):
        closes = [100, 110, 90, 120]  # last/ (last-3) = 120/100-1 = 0.2
        self.assertAlmostEqual(pct_return(closes, 3), 0.2)
        self.assertIsNone(pct_return(closes, 10))  # мало історії

    def test_realized_vol(self):
        rets = [0.0, 0.01, -0.01, 0.02, -0.02]
        self.assertIsNotNone(realized_vol(rets, 4))
        self.assertIsNone(realized_vol([None, None], 4))


def _strategy(closes, pool=None, position_for=None, closer=None):
    cfg = TsMomentumStrategy.build_config(None)
    cfg['lookback_days'] = 5
    cfg['atr_period'] = 3
    cfg['min_quote_volume_24h_usdt'] = 0  # вимкнути фільтр ліквідності в тестах
    s = TsMomentumStrategy(EventBus(), cfg, bingx_client=FakeExchange(closes))
    s.set_symbol_pool_provider(lambda: (pool if pool is not None else {'BTC-USDT'}))
    if position_for is not None:
        s.set_position_provider(position_for)
    if closer is not None:
        s.set_close_callback(closer)
    return s


class TestSignal(unittest.IsolatedAsyncioTestCase):
    async def test_pool_gate_ignores_outside(self):
        s = _strategy([100] * 20, pool={'ETH-USDT'})
        self.assertIsNone(await s.analyze('BTC-USDT', 100.0))
        # поза пулом — навіть bucket не чіпаємо, REST не дёргаем
        self.assertNotIn('BTC-USDT', s._last_bucket)
        self.assertEqual(s.bingx_client.klines_calls, 0)

    async def test_open_long_on_positive_momentum(self):
        # зростаючий ряд -> mom>0 -> LONG; позиції немає -> OPEN
        closes = [100 + i for i in range(40)]
        s = _strategy(closes, position_for=lambda sym, side: None)
        sig = await s._reconcile('BTC-USDT', _bucket_now(), DAY)
        self.assertIsInstance(sig, dict)
        self.assertEqual(sig['side'], 'LONG')
        self.assertIsNone(sig.get('take_profit_levels'))
        self.assertLess(sig['stop_loss_price'], sig['reference_price'])  # SL нижче для LONG
        self.assertEqual(sig['leverage'], 3)
        self.assertEqual(sig['risk_percent'], 1.0)
        self.assertIsNone(sig['quantity'])

    async def test_open_short_on_negative_momentum(self):
        closes = [140 - i for i in range(40)]  # падіння -> mom<0 -> SHORT
        s = _strategy(closes, position_for=lambda sym, side: None)
        sig = await s._reconcile('BTC-USDT', _bucket_now(), DAY)
        self.assertEqual(sig['side'], 'SHORT')
        self.assertGreater(sig['stop_loss_price'], sig['reference_price'])  # SL вище для SHORT

    async def test_long_flat_mode_no_short(self):
        closes = [140 - i for i in range(40)]
        s = _strategy(closes, position_for=lambda sym, side: None)
        s._params['mode'] = 'long_flat'
        sig = await s._reconcile('BTC-USDT', _bucket_now(), DAY)
        self.assertIsNone(sig)  # шортів немає — залишаємось поза ринком

    async def test_hold_when_same_side(self):
        closes = [100 + i for i in range(40)]  # LONG desired
        pos = {'BTC-USDT_LONG': {'strategy': 'TsMomentumStrategy', 'side': 'LONG'}}
        s = _strategy(closes, position_for=lambda sym, side: pos.get(f"{sym}_{side}"))
        closed = []
        s.set_close_callback(lambda *a: closed.append(a))
        sig = await s._reconcile('BTC-USDT', _bucket_now(), DAY)
        self.assertIsNone(sig)       # уже в LONG — нічого не відкриваємо
        self.assertEqual(closed, [])  # і не закриваємо

    async def test_close_on_flip(self):
        # desired LONG (зростання), але відкрита SHORT -> закрити SHORT
        closes = [100 + i for i in range(40)]
        pos = {'BTC-USDT_SHORT': {'strategy': 'TsMomentumStrategy', 'side': 'SHORT'}}
        calls = []

        async def closer(symbol, side, reason):
            calls.append((symbol, side, reason))

        s = _strategy(closes, position_for=lambda sym, side: pos.get(f"{sym}_{side}"), closer=closer)
        sig = await s._reconcile('BTC-USDT', _bucket_now(), DAY)
        self.assertIsNone(sig)  # на флипі лише закриваємо, відкриття — наступного дня
        self.assertEqual(calls, [('BTC-USDT', 'SHORT', 'tsmom_flip')])


def _bucket_now():
    import time
    return int(time.time() * 1000) // DAY


class TestClosePosition(unittest.IsolatedAsyncioTestCase):
    async def _trader(self):
        # мінімальний SimpleTrader без реальних залежностей
        import core.trading.simple_trader as st
        trader = st.SimpleTrader.__new__(st.SimpleTrader)
        trader.open_positions = {}
        trader.exchange = mock.MagicMock()
        trader.exchange.cancel_order = mock.AsyncMock()
        trader.exchange.create_order = mock.AsyncMock()
        return trader

    async def test_close_sends_reduce_only_and_cancels(self):
        trader = await self._trader()
        from core.common.position_math import position_key
        trader.open_positions[position_key('BTC-USDT', 'LONG')] = {
            'symbol': 'BTC-USDT', 'side': 'LONG', 'quantity': 0.5,
            'sl_order_id': 'sl1', 'tp_order_ids': ['tp1', 'tp2'], 'strategy': 'TsMomentumStrategy',
        }
        ok = await trader.close_position('BTC-USDT', 'LONG', 'tsmom_flip')
        self.assertTrue(ok)
        # скасовано SL + 2 TP
        self.assertEqual(trader.exchange.cancel_order.await_count, 3)
        # reduce-only MARKET на закриття з протилежною стороною
        _, kwargs = trader.exchange.create_order.call_args
        self.assertEqual(kwargs['order_type'], 'MARKET')
        self.assertEqual(kwargs['side'], 'SELL')          # закриття LONG
        self.assertEqual(kwargs['position_side'], 'LONG')  # hedge-mode
        self.assertTrue(kwargs['reduce_only'])

    async def test_close_noop_when_no_position(self):
        trader = await self._trader()
        ok = await trader.close_position('BTC-USDT', 'LONG')
        self.assertFalse(ok)
        trader.exchange.create_order.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
