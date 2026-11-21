"""
Юніт-тести торгового ядра: індикатори, предохранитель серії стоп-лоссів і
risk-сайзинг у RiskManager, SimpleTrader з risk_percent/trail_meta,
аналітика угод і ExcursionTracker.

Запуск: python -m unittest tests.test_trading_core -v
Усі зовнішні залежності (біржа, БД, налаштування) — фейки; мережа не
використовується. MONGO_URI потрібен лише для імпорту core.database
(див. примітку в tests/test_pnl_accounting.py).
"""
import os
import time
import unittest
import unittest.mock

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")

from core.analytics import ExcursionTracker  # noqa: E402
from core.database.analytics_fields import entry_derived_fields, exit_derived_fields  # noqa: E402
from core.events import Event, EventBus, EventType  # noqa: E402
from core.risk import RiskManager, TrailingStopManager  # noqa: E402
from core.strategies.indicators import (  # noqa: E402
    atr, ema, interval_to_ms, sma,
)
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


# -------------------------------------------------------------- фейки


class FakeTrader:
    def __init__(self, position):
        self.open_positions = {f"{position['symbol']}_{position['side']}": position}


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
        self.analytics = None

    def insert_trade_analytics(self, **kwargs):
        self.analytics = kwargs

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
        meta = {'mode': 'roi_gap', 'breakeven_roi': 10.0, 'start_roi': 20.0, 'gap_roi': 10.0}
        ok = await trader.open_position(
            symbol='BTC-USDT', side='LONG', quantity=None, leverage=10,
            stop_loss_price=97.0, strategy='TsMomentumStrategy',
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
        self.assertEqual(position['trail_meta']['mode'], 'roi_gap')
        self.assertEqual(json.loads(db.metadata)['trail_meta']['mode'], 'roi_gap')  # переживёт рестарт

    async def test_no_position_when_sizing_impossible(self):
        ok, exchange, *_ = await self._open(None)
        self.assertFalse(ok)
        self.assertEqual(exchange.orders, [])  # без безопасного quantity — НЕ открываем "каким-нибудь" размером


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


# ----------------------------------------------- розширена аналітика: поля, MFE/MAE, контекст

class TestAnalyticsDerivedFields(unittest.TestCase):
    def test_entry_fields_for_oversized_tight_stop_trade(self):
        from datetime import datetime
        out = entry_derived_fields(
            side='SHORT', entry_price=1.318020, quantity=681_275.84, leverage=10, margin_usdt=89_793.5,
            stop_loss_price_initial=1.319423, reference_price=1.318710, equity_at_entry=95_600.0,
            opened_at=datetime(2026, 10, 8, 9, 15, 2),  # четвер
        )
        self.assertAlmostEqual(out['notional_usdt'], 897_935.0, delta=5)
        self.assertAlmostEqual(out['stop_distance_pct'], 0.1064, places=3)
        self.assertAlmostEqual(out['stop_distance_roi_pct'], 1.064, places=2)
        self.assertAlmostEqual(out['planned_risk_usdt'], 955.8, delta=1)
        self.assertAlmostEqual(out['margin_pct_of_equity'], 93.9, delta=0.1)
        self.assertAlmostEqual(out['planned_risk_pct_of_equity'], 1.0, places=2)
        self.assertAlmostEqual(out['entry_slippage_pct'], 0.0523, places=3)  # шорт продали НИЖЧЕ reference => гірше (>0)
        self.assertEqual(out['opened_hour_utc'], 9)
        self.assertEqual(out['opened_weekday'], 3)

    def test_entry_slippage_sign_means_worse_when_positive(self):
        from datetime import datetime
        now = datetime(2026, 1, 1)
        common = dict(quantity=1, leverage=10, margin_usdt=None, stop_loss_price_initial=None,
                      equity_at_entry=None, opened_at=now)
        long_worse = entry_derived_fields(side='LONG', entry_price=101, reference_price=100, **common)
        short_worse = entry_derived_fields(side='SHORT', entry_price=99, reference_price=100, **common)
        long_better = entry_derived_fields(side='LONG', entry_price=99, reference_price=100, **common)
        self.assertGreater(long_worse['entry_slippage_pct'], 0)
        self.assertGreater(short_worse['entry_slippage_pct'], 0)
        self.assertLess(long_better['entry_slippage_pct'], 0)

    def test_exit_fields_stop_slippage_and_r_multiple(self):
        row = {'side': 'LONG', 'entry_price': 100.0, 'stop_loss_price_initial': 98.0,
               'planned_risk_usdt': 1000.0, 'notional_usdt': 50_000.0, 'sl_moves': []}
        out = exit_derived_fields(row, close_price=96.0, net_pnl=-3000.0,
                                   commission_open=-25.0, commission_close=-25.0, close_reason='stop_loss')
        self.assertAlmostEqual(out['stop_slippage_pct'], 2.0)      # закрилось на 2% нижче стопу
        self.assertAlmostEqual(out['r_multiple'], -3.0)
        self.assertAlmostEqual(out['price_move_pct'], -4.0)
        self.assertAlmostEqual(out['commission_pct_of_notional'], 0.1)
        self.assertEqual(out['sl_moves_count'], 0)
        self.assertEqual(out['final_stop_price'], 98.0)

    def test_exit_fields_use_last_moved_stop_and_short_side(self):
        row = {'side': 'SHORT', 'entry_price': 100.0, 'stop_loss_price_initial': 103.0,
               'planned_risk_usdt': 300.0, 'sl_moves': [{'new_stop_price': 99.9}, {'new_stop_price': 99.0}]}
        out = exit_derived_fields(row, close_price=99.5, net_pnl=-10.0,
                                   commission_open=None, commission_close=None, close_reason='stop_loss')
        self.assertEqual(out['final_stop_price'], 99.0)
        self.assertEqual(out['sl_moves_count'], 2)
        self.assertAlmostEqual(out['stop_slippage_pct'], 0.5)       # шорт закрито на 0.5% вище стопу
        self.assertNotIn('commission_pct_of_notional', out)

    def test_exit_fields_empty_row_is_safe(self):
        self.assertEqual(exit_derived_fields(None, 1.0, 1.0, 0.0, 0.0, 'manual'), {})


class FakeExcursionDb:
    def __init__(self, stored=None):
        self.saved = []
        self.stored = stored

    def update_trade_excursion(self, order_id, excursion):
        self.saved.append((order_id, dict(excursion)))

    def get_trade_analytics(self, order_id):
        return self.stored


def _tick(symbol, price):
    return Event(type=EventType.PRICE_UPDATED, data=[{'s': symbol, 'p': str(price)}])


class TestExcursionTracker(unittest.IsolatedAsyncioTestCase):
    def _make(self, side='LONG', stored=None):
        position = {'order_id': '42', 'symbol': 'BTC-USDT', 'side': side, 'entry_price': 100.0,
                    'leverage': 10, 'trail_meta': {'atr': 2.0}}
        trader = FakeTrader(position)
        db = FakeExcursionDb(stored)
        tracker = ExcursionTracker(EventBus(), db, trader, {'excursion_flush_seconds': 0})
        return tracker, db, trader

    async def _settle(self, tracker):
        import asyncio
        while tracker._tasks:
            await asyncio.gather(*list(tracker._tasks))

    async def test_long_mfe_mae_and_final_record(self):
        tracker, db, trader = self._make('LONG')
        for price in (101.0, 104.0, 99.0, 97.0, 100.5):
            await tracker._on_price_update(_tick('BTC-USDT', price))
        await self._settle(tracker)

        saved_order, snap = db.saved[-1]
        self.assertEqual(saved_order, '42')
        self.assertAlmostEqual(snap['mfe_pct'], 4.0)
        self.assertAlmostEqual(snap['mae_pct'], -3.0)
        self.assertAlmostEqual(snap['mfe_roi_pct'], 40.0)       # плече 10
        self.assertAlmostEqual(snap['mfe_atr'], 2.0)            # +4 / ATR 2
        self.assertAlmostEqual(snap['mae_atr'], -1.5)

        # закриття по стопу ПОГАНІШЕ за мінімум тіка (проскальзування) -> входить у MAE
        trader.open_positions.clear()
        await tracker._on_position_closed(Event(
            type=EventType.POSITION_CLOSED, data={'order_id': '42', 'close_price': 95.0}))
        _, final = db.saved[-1]
        self.assertTrue(final['final'])
        self.assertAlmostEqual(final['mae_pct'], -5.0)
        self.assertAlmostEqual(final['exit_pct'], -5.0)
        self.assertAlmostEqual(final['gave_back_pct'], 4.0 - (-5.0))
        self.assertAlmostEqual(final['captured_pct_of_mfe'], -5.0 / 4.0 * 100.0)  # вийшли в мінус при MFE +4%
        self.assertEqual(tracker._state, {})                     # стан прибрано

    async def test_short_side_is_mirrored(self):
        tracker, db, _ = self._make('SHORT')
        for price in (99.0, 96.0, 103.0):
            await tracker._on_price_update(_tick('BTC-USDT', price))
        await self._settle(tracker)
        _, snap = db.saved[-1]
        self.assertAlmostEqual(snap['mfe_pct'], 4.0)     # шорт: найнижча ціна = макс. профіт
        self.assertAlmostEqual(snap['mae_pct'], -3.0)    # найвища ціна = макс. збиток
        self.assertAlmostEqual(snap['mfe_price'], 96.0)

    async def test_restores_saved_extremes_after_restart(self):
        from datetime import datetime, timezone
        two_hours_ago = datetime.fromtimestamp(time.time() - 7200, timezone.utc).replace(tzinfo=None)
        stored = {'opened_at': two_hours_ago,
                  'excursion': {'mfe_price': 108.0, 'mfe_after_seconds': 600.0,
                                'mae_price': 98.0, 'mae_after_seconds': 60.0}}
        tracker, db, _ = self._make('LONG', stored=stored)
        await tracker._on_price_update(_tick('BTC-USDT', 101.0))   # перший тик після "рестарту"
        await self._settle(tracker)
        await tracker._on_price_update(_tick('BTC-USDT', 101.0))
        await self._settle(tracker)
        _, snap = db.saved[-1]
        self.assertAlmostEqual(snap['mfe_pct'], 8.0)     # піки зі збереженого, а не лише від поточного тіка
        self.assertAlmostEqual(snap['mae_pct'], -2.0)

    async def test_other_symbols_and_garbage_ticks_are_ignored(self):
        tracker, db, _ = self._make('LONG')
        await tracker._on_price_update(_tick('ETH-USDT', 5.0))
        await tracker._on_price_update(Event(type=EventType.PRICE_UPDATED, data=[]))
        await tracker._on_price_update(Event(type=EventType.PRICE_UPDATED, data=[{'s': 'BTC-USDT', 'p': 'x'}]))
        await self._settle(tracker)
        self.assertEqual(db.saved, [])
        self.assertEqual(tracker._state, {})


class TestSignalContextAndTraderPassThrough(unittest.IsolatedAsyncioTestCase):

    async def test_trader_passes_context_equity_and_concurrency_to_db(self):
        exchange, db, risk = FakeTradeExchange(), FakeTradeDb(), FakeSizingRisk(0.5)
        risk.last_equity, risk.last_equity_at = 95_000.0, time.time()
        trader = SimpleTrader(exchange, EventBus(), db, risk_manager=risk)
        ok = await trader.open_position(
            symbol='BTC-USDT', side='LONG', quantity=None, leverage=10, stop_loss_price=97.0,
            strategy='TsMomentumStrategy', reference_price=100.0, risk_percent=1.0,
            signal_context={'atr_pct': 0.9},
        )
        self.assertTrue(ok)
        self.assertEqual(db.analytics['signal_context'], {'atr_pct': 0.9})
        self.assertEqual(db.analytics['equity_at_entry'], 95_000.0)
        self.assertEqual(db.analytics['concurrent_positions'], 0)   # інших відкритих не було

    async def test_stale_equity_is_not_recorded(self):
        exchange, db, risk = FakeTradeExchange(), FakeTradeDb(), FakeSizingRisk(0.5)
        risk.last_equity, risk.last_equity_at = 95_000.0, time.time() - 3600
        trader = SimpleTrader(exchange, EventBus(), db, risk_manager=risk)
        await trader.open_position(symbol='BTC-USDT', side='LONG', quantity=None, leverage=10,
                                   stop_loss_price=97.0, reference_price=100.0, risk_percent=1.0)
        self.assertIsNone(db.analytics['equity_at_entry'])         # годинний equity — не віримо

    async def test_risk_manager_remembers_last_equity(self):
        class BalanceExchange:
            async def get_account_balance(self):
                return {'code': 0, 'data': {'balance': {'equity': '12345.5'}}}
        rm = RiskManager(db=None, event_bus=EventBus(), exchange=BalanceExchange(),
                         config={'max_consecutive_losses': 0}, settings_manager=FakeSettings())
        self.assertIsNone(rm.last_equity)
        self.assertEqual(await rm.get_equity(), 12345.5)
        self.assertEqual(rm.last_equity, 12345.5)
        self.assertAlmostEqual(rm.last_equity_at, time.time(), delta=5)


if __name__ == '__main__':
    unittest.main()
