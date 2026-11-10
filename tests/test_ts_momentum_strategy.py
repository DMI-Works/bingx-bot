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

    async def test_first_tick_after_start_reconciles_immediately(self):
        # регресія: перший тік після старту лише запам'ятовував свічку, і
        # після кожного рестарту стратегія чекала наступної доби
        import asyncio
        closes = [100 + i for i in range(40)]
        s = _strategy(closes, position_for=lambda sym, side: None)
        self.assertIsNone(await s.analyze('BTC-USDT', 139.0))
        await asyncio.gather(*list(s._tasks))
        self.assertEqual(s.bingx_client.klines_calls, 1)
        # EventBus.publish лише кладе подію в чергу (шина в тесті не запущена)
        event = s.event_bus._queue.get_nowait()
        self.assertEqual(event.data['side'], 'LONG')

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


class TestMinMargin(unittest.TestCase):
    def _trader(self, min_margin):
        import core.trading.simple_trader as st
        trader = st.SimpleTrader.__new__(st.SimpleTrader)
        trader.settings_manager = mock.MagicMock()
        trader.settings_manager.get_min_margin_usdt.return_value = min_margin
        return trader

    def test_small_margin_raised_to_minimum(self):
        # 0.01 * 100 / 10 = 0.1 USDT маржі -> до 10 USDT: 10 * 10 / 100 = 1.0
        qty = self._trader(10.0)._apply_min_margin('X-USDT', 'LONG', 0.01, 10, 100.0)
        self.assertAlmostEqual(qty, 1.0)

    def test_bigger_margin_untouched(self):
        qty = self._trader(10.0)._apply_min_margin('X-USDT', 'LONG', 5.0, 10, 100.0)
        self.assertEqual(qty, 5.0)

    def test_zero_disables(self):
        qty = self._trader(0.0)._apply_min_margin('X-USDT', 'LONG', 0.01, 10, 100.0)
        self.assertEqual(qty, 0.01)


