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


def _strategy(closes, pool=None):
    cfg = TsMomentumStrategy.build_config(None)
    cfg['lookback_days'] = 5
    cfg['atr_period'] = 3
    cfg['min_quote_volume_24h_usdt'] = 0  # вимкнути фільтр ліквідності в тестах
    s = TsMomentumStrategy(EventBus(), cfg, bingx_client=FakeExchange(closes))
    s.set_symbol_pool_provider(lambda: (pool if pool is not None else {'BTC-USDT'}))
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
        s = _strategy(closes)
        self.assertIsNone(await s.analyze('BTC-USDT', 139.0))
        await asyncio.gather(*list(s._tasks))
        self.assertEqual(s.bingx_client.klines_calls, 1)
        # EventBus.publish лише кладе подію в чергу (шина в тесті не запущена)
        event = s.event_bus._queue.get_nowait()
        self.assertEqual(event.data['side'], 'LONG')

    async def _tick(self, s):
        import asyncio
        await s.analyze('BTC-USDT', 139.0)
        await asyncio.gather(*list(s._tasks))

    async def test_no_recheck_before_interval(self):
        s = _strategy([100 + i for i in range(40)])
        await self._tick(s)
        await self._tick(s)
        self.assertEqual(s.bingx_client.klines_calls, 1)

    async def test_recheck_enters_after_interval_without_restart(self):
        # регресія 10.11: після стопу монета чекала наступної доби або редеплою
        s = _strategy([100 + i for i in range(40)])
        await self._tick(s)
        s.event_bus._queue.get_nowait()
        s._last_check_at['BTC-USDT'] -= 3601
        await self._tick(s)
        self.assertEqual(s.bingx_client.klines_calls, 2)
        target = s.event_bus._queue.get_nowait().data
        self.assertEqual((target['action'], target['side']), ('TARGET', 'LONG'))
        self.assertEqual(target['reentry_pause_minutes'], 1440)
        self.assertEqual(target['reentry_pause_after_profit_minutes'], 20160)

    async def test_recheck_can_be_disabled(self):
        s = _strategy([100 + i for i in range(40)])
        s._params['recheck_interval_minutes'] = 0.0
        await self._tick(s)
        s._last_check_at['BTC-USDT'] -= 86400
        await self._tick(s)
        self.assertEqual(s.bingx_client.klines_calls, 1)

    async def test_failed_recheck_is_retried(self):
        s = _strategy([100 + i for i in range(40)])
        await self._tick(s)
        s._last_check_at['BTC-USDT'] -= 3601
        s._reconcile = mock.AsyncMock(side_effect=RuntimeError('boom'))
        await self._tick(s)
        self.assertNotIn('BTC-USDT', s._last_check_at)
        self.assertGreater(s._retry_after['BTC-USDT'], 0)

    async def test_open_long_on_positive_momentum(self):
        # зростаючий ряд -> mom>0 -> LONG; позиції немає -> OPEN
        closes = [100 + i for i in range(40)]
        s = _strategy(closes)
        sig = await s._reconcile('BTC-USDT', _bucket_now(), DAY)
        self.assertIsInstance(sig, dict)
        self.assertEqual(sig['side'], 'LONG')
        self.assertIsNone(sig.get('take_profit_levels'))
        self.assertLess(sig['stop_loss_price'], sig['reference_price'])  # SL нижче для LONG
        self.assertEqual(sig['leverage'], 10)
        self.assertEqual(sig['risk_percent'], 3.0)
        self.assertTrue(sig['fit_leverage'])
        self.assertIsNone(sig['quantity'])

    async def test_open_short_on_negative_momentum(self):
        closes = [140 - i for i in range(40)]  # падіння -> mom<0 -> SHORT
        s = _strategy(closes)
        sig = await s._reconcile('BTC-USDT', _bucket_now(), DAY)
        self.assertEqual(sig['side'], 'SHORT')
        self.assertGreater(sig['stop_loss_price'], sig['reference_price'])  # SL вище для SHORT

    async def test_long_flat_mode_no_short(self):
        closes = [140 - i for i in range(40)]
        s = _strategy(closes)
        s._params['mode'] = 'long_flat'
        sig = await s._reconcile('BTC-USDT', _bucket_now(), DAY)
        # шортів немає — ціль flat (трейдер закриє LONG, якщо був, і нічого не відкриє)
        self.assertEqual(sig['action'], 'TARGET')
        self.assertIsNone(sig['side'])



