"""Відбір монет, пауза WallBreakout після закриття, траєкторія угоди і
симуляція виходів у scripts/trade_report.py. Без БД і біржі."""

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.analytics.excursion_tracker import ExcursionTracker  # noqa: E402
from core.events import Event, EventType  # noqa: E402
from core.exchange.symbol_selector import SymbolSelector  # noqa: E402
from core.strategies.wall_breakout_strategy import WallBreakoutStrategy  # noqa: E402
from scripts.trade_report import simulate_mfe_tp, simulate_path  # noqa: E402


def ticker(symbol, volume, high, low, price=1.0):
    return {
        'symbol': symbol, 'quoteVolume': volume, 'lastPrice': price,
        'bidPrice': price, 'askPrice': price * 1.0001,
        'highPrice': high, 'lowPrice': low, 'priceChangePercent': 0,
    }


class FakeExchange:
    def __init__(self, tickers):
        self.tickers = tickers
        self.subscribed_symbols = set()

    async def get_all_tickers(self):
        return self.tickers

    async def get_positions(self):
        return []

    async def subscribe_trades(self, symbol):
        self.subscribed_symbols.add(symbol)

    async def subscribe_depth(self, symbol):
        pass

    async def unsubscribe_trades(self, symbol):
        self.subscribed_symbols.discard(symbol)

    async def unsubscribe_depth(self, symbol):
        pass


class NoSignals:
    def had_signal_within(self, symbol, seconds):
        return False


class FakeBus:
    def __init__(self):
        self.published = []

    def subscribe(self, event_type, handler):
        pass

    async def publish(self, event):
        self.published.append(event)


class TestSymbolSelector(unittest.TestCase):
    def test_excludes_non_crypto_and_ranks_by_volatility(self):
        exchange = FakeExchange([
            ticker('BTC-USDT', 5e9, 1.02, 1.0),          # найбільший об'єм, вузький діапазон
            ticker('PEPE-USDT', 5e7, 1.30, 1.0),         # найживіша
            ticker('NCSKMRVL2USD-USDT', 9e9, 1.5, 1.0),  # акція — виключається
            ticker('DUST-USDT', 1e5, 2.0, 1.0),          # не проходить об'єм
        ])
        selector = SymbolSelector(exchange, {'min_volume_24h': 1e6, 'max_symbols': 1})
        self.assertEqual(asyncio.run(selector.select()), ['PEPE-USDT'])

        selector.filters['rank_by'] = 'volume'
        self.assertEqual(asyncio.run(selector.select()), ['BTC-USDT'])

    def test_quiet_symbol_goes_to_back_of_queue(self):
        exchange = FakeExchange([
            ticker('A-USDT', 1e8, 1.5, 1.0),
            ticker('B-USDT', 1e8, 1.3, 1.0),
            ticker('C-USDT', 1e8, 1.1, 1.0),
        ])
        selector = SymbolSelector(
            exchange,
            {'min_volume_24h': 1e6, 'max_symbols': 1, 'rotation_interval_seconds': 60},
            signal_tracker=NoSignals(),
        )
        self.assertEqual(asyncio.run(selector.apply()), {'A-USDT'})

        # підписка стара й без сигналів -> A тиха, замінюється на B
        selector._subscribed_at['A-USDT'] = time.time() - 3600
        self.assertEqual(asyncio.run(selector.apply()), {'B-USDT'})

        # B теж тиха: A ще на паузі -> береться C, а не повернення до A
        selector._subscribed_at['B-USDT'] = time.time() - 3600
        self.assertEqual(asyncio.run(selector.apply()), {'C-USDT'})

        # усі на паузі — слот не лишається порожнім
        selector._subscribed_at['C-USDT'] = time.time() - 3600
        self.assertEqual(len(asyncio.run(selector.apply())), 1)

    def test_young_subscription_is_not_quiet(self):
        exchange = FakeExchange([ticker('A-USDT', 1e8, 1.5, 1.0), ticker('B-USDT', 1e8, 1.3, 1.0)])
        selector = SymbolSelector(
            exchange,
            {'min_volume_24h': 1e6, 'max_symbols': 1, 'rotation_interval_seconds': 3600},
            signal_tracker=NoSignals(),
        )
        asyncio.run(selector.apply())
        self.assertEqual(asyncio.run(selector.apply()), {'A-USDT'})

    def test_market_context_from_last_rotation(self):
        selector = SymbolSelector(FakeExchange([ticker('A-USDT', 1e8, 1.2, 1.0)]), {'min_volume_24h': 1e6})
        asyncio.run(selector.select())
        ctx = selector.market_context('A-USDT')
        self.assertAlmostEqual(ctx['range_24h_pct'], 20.0)
        self.assertEqual(ctx['quote_volume_24h'], 1e8)
        self.assertIsNone(selector.market_context('MISSING-USDT'))


