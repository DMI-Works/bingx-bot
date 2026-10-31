"""
Юніт-тести ізоляції окремого (трендового) пулу символів:
  - SymbolSelector(trade_only=True) підписує монети ЛИШЕ на @trade, без @depth20;
  - координація двох пулів через reserved_symbols_provider: @trade спільного
    символу не знімається, поки його тримає інший пул;
  - TrendSupertrendStrategy, прив'язана до пулу через set_symbol_pool_provider,
    ігнорує тики символів поза пулом (жодних REST-запитів за свічками).

Запуск: python -m unittest tests.test_trend_symbol_pool -v
Усі зовнішні залежності — фейки; мережа не використовується. MONGO_URI
потрібен лише для імпорту core.* (див. tests/test_pnl_accounting.py).
"""
import os
import unittest

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")

from core.events import EventBus  # noqa: E402
from core.exchange.symbol_selector import SymbolSelector  # noqa: E402
from core.strategies.trend_supertrend_strategy import TrendSupertrendStrategy  # noqa: E402


class FakeExchange:
    """Фейкова біржа: роздільні набори @trade/@depth, лічильники викликів."""

    def __init__(self, tickers=None, positions=None):
        self.subscribed_symbols = set()
        self.depth_subscribed_symbols = set()
        self._tickers = tickers or []
        self._positions = positions or []
        self.depth_subscribe_calls = 0
        self.depth_unsubscribe_calls = 0

    async def subscribe_trades(self, symbol):
        self.subscribed_symbols.add(symbol)

    async def unsubscribe_trades(self, symbol):
        self.subscribed_symbols.discard(symbol)

    async def subscribe_depth(self, symbol, level=20):
        self.depth_subscribe_calls += 1
        self.depth_subscribed_symbols.add(symbol)

    async def unsubscribe_depth(self, symbol, level=20):
        self.depth_unsubscribe_calls += 1
        self.depth_subscribed_symbols.discard(symbol)

    async def get_positions(self):
        return self._positions

    async def get_all_tickers(self):
        return self._tickers


def _ticker(symbol, volume=100_000_000.0, price=100.0):
    return {
        'symbol': symbol,
        'quoteVolume': volume,
        'lastPrice': price,
        'bidPrice': price - 0.01,
        'askPrice': price + 0.01,
        'highPrice': price * 1.1,
        'lowPrice': price * 0.9,
        'priceChangePercent': 1.0,
    }


_TREND_FILTERS = {
    'rank_by': 'volume',
    'max_symbols': 10,
    'min_volume_24h': 50_000_000,
    'max_spread_percent': 0.2,
}


class TestTradeOnlyPool(unittest.IsolatedAsyncioTestCase):
    async def test_trade_only_never_subscribes_depth(self):
        exchange = FakeExchange(tickers=[_ticker('BTC-USDT'), _ticker('ETH-USDT')])
        selector = SymbolSelector(exchange, _TREND_FILTERS, trade_only=True,
                                  pool_label="TREND-SYMBOLS")

        selected = await selector.apply()

        self.assertEqual(selected, {'BTC-USDT', 'ETH-USDT'})
        self.assertEqual(exchange.subscribed_symbols, {'BTC-USDT', 'ETH-USDT'})
        self.assertEqual(exchange.depth_subscribed_symbols, set())
        self.assertEqual(exchange.depth_subscribe_calls, 0)

    async def test_wall_pool_subscribes_both(self):
        exchange = FakeExchange(tickers=[_ticker('BTC-USDT')])
        selector = SymbolSelector(exchange, _TREND_FILTERS)  # trade_only=False

        await selector.apply()

        self.assertEqual(exchange.subscribed_symbols, {'BTC-USDT'})
        self.assertEqual(exchange.depth_subscribed_symbols, {'BTC-USDT'})


class TestPoolCoordination(unittest.IsolatedAsyncioTestCase):
    async def test_trade_sub_kept_while_reserved_by_other_pool(self):
        exchange = FakeExchange()
        # обидва пули раніше тримали AAA і BBB на @trade
        exchange.subscribed_symbols = {'AAA', 'BBB', 'CCC'}
        reserved = {'AAA'}  # AAA тримає інший пул

        selector = SymbolSelector(exchange, _TREND_FILTERS, trade_only=True,
                                  reserved_symbols_provider=lambda: reserved)
        selector.current_symbols = {'AAA', 'BBB'}

        # цей пул більше хоче лише CCC -> AAA і BBB йдуть на відписку
        async def _select():
            return ['CCC']
        selector.select = _select

        await selector.apply()

        # BBB — не зарезервований -> @trade знято; AAA — зарезервований -> лишився
        self.assertIn('AAA', exchange.subscribed_symbols)
        self.assertNotIn('BBB', exchange.subscribed_symbols)
        self.assertIn('CCC', exchange.subscribed_symbols)
        # трендовий пул @depth не чіпає взагалі
        self.assertEqual(exchange.depth_unsubscribe_calls, 0)

    async def test_no_duplicate_trade_subscribe_when_other_pool_has_it(self):
        exchange = FakeExchange(tickers=[_ticker('BTC-USDT')])
        exchange.subscribed_symbols = {'BTC-USDT'}  # вже підписаний іншим пулом

        calls = []
        orig = exchange.subscribe_trades

        async def _counting(symbol):
            calls.append(symbol)
            await orig(symbol)
        exchange.subscribe_trades = _counting

        selector = SymbolSelector(exchange, _TREND_FILTERS, trade_only=True)
        await selector.apply()

        self.assertIn('BTC-USDT', selector.current_symbols)
        self.assertEqual(calls, [])  # дубль-підписку не слали


class TestStrategyPoolGate(unittest.IsolatedAsyncioTestCase):
    def _strategy(self):
        config = TrendSupertrendStrategy.build_config(None)
        # bingx_client=None: якщо гейт пропустить символ, стратегія спробує піти
        # за свічками і впаде на warning -> нам достатньо перевірити ранній вихід
        return TrendSupertrendStrategy(EventBus(), config, bingx_client=object())

    async def test_symbol_outside_pool_is_ignored(self):
        strategy = self._strategy()
        strategy.set_symbol_pool_provider(lambda: {'ETH-USDT'})

        # BTC-USDT не в пулі -> жодної роботи (бакети не чіпаються)
        self.assertIsNone(await strategy.analyze('BTC-USDT', 100.0))
        self.assertNotIn('BTC-USDT', strategy._last_bucket)

    async def test_symbol_in_pool_primes_bucket(self):
        strategy = self._strategy()
        strategy.set_symbol_pool_provider(lambda: {'BTC-USDT'})

        # у пулі -> перший тік лише запам'ятовує bucket (previous_bucket None)
        self.assertIsNone(await strategy.analyze('BTC-USDT', 100.0))
        self.assertIn('BTC-USDT', strategy._last_bucket)


if __name__ == '__main__':
    unittest.main()