class TestTraderTargetReconcile(unittest.IsolatedAsyncioTestCase):
    """Звірка цілі TsMomentum з ВЛАСНИМИ позиціями користувача (SimpleTrader)."""
    META = {'mode': 'roi_gap', 'breakeven_roi': 10.0, 'start_roi': 30.0, 'gap_roi': 20.0}

    def _trader(self, positions):
        import core.trading.simple_trader as st
        trader = st.SimpleTrader.__new__(st.SimpleTrader)
        trader.open_positions = positions
        trader.db = mock.MagicMock()
        trader.close_position = mock.AsyncMock(return_value=True)
        trader._handle_signal_open = None
        trader.open_position = mock.AsyncMock(return_value=True)
        trader.settings_manager = None
        trader._last_close_at = {}
        trader._loss_close_at = {}
        trader._profit_close_at = {}
        return trader

    async def _closed(self, trader, net_pnl, roe, strategy='TsMomentumStrategy'):
        from core.events import Event, EventType
        await trader._remember_close(Event(type=EventType.POSITION_CLOSED, data={
            'symbol': 'BTC-USDT', 'strategy': strategy, 'net_pnl': net_pnl, 'roe_percent': roe,
        }))

    async def test_pause_after_loss_and_breakeven_only(self):
        for net_pnl, roe, paused in [(-1.0, -10.0, True), (0.05, 1.5, True), (-0.03, 0.7, True), (1.2, 12.0, False)]:
            trader = self._trader({})
            await self._closed(trader, net_pnl, roe)
            await trader._handle_target(self._target('LONG', reentry_pause_minutes=30))
            self.assertEqual(trader.open_position.await_count, 0 if paused else 1, (net_pnl, roe))

    async def test_pause_expires(self):
        import time
        trader = self._trader({})
        await self._closed(trader, -1.0, -10.0)
        trader._loss_close_at[('BTC-USDT', 'TsMomentumStrategy')] = time.time() - 1801
        await trader._handle_target(self._target('LONG', reentry_pause_minutes=30))
        trader.open_position.assert_awaited_once()

    async def test_pause_is_per_strategy_and_can_be_disabled(self):
        trader = self._trader({})
        await self._closed(trader, -1.0, -10.0, strategy='WallBreakoutStrategy')
        await trader._handle_target(self._target('LONG', reentry_pause_minutes=30))
        trader.open_position.assert_awaited_once()
        trader = self._trader({})
        await self._closed(trader, -1.0, -10.0)
        await trader._handle_target(self._target('LONG', reentry_pause_minutes=0))
        trader.open_position.assert_awaited_once()

    async def test_pause_does_not_block_flip_close(self):
        from core.common.position_math import position_key
        pos = {'strategy': 'TsMomentumStrategy', 'side': 'SHORT', 'order_id': 'o1'}
        trader = self._trader({position_key('BTC-USDT', 'SHORT'): pos})
        await self._closed(trader, -1.0, -10.0)
        await trader._handle_target(self._target('LONG', reentry_pause_minutes=30))
        trader.close_position.assert_awaited_once()

    def _target(self, side, **extra):
        return {'action': 'TARGET', 'symbol': 'BTC-USDT', 'side': side, 'strategy': 'TsMomentumStrategy',
                'trail_meta': dict(self.META), 'refresh_only': False, 'stop_loss_price': 90.0,
                'reference_price': 100.0, 'quantity': None, 'risk_percent': 1.0, 'leverage': 10, **extra}

    async def test_hold_when_same_side_and_refresh_meta(self):
        from core.common.position_math import position_key
        pos = {'strategy': 'TsMomentumStrategy', 'side': 'LONG', 'trail_meta': {'mode': 'none'}, 'order_id': 'o1'}
        trader = self._trader({position_key('BTC-USDT', 'LONG'): pos})
        await trader._handle_target(self._target('LONG'))
        trader.close_position.assert_not_awaited()
        trader.open_position.assert_not_awaited()
        self.assertEqual(pos['trail_meta'], self.META)  # стара позиція отримала roi_gap

    async def test_close_on_flip(self):
        from core.common.position_math import position_key
        pos = {'strategy': 'TsMomentumStrategy', 'side': 'SHORT', 'order_id': 'o1'}
        trader = self._trader({position_key('BTC-USDT', 'SHORT'): pos})
        await trader._handle_target(self._target('LONG'))
        trader.close_position.assert_awaited_once_with('BTC-USDT', 'SHORT', 'tsmom_flip')
        trader.open_position.assert_not_awaited()  # відкриття — на наступній звірці

    async def test_open_when_flat(self):
        trader = self._trader({})
        await trader._handle_target(self._target('LONG'))
        trader.open_position.assert_awaited_once()
        self.assertEqual(trader.open_position.call_args.kwargs['side'], 'LONG')

    async def test_open_passes_fit_leverage(self):
        trader = self._trader({})
        await trader._handle_target(self._target('LONG', fit_leverage=True))
        self.assertTrue(trader.open_position.call_args.kwargs['fit_leverage'])

    async def test_pause_after_profit_exit_same_side_only(self):
        from core.events import Event, EventType
        trader = self._trader({})
        await trader._remember_close(Event(type=EventType.POSITION_CLOSED, data={
            'symbol': 'BTC-USDT', 'side': 'LONG', 'strategy': 'TsMomentumStrategy', 'net_pnl': 12.0,
            'roe_percent': 60.0, 'close_order_type': 'STOP_MARKET'}))
        trader.db.save_setting.assert_called_once()
        await trader._handle_target(self._target('LONG', reentry_pause_after_profit_minutes=20160))
        trader.open_position.assert_not_awaited()
        await trader._handle_target(self._target('SHORT', reentry_pause_after_profit_minutes=20160))
        trader.open_position.assert_awaited_once()

    async def test_profit_pause_survives_restart(self):
        import json
        import core.trading.simple_trader as st
        import time as _t
        trader = self._trader({})
        trader.db.get_setting.return_value = json.dumps({'BTC-USDT|TsMomentumStrategy': [_t.time(), 'LONG']})
        trader._profit_close_at = trader._load_profit_pauses()
        await trader._handle_target(self._target('LONG', reentry_pause_after_profit_minutes=20160))
        trader.open_position.assert_not_awaited()

    async def test_refresh_keeps_entry_atr(self):
        from core.common.position_math import position_key
        old = {'mode': 'roi_gap', 'unit': 'atr', 'atr': 2.0, 'lock_trigger_atr': 8.0, 'lock_stop_atr': 4.0,
               'trail_start_atr': 15.0, 'trail_gap_atr': 7.0}
        pos = {'strategy': 'TsMomentumStrategy', 'side': 'LONG', 'trail_meta': dict(old), 'order_id': 'o1'}
        trader = self._trader({position_key('BTC-USDT', 'LONG'): pos})
        new = {**old, 'atr': 3.5, 'lock_trigger_atr': 10.0}
        await trader._handle_target(self._target('LONG', trail_meta=new, refresh_only=True))
        self.assertEqual(pos['trail_meta']['atr'], 2.0)              # ATR входу
        self.assertEqual(pos['trail_meta']['lock_trigger_atr'], 10.0)  # нові параметри

    async def test_refresh_only_never_opens_or_closes(self):
        from core.common.position_math import position_key
        pos = {'strategy': 'TsMomentumStrategy', 'side': 'SHORT', 'order_id': 'o1'}
        trader = self._trader({position_key('BTC-USDT', 'SHORT'): pos})
        await trader._handle_target(self._target('LONG', refresh_only=True))
        trader.close_position.assert_not_awaited()
        trader.open_position.assert_not_awaited()

    async def test_other_strategy_position_ignored(self):
        from core.common.position_math import position_key
        pos = {'strategy': 'TrendSupertrendStrategy', 'side': 'SHORT', 'order_id': 'o1'}
        trader = self._trader({position_key('BTC-USDT', 'SHORT'): pos})
        await trader._handle_target(self._target('LONG'))
        trader.close_position.assert_not_awaited()  # чужу позицію не чіпаємо