class TestWallBreakoutReentry(unittest.TestCase):
    def _breakout(self, symbol='KAIA-USDT', side='ask'):
        return Event(type=EventType.WALL_BREAKOUT, data={
            'symbol': symbol, 'side': side, 'wall_price': 1.0, 'current_price': 1.01,
            'consumed_pct': 50.0, 'context': {'wall_multiple': 7.5},
        })

    def test_cooldown_after_close_and_context(self):
        bus = FakeBus()
        strategy = WallBreakoutStrategy(bus, dict(WallBreakoutStrategy.DEFAULT_PARAMS, cooldown_seconds=0))
        strategy.enabled = True

        asyncio.run(strategy._on_wall_breakout(self._breakout()))
        self.assertEqual(len(bus.published), 1)
        context = bus.published[0].data['analytics_context']
        self.assertEqual(context['wall_multiple'], 7.5)
        self.assertEqual(context['consumed_pct'], 50.0)

        asyncio.run(strategy._on_position_closed(Event(
            type=EventType.POSITION_CLOSED, data={'symbol': 'KAIA-USDT', 'strategy': 'WallBreakoutStrategy'},
        )))
        asyncio.run(strategy._on_wall_breakout(self._breakout(side='bid')))
        self.assertEqual(len(bus.published), 1, "вхід одразу після закриття має бути пропущений")

        # чужа стратегія паузу не ставить
        asyncio.run(strategy._on_position_closed(Event(
            type=EventType.POSITION_CLOSED, data={'symbol': 'SOL-USDT', 'strategy': 'TrendSupertrendStrategy'},
        )))
        asyncio.run(strategy._on_wall_breakout(self._breakout(symbol='SOL-USDT')))
        self.assertEqual(len(bus.published), 2)

        strategy.last_close_time['KAIA-USDT'] -= strategy.reentry_cooldown_seconds + 1
        asyncio.run(strategy._on_wall_breakout(self._breakout(side='bid')))
        self.assertEqual(len(bus.published), 3)
        self.assertIn('seconds_since_prev_close', bus.published[-1].data['analytics_context'])


class TestOrderBookBreakoutContext(unittest.TestCase):
    def test_breakout_event_carries_context(self):
        from core.strategies.orderbook_analyzer import OrderBookAnalyzer

        bus = FakeBus()
        analyzer = OrderBookAnalyzer(bus, {'wall_multiplier': 3.0, 'breakout_consumption_pct': 30.0})
        asks = [[1.000, 1], [1.001, 1], [1.002, 50], [1.003, 1], [1.004, 1]]
        bids = [[0.999, 1], [0.998, 1], [0.997, 1]]

        async def run():
            await analyzer._on_orderbook_update(Event(EventType.ORDERBOOK_UPDATED, {
                'symbol': 'X-USDT', 'bids': bids, 'asks': asks}))
            asks[2] = [1.002, 10]  # стіну з'їли на 80%
            await analyzer._on_orderbook_update(Event(EventType.ORDERBOOK_UPDATED, {
                'symbol': 'X-USDT', 'bids': bids, 'asks': asks}))
            await analyzer._on_price_update(Event(EventType.PRICE_UPDATED, {'s': 'X-USDT', 'p': '1.0025'}))

        asyncio.run(run())
        breakouts = [e for e in bus.published if e.type == EventType.WALL_BREAKOUT]
        self.assertEqual(len(breakouts), 1)
        context = breakouts[0].data['context']
        self.assertEqual(context['wall_side'], 'ask')
        self.assertAlmostEqual(context['wall_notional_usdt'], 50 * 1.002)
        self.assertGreater(context['wall_multiple'], 3.0)
        self.assertIsNone(context['price_change_300s_pct'])  # історії ще немає


class TestExcursionPath(unittest.TestCase):
    def _state(self, side='LONG', max_points=2000):
        return {
            'side': side, 'entry': 100.0, 'opened_ts': 0.0,
            'high': 100.0, 'high_ts': 0.0, 'low': 100.0, 'low_ts': 0.0, 'dirty': False,
            'path': [(0.0, 0.0)], 'path_step': 0.05, 'path_max_points': max_points,
        }

    def test_records_only_meaningful_moves(self):
        state = self._state('SHORT')
        for t, price in ((1, 100.01), (2, 99.9), (3, 99.92), (4, 100.2)):
            ExcursionTracker._apply_price(state, price, t)
        # SHORT: падіння ціни — рух у плюс
        self.assertEqual(state['path'], [(0.0, 0.0), (2.0, 0.1), (4.0, -0.2)])

    def test_thins_out_when_too_long(self):
        state = self._state(max_points=10)
        for i in range(1, 30):
            ExcursionTracker._apply_price(state, 100.0 + (i % 2) * 0.2, i)
        self.assertLessEqual(len(state['path']), 11)
        self.assertGreater(state['path_step'], 0.05)

    def test_encode_decode_roundtrip(self):
        path = [(0.0, 0.0), (1.5, 0.12), (30.0, -0.5)]
        self.assertEqual(ExcursionTracker.decode_path(ExcursionTracker.encode_path(path)), path)


class TestTradeReportSimulation(unittest.TestCase):
    def test_simulate_path(self):
        path = [(0, 0.0), (1, 0.6), (2, -1.2)]
        self.assertEqual(simulate_path(path, sl_pct=1.0, tp_pct=0.5), (0.5, True))
        self.assertEqual(simulate_path(path, sl_pct=1.0, tp_pct=None), (-1.0, True))
        # безубиток після +0.5%: стоп піднявся до +0.1
        self.assertEqual(simulate_path(path, 1.0, None, be_trigger_pct=0.5, be_offset_pct=0.1), (0.1, True))
        self.assertEqual(simulate_path([(0, 0.0), (1, 0.3)], 1.0, 2.0), (0.3, False))

    def test_mfe_tp_keeps_actual_result_when_not_reached(self):
        rows = [
            {'excursion.mfe_roi_pct': '12', 'net_pnl': '-1.1', 'margin_usdt': '5', 'commission_usdt': '-0.1'},
            {'excursion.mfe_roi_pct': '3', 'net_pnl': '-1.1', 'margin_usdt': '5', 'commission_usdt': '-0.1'},
        ]
        result = simulate_mfe_tp(rows, 10)
        self.assertAlmostEqual(result[0], 10 - 2.0)   # TP досягнуто, мінус комісія 2% ROI
        self.assertAlmostEqual(result[1], -22.0)      # фактичний net ROE


if __name__ == '__main__':
    unittest.main()