class TestRoiGapTrailing(unittest.IsolatedAsyncioTestCase):
    META = {'mode': 'roi_gap', 'breakeven_roi': 10.0, 'start_roi': 30.0, 'gap_roi': 20.0}

    def test_target_levels(self):
        from core.risk.trailing_roi_gap import roi_gap_target
        self.assertIsNone(roi_gap_target(self.META, 8.0, 1.0))        # рано
        self.assertEqual(roi_gap_target(self.META, 12.0, 1.0), 1.0)    # безубиток
        self.assertEqual(roi_gap_target(self.META, 50.0, 1.0), 30.0)   # пік 50 - 20

    async def test_signal_uses_roi_gap(self):
        closes = [100 + i for i in range(40)]
        s = _strategy(closes, position_for=lambda sym, side: None)
        sig = await s._reconcile('BTC-USDT', _bucket_now(), DAY)
        self.assertEqual(sig['trail_meta'], self.META)

    async def test_hold_upgrades_old_position_meta(self):
        # позиція, відкрита з trail_meta={'mode':'none'}, отримує roi_gap на звірці
        pos = {'strategy': 'TsMomentumStrategy', 'trail_meta': {'mode': 'none'}}
        closes = [100 + i for i in range(40)]
        s = _strategy(closes, position_for=lambda sym, side: pos if side == 'LONG' else None)
        await s._reconcile('BTC-USDT', _bucket_now(), DAY)
        self.assertEqual(pos['trail_meta'], self.META)

    async def test_manager_moves_stop_to_peak_minus_gap(self):
        from core.risk.trailing_stop_manager import TrailingStopManager
        from core.risk.trailing_state import TrailState
        tsm = TrailingStopManager.__new__(TrailingStopManager)
        tsm.taker_fee_rate = 0.0005
        tsm._retry_after = {}
        tsm.move_retry_cooldown_seconds = 10
        position = {'symbol': 'BTC-USDT', 'side': 'LONG', 'entry_price': 100.0, 'leverage': 10,
                    'stop_loss_price': 88.0, 'sl_order_id': 'sl1', 'trail_meta': dict(self.META)}
        tsm.trader = mock.MagicMock()
        tsm.trader.open_positions = {'k': position}
        tsm._move_stop_loss = mock.AsyncMock(return_value=1)
        # ціна 105 при 10x = +50% ROI -> стоп на +30% ROI = 103
        await tsm._process_roi_gap_position('k', position, TrailState(), 105.0, 10.0)
        candidates = tsm._move_stop_loss.call_args.args[3]
        self.assertAlmostEqual(candidates[0][2], 103.0)

    async def test_no_loop_when_stop_already_at_breakeven(self):
        # регресія 06.11: стоп на безубитку (0.99999...% ROI при цілі 1.0%)
        # переставлявся на кожен тік — 10 000 переносів і повідомлень
        from core.risk.trailing_stop_manager import TrailingStopManager
        from core.risk.trailing_state import TrailState
        tsm = TrailingStopManager.__new__(TrailingStopManager)
        tsm.taker_fee_rate = 0.0005
        tsm._retry_after = {}
        tsm.move_retry_cooldown_seconds = 10
        position = {'symbol': 'AVAX-USDT', 'side': 'LONG', 'entry_price': 8.171, 'leverage': 10,
                    'stop_loss_price': 8.187341999999999, 'sl_order_id': 'sl1', 'trail_meta': dict(self.META)}
        tsm.trader = mock.MagicMock()
        tsm.trader.open_positions = {'k': position}
        tsm._move_stop_loss = mock.AsyncMock(return_value=1)
        state = TrailState()
        for price in (8.30, 8.31, 8.32):  # ROI ~16-18%: безубиток досягнуто, трейлінг ще ні
            await tsm._process_roi_gap_position('k', position, state, price, 10.0)
        tsm._move_stop_loss.assert_not_awaited()

    async def test_breakeven_stop_has_slippage_reserve(self):
        from core.risk.trailing_stop_manager import TrailingStopManager
        from core.risk.trailing_state import TrailState
        for old_stop in (88.0, 100.1):  # початковий стоп і старий безубиток +1% ROI
            tsm = TrailingStopManager.__new__(TrailingStopManager)
            tsm.taker_fee_rate = 0.0005
            tsm._retry_after = {}
            tsm.move_retry_cooldown_seconds = 10
            position = {'symbol': 'BTC-USDT', 'side': 'LONG', 'entry_price': 100.0, 'leverage': 10,
                        'stop_loss_price': old_stop, 'sl_order_id': 'sl1', 'trail_meta': dict(self.META)}
            tsm.trader = mock.MagicMock()
            tsm.trader.open_positions = {'k': position}
            tsm._move_stop_loss = mock.AsyncMock(return_value=1)
            # ціна 101.5 при 10x = +15% ROI: безубиток досягнуто, трейлінг ще ні
            await tsm._process_roi_gap_position('k', position, TrailState(), 101.5, 10.0)
            _, target_roi, target_price = tsm._move_stop_loss.call_args.args[3][0]
            self.assertAlmostEqual(target_roi, 2.0)
            self.assertAlmostEqual(target_price, 100.2)

    async def test_cooldown_between_moves(self):
        from core.risk.trailing_stop_manager import TrailingStopManager
        from core.risk.trailing_state import TrailState
        tsm = TrailingStopManager.__new__(TrailingStopManager)
        tsm.taker_fee_rate = 0.0005
        tsm._retry_after = {}
        tsm.move_retry_cooldown_seconds = 10
        position = {'symbol': 'BTC-USDT', 'side': 'LONG', 'entry_price': 100.0, 'leverage': 10,
                    'stop_loss_price': 88.0, 'sl_order_id': 'sl1', 'trail_meta': dict(self.META)}
        tsm.trader = mock.MagicMock()
        tsm.trader.open_positions = {'k': position}
        tsm._move_stop_loss = mock.AsyncMock(return_value=1)  # стоп "не зрушив" у позиції
        state = TrailState()
        await tsm._process_roi_gap_position('k', position, state, 105.0, 10.0)
        await tsm._process_roi_gap_position('k', position, state, 105.0, 10.0)
        self.assertEqual(tsm._move_stop_loss.await_count, 1)

    async def test_trailing_manager_skips_mode_none(self):
        from core.events import Event, EventType
        from core.risk.trailing_stop_manager import TrailingStopManager
        from core.common.position_math import position_key
        tsm = TrailingStopManager.__new__(TrailingStopManager)
        tsm.enabled = True
        tsm.trail_levels_percent = [6.0]
        tsm.trader = mock.MagicMock()
        tsm.trader.open_positions = {
            position_key('BTC-USDT', 'SHORT'): {'symbol': 'BTC-USDT', 'side': 'SHORT', 'trail_meta': {'mode': 'none'}},
            position_key('BTC-USDT', 'LONG'): {'symbol': 'BTC-USDT', 'side': 'LONG', 'trail_meta': None},
        }
        tsm._process_position = mock.AsyncMock()
        await tsm._on_price_update(Event(type=EventType.PRICE_UPDATED, data={'s': 'BTC-USDT', 'p': '100'}))
        # обробляється лише LONG (звичайна лестница), SHORT з mode=none — ні
        self.assertEqual(tsm._process_position.await_count, 1)
        self.assertEqual(tsm._process_position.call_args.args[0], position_key('BTC-USDT', 'LONG'))