def _bucket_now():
    import time
    return int(time.time() * 1000) // DAY


class TestMaxMargin(unittest.TestCase):
    def _trader(self, max_margin):
        import core.trading.simple_trader as st
        trader = st.SimpleTrader.__new__(st.SimpleTrader)
        trader.settings_manager = mock.MagicMock()
        trader.settings_manager.get_max_margin_usdt.return_value = max_margin
        return trader

    def test_big_margin_cut_to_maximum(self):
        # 20 * 100 / 10 = 200 USDT маржі -> до 100 USDT: 100 * 10 / 100 = 10
        qty = self._trader(100.0)._apply_max_margin('X-USDT', 'LONG', 20.0, 10, 100.0)
        self.assertAlmostEqual(qty, 10.0)

    def test_small_margin_untouched(self):
        self.assertEqual(self._trader(100.0)._apply_max_margin('X-USDT', 'LONG', 5.0, 10, 100.0), 5.0)

    def test_zero_disables_cap(self):
        self.assertEqual(self._trader(0.0)._apply_max_margin('X-USDT', 'LONG', 20.0, 10, 100.0), 20.0)


class TestRiskSettingsMargins(unittest.IsolatedAsyncioTestCase):
    def _session(self, min_margin=10.0, max_margin=100.0):
        session = mock.MagicMock()
        sm = session.settings_manager
        sm.get_min_margin_usdt.return_value = min_margin
        sm.get_max_margin_usdt.return_value = max_margin
        sm.get_max_stop_roi_percent.return_value = 30.0
        sm.set_min_margin_usdt = mock.AsyncMock()
        sm.set_max_margin_usdt = mock.AsyncMock()
        session.risk_manager.effective_max_open_positions.return_value = 3
        return session

    async def test_saves_max_margin(self):
        from webapp.backend.routes.settings import set_risk_settings
        session = self._session()
        await set_risk_settings({"max_margin_usdt": 50}, session)
        session.settings_manager.set_max_margin_usdt.assert_awaited_once_with(50.0)

    async def test_min_above_max_rejected(self):
        from fastapi import HTTPException
        from webapp.backend.routes.settings import set_risk_settings
        session = self._session()
        with self.assertRaises(HTTPException):
            await set_risk_settings({"min_margin_usdt": 150}, session)
        with self.assertRaises(HTTPException):
            await set_risk_settings({"max_margin_usdt": 5}, session)
        session.settings_manager.set_min_margin_usdt.assert_not_awaited()
        session.settings_manager.set_max_margin_usdt.assert_not_awaited()

    async def test_zero_max_disables_check(self):
        from webapp.backend.routes.settings import set_risk_settings
        session = self._session(max_margin=0.0)
        await set_risk_settings({"min_margin_usdt": 150}, session)
        session.settings_manager.set_min_margin_usdt.assert_awaited_once_with(150.0)


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
    META = {'mode': 'roi_gap', 'breakeven_roi': 10.0, 'breakeven_stop_roi': 2.0, 'start_roi': 30.0, 'gap_roi': 20.0}

    def test_target_levels(self):
        from core.risk.trailing_roi_gap import roi_gap_target
        self.assertIsNone(roi_gap_target(self.META, 8.0, 1.0))        # рано
        self.assertEqual(roi_gap_target(self.META, 12.0, 1.0), 1.0)    # безубиток
        self.assertEqual(roi_gap_target(self.META, 50.0, 1.0), 30.0)   # пік 50 - 20

    async def test_signal_uses_roi_gap(self):
        closes = [100 + i for i in range(40)]
        s = _strategy(closes)
        sig = await s._reconcile('BTC-USDT', _bucket_now(), DAY)
        meta = sig['trail_meta']
        self.assertEqual(
            {k: v for k, v in meta.items() if k != 'atr'},
            {'mode': 'roi_gap', 'unit': 'atr', 'lock_trigger_atr': 0.0, 'lock_stop_atr': 0.0,
             'trail_start_atr': 12.0, 'trail_gap_atr': 7.0},
        )
        self.assertAlmostEqual(meta['atr'], sig['analytics_context']['atr'])
        # стоп = 3 ATR від ціни
        self.assertAlmostEqual(sig['reference_price'] - sig['stop_loss_price'], 3.0 * meta['atr'])

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

    async def _breakeven_target(self, meta):
        from core.risk.trailing_stop_manager import TrailingStopManager
        from core.risk.trailing_state import TrailState
        tsm = TrailingStopManager.__new__(TrailingStopManager)
        tsm.taker_fee_rate = 0.0005
        tsm._retry_after = {}
        tsm.move_retry_cooldown_seconds = 10
        position = {'symbol': 'BTC-USDT', 'side': 'LONG', 'entry_price': 100.0, 'leverage': 10,
                    'stop_loss_price': 88.0, 'sl_order_id': 'sl1', 'trail_meta': meta}
        tsm.trader = mock.MagicMock()
        tsm.trader.open_positions = {'k': position}
        tsm._move_stop_loss = mock.AsyncMock(return_value=1)
        await tsm._process_roi_gap_position('k', position, TrailState(), 101.5, 10.0)
        return tsm._move_stop_loss.call_args.args[3][0]

    async def test_breakeven_stop_from_param(self):
        _, roi, price = await self._breakeven_target({**self.META, 'breakeven_stop_roi': 3.0})
        self.assertAlmostEqual(roi, 3.0)
        self.assertAlmostEqual(price, 100.3)

    async def test_old_meta_without_param_uses_fee_reserve(self):
        meta = {k: v for k, v in self.META.items() if k != 'breakeven_stop_roi'}
        _, roi, _ = await self._breakeven_target(meta)
        self.assertAlmostEqual(roi, 2.0)  # 2 × round-trip комісія при 10x

    def test_breakeven_stop_must_be_below_trigger(self):
        cfg = TsMomentumStrategy.build_config(None)
        cfg['profit_lock_trigger_atr'] = 8.0
        cfg['profit_lock_stop_atr'] = 8.0
        with self.assertRaises(ValueError):
            TsMomentumStrategy._parse_params(cfg)
        cfg['profit_lock_stop_atr'] = 3.0
        self.assertEqual(TsMomentumStrategy._parse_params(cfg)['profit_lock_stop_atr'], 3.0)
        cfg['profit_trail_gap_atr'] = 15.0
        with self.assertRaises(ValueError):
            TsMomentumStrategy._parse_params(cfg)

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