class TestPendingOpenNotManual(unittest.IsolatedAsyncioTestCase):
    # регресія 05.11: ACCOUNT_UPDATE про власний вхід бота приходив раніше,
    # ніж позиція реєструвалась, і створював «ручний» дубль, що висів у відкритих
    async def test_account_update_for_pending_entry_is_ignored(self):
        import core.trading.simple_trader as st
        from core.events import Event, EventType
        from core.common.position_math import position_key
        trader = st.SimpleTrader.__new__(st.SimpleTrader)
        trader.open_positions = {}
        trader._pending_opens = {position_key('XRP-USDT', 'SHORT')}
        trader._track_manual_position = mock.AsyncMock()
        trader._MANUAL_DETECT_DELAY_SECONDS = 0
        trader.db = mock.MagicMock()
        trader.db.has_open_position.return_value = False
        event = Event(type=EventType.HANDLE_ACCOUNT_INFO_UPDATE, data={'a': {'P': [
            {'s': 'XRP-USDT', 'ps': 'SHORT', 'pa': '-22', 'ep': '1.3189'},
            {'s': 'ETH-USDT', 'ps': 'LONG', 'pa': '0.01', 'ep': '2400'},
        ]}})
        await trader._handle_account_update(event)
        import asyncio
        await asyncio.gather(*list(trader._background_tasks()))
        # XRP (pending вхід бота) пропущено, ETH (справді ручна) — записано
        trader._track_manual_position.assert_awaited_once_with('ETH-USDT', 'LONG', 2400.0, 0.01)

    async def test_position_of_other_instance_is_not_manual(self):
        # регресія 05.11: під час редеплою старий інстанс бачив вхід нового
        # як «ручний»; якщо позиція вже є в БД — це не ручна
        import asyncio
        import core.trading.simple_trader as st
        from core.events import Event, EventType
        trader = st.SimpleTrader.__new__(st.SimpleTrader)
        trader.open_positions = {}
        trader._pending_opens = set()
        trader._track_manual_position = mock.AsyncMock()
        trader._MANUAL_DETECT_DELAY_SECONDS = 0
        trader.db = mock.MagicMock()
        trader.db.has_open_position.return_value = True
        await trader._handle_account_update(Event(type=EventType.HANDLE_ACCOUNT_INFO_UPDATE, data={'a': {'P': [
            {'s': 'ETH-USDT', 'ps': 'SHORT', 'pa': '-0.04', 'ep': '2253'},
        ]}}))
        await asyncio.gather(*list(trader._background_tasks()))
        trader._track_manual_position.assert_not_awaited()


class TestStopClampedToMaxRoi(unittest.TestCase):
    # регресія 05.11: TsMomentum 10x з аварійним стопом 16% (= 160% ROI)
    # ліквідувало б раніше стопу; плече лишається, стоп підтягується
    def _trader(self, max_roi=70.0):
        import core.trading.simple_trader as st
        trader = st.SimpleTrader.__new__(st.SimpleTrader)
        trader.settings_manager = mock.MagicMock()
        trader.settings_manager.get_max_stop_roi_percent.return_value = max_roi
        return trader

    def test_short_stop_tightened(self):
        # 70% ROI / 10x = 7% від ціни -> SHORT стоп 107
        stop = self._trader()._clamp_stop_to_max_roi('ETH-USDT', 'SHORT', 10, 116.0, 100.0)
        self.assertAlmostEqual(stop, 107.0)

    def test_long_stop_tightened(self):
        stop = self._trader()._clamp_stop_to_max_roi('BTC-USDT', 'LONG', 10, 88.0, 100.0)
        self.assertAlmostEqual(stop, 93.0)

    def test_close_stop_untouched(self):
        stop = self._trader()._clamp_stop_to_max_roi('ETH-USDT', 'LONG', 10, 99.0, 100.0)
        self.assertEqual(stop, 99.0)


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