class TestAtrProfitLock(unittest.IsolatedAsyncioTestCase):
    """roi_gap у одиницях ATR: рівні фіксації переводяться в ROI за плечем позиції."""
    META = {'mode': 'roi_gap', 'unit': 'atr', 'atr': 2.0, 'lock_trigger_atr': 8.0, 'lock_stop_atr': 4.0,
            'trail_start_atr': 15.0, 'trail_gap_atr': 7.0}

    def _tsm(self, position):
        from core.risk.trailing_stop_manager import TrailingStopManager
        tsm = TrailingStopManager.__new__(TrailingStopManager)
        tsm.taker_fee_rate = 0.0005
        tsm._retry_after = {}
        tsm.move_retry_cooldown_seconds = 10
        tsm.trader = mock.MagicMock()
        tsm.trader.open_positions = {'k': position}
        tsm._move_stop_loss = mock.AsyncMock(return_value=1)
        return tsm

    def _pos(self, leverage, side='LONG', stop=94.0):
        return {'symbol': 'BTC-USDT', 'side': side, 'entry_price': 100.0, 'leverage': leverage,
                'stop_loss_price': stop, 'sl_order_id': 'sl1', 'trail_meta': dict(self.META)}

    def test_conversion_depends_on_leverage(self):
        from core.risk.trailing_roi_gap import atr_meta_to_roi
        m3 = atr_meta_to_roi(self.META, 100.0, 3)
        self.assertAlmostEqual(m3['breakeven_roi'], 48.0)       # 8 ATR = 16% ціни × 3
        self.assertAlmostEqual(m3['breakeven_stop_roi'], 24.0)
        self.assertAlmostEqual(m3['gap_roi'], 42.0)
        self.assertAlmostEqual(atr_meta_to_roi(self.META, 100.0, 1)['breakeven_roi'], 16.0)
        self.assertEqual(atr_meta_to_roi({**self.META, 'atr': None}, 100.0, 3)['breakeven_roi'], 0.0)

    async def test_no_move_before_lock_trigger(self):
        from core.risk.trailing_state import TrailState
        pos = self._pos(3)
        tsm = self._tsm(pos)
        await tsm._process_roi_gap_position('k', pos, TrailState(), 115.0, 3.0)  # +7.5 ATR
        tsm._move_stop_loss.assert_not_awaited()

    async def test_lock_moves_stop_to_plus_4_atr(self):
        from core.risk.trailing_state import TrailState
        for lev in (1, 3, 7):  # ціна стопу однакова за будь-якого плеча
            pos = self._pos(lev)
            tsm = self._tsm(pos)
            await tsm._process_roi_gap_position('k', pos, TrailState(), 117.0, float(lev))  # +8.5 ATR
            _, _, price = tsm._move_stop_loss.call_args.args[3][0]
            self.assertAlmostEqual(price, 108.0)

    async def test_trail_follows_peak_with_7_atr_gap_short(self):
        from core.risk.trailing_state import TrailState
        pos = self._pos(3, side='SHORT', stop=106.0)
        tsm = self._tsm(pos)
        await tsm._process_roi_gap_position('k', pos, TrailState(), 66.0, 3.0)  # +17 ATR
        _, _, price = tsm._move_stop_loss.call_args.args[3][0]
        self.assertAlmostEqual(price, 80.0)  # пік 66 + 7 ATR


class TestFitLeverage(unittest.TestCase):
    def _trader(self, max_roi):
        from core.trading.simple_trader import SimpleTrader
        t = SimpleTrader.__new__(SimpleTrader)
        t.settings_manager = mock.MagicMock()
        t.settings_manager.get_max_stop_roi_percent.return_value = max_roi
        return t

    def test_leverage_fits_stop_into_max_roi(self):
        t = self._trader(30.0)
        self.assertEqual(t._fit_leverage_to_stop('X', 'LONG', 10, 90.0, 100.0), 3)    # стоп 10% -> 3x
        self.assertEqual(t._fit_leverage_to_stop('X', 'LONG', 10, 75.0, 100.0), 1)    # 25% -> 1x
        self.assertEqual(t._fit_leverage_to_stop('X', 'SHORT', 10, 102.0, 100.0), 10) # 2% -> стеля 10x
        self.assertEqual(self._trader(70.0)._fit_leverage_to_stop('X', 'LONG', 10, 90.0, 100.0), 7)

    def test_stop_not_tightened_after_fit(self):
        t = self._trader(30.0)
        lev = t._fit_leverage_to_stop('X', 'LONG', 10, 90.0, 100.0)
        self.assertEqual(t._clamp_stop_to_max_roi('X', 'LONG', lev, 90.0, 100.0), 90.0)
        # не влазить навіть на 1x -> 1x і стоп стискається
        self.assertEqual(t._fit_leverage_to_stop('X', 'LONG', 10, 60.0, 100.0), 1)
        self.assertAlmostEqual(t._clamp_stop_to_max_roi('X', 'LONG', 1, 60.0, 100.0), 70.0)


class TestStrategyParamsVersion(unittest.TestCase):
    def _store(self, rows):
        from core.database.strategy_settings import StrategySettingsStore
        store = StrategySettingsStore.__new__(StrategySettingsStore)
        coll = mock.MagicMock()

        def find_one(q):
            for r in rows:
                if r['strategy_name'] == q['strategy_name'] and r['is_default'] == q['is_default']:
                    return r
            return None

        def update_one(q, upd):
            for r in rows:
                if r.get('_id') == q.get('_id'):
                    for k, v in upd['$set'].items():
                        if '.' in k:
                            a, b = k.split('.', 1)
                            r[a][b] = v
                        else:
                            r[k] = v
        coll.find_one.side_effect = find_one
        coll.update_one.side_effect = update_one
        store.collection = coll
        return store

    def test_old_version_resets_active_and_default(self):
        rows = [
            {'_id': 1, 'strategy_name': 'S', 'is_default': True, 'params': {'a': 1, 'old': 5}},
            {'_id': 2, 'strategy_name': 'S', 'is_default': False, 'params': {'a': 7, 'old': 5}, 'enabled': True},
        ]
        store = self._store(rows)
        store.seed_defaults('S', {'a': 2, 'new': 3}, params_version=2)
        self.assertEqual(rows[0]['params'], {'a': 2, 'new': 3})
        self.assertEqual(rows[1]['params'], {'a': 2, 'new': 3})
        self.assertTrue(rows[1]['enabled'])
        # повторний старт — уже актуальна версія, правки користувача не чіпаємо
        rows[1]['params']['a'] = 9
        store.seed_defaults('S', {'a': 2, 'new': 3}, params_version=2)
        self.assertEqual(rows[1]['params']['a'], 9)

    def test_no_version_keeps_user_params(self):
        rows = [
            {'_id': 1, 'strategy_name': 'S', 'is_default': True, 'params': {'a': 1}},
            {'_id': 2, 'strategy_name': 'S', 'is_default': False, 'params': {'a': 7}},
        ]
        store = self._store(rows)
        store.seed_defaults('S', {'a': 2, 'b': 1})
        self.assertEqual(rows[1]['params'], {'a': 7, 'b': 1})
