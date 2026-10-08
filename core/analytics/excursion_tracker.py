"""
ExcursionTracker — MFE/MAE угод для trade_analytics.

  MFE (max favorable excursion) — наскільки далеко ціна пішла У ПЛЮС від входу;
  MAE (max adverse excursion)   — наскільки далеко У МІНУС, поки угода була відкрита.

Без цього неможливо відрізнити "вхід був поганий" (ціна одразу пішла проти нас,
MFE ≈ 0) від "вхід був нормальний, але вихід/трейлінг віддав прибуток" (MFE
великий, а результат нульовий або від'ємний). Це перше, що треба знати, щоб
правити налаштування стратегії.

Працює на тих самих тиках PRICE_UPDATED, що й TrailingStopManager, нічого
в торгівлі не змінює. У БД пише не частіше, ніж раз на excursion_flush_seconds
(і лише якщо екстремум змінився), у окремому потоці — щоб синхронний pymongo не
блокував EventBus. Фінальний запис — на POSITION_CLOSED (ціна закриття теж
враховується як екстремум, тож проскальзування стопу потрапляє в MAE).

Після рестарту бота стан підтягується з уже збереженого excursion в БД.
"""

import asyncio
import logging
import time
from datetime import timezone
from typing import Dict, Optional

from ..database import Database
from ..events import EventBus, Event, EventType

logger = logging.getLogger(__name__)


class ExcursionTracker:
    def __init__(self, event_bus: EventBus, db: Database, trader, config: Optional[dict] = None):
        cfg = config or {}
        self.event_bus = event_bus
        self.db = db
        self.trader = trader
        self.enabled: bool = cfg.get('enabled', True)
        self.flush_interval_seconds: float = float(cfg.get('excursion_flush_seconds', 30))

        # order_id -> стан екстремумів (ціни й час — у секундах unix)
        self._state: Dict[str, dict] = {}
        self._tasks: set = set()

        if self.enabled:
            self.event_bus.subscribe(EventType.PRICE_UPDATED, self._on_price_update)
            self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_closed)
            logger.info(f"ExcursionTracker: enabled, flush every {self.flush_interval_seconds:g}s")

    # ---------- тики ----------

    async def _on_price_update(self, event: Event) -> None:
        raw = event.data
        if not raw:
            return
        try:
            tick = raw[0] if isinstance(raw, list) else raw
        except (IndexError, TypeError):
            return

        symbol = tick.get('s')
        try:
            price = float(tick.get('p', 0))
        except (TypeError, ValueError):
            price = 0.0
        if not symbol or price <= 0:
            return

        now = time.time()
        for side in ('LONG', 'SHORT'):
            position = self.trader.open_positions.get(f"{symbol}_{side}")
            if not position or not position.get('order_id') or not position.get('entry_price'):
                continue
            state = self._get_state(position, now)
            self._apply_price(state, price, now)

            if state['dirty'] and not state['flushing'] and now - state['last_flush'] >= self.flush_interval_seconds:
                self._spawn(self._flush(state))

    def _get_state(self, position: dict, now: float) -> dict:
        order_id = str(position['order_id'])
        state = self._state.get(order_id)
        if state is None:
            entry = float(position['entry_price'])
            meta = position.get('trail_meta')
            atr = meta.get('atr') if isinstance(meta, dict) else None
            try:
                leverage = float(position.get('leverage') or 0)
            except (TypeError, ValueError):
                leverage = 0.0
            state = {
                'order_id': order_id,
                'side': position['side'],
                'entry': entry,
                'leverage': leverage,
                'atr': float(atr) if atr else None,
                'opened_ts': now,
                'high': entry, 'high_ts': now,
                'low': entry, 'low_ts': now,
                'dirty': True,
                'flushing': False,
                'last_flush': now,  # перший запис — через flush_interval, не на кожен тік
            }
            self._state[order_id] = state
            # якщо бот перезапускався посеред угоди — підтягуємо вже збережене
            self._spawn(self._restore(state))
        return state

    @staticmethod
    def _apply_price(state: dict, price: float, now: float) -> None:
        if price > state['high']:
            state['high'], state['high_ts'], state['dirty'] = price, now, True
        if price < state['low']:
            state['low'], state['low_ts'], state['dirty'] = price, now, True

    # ---------- знімок ----------

    @staticmethod
    def _snapshot(state: dict) -> dict:
        entry = state['entry']
        long_side = state['side'] == 'LONG'
        mfe_price, mfe_ts = (state['high'], state['high_ts']) if long_side else (state['low'], state['low_ts'])
        mae_price, mae_ts = (state['low'], state['low_ts']) if long_side else (state['high'], state['high_ts'])
        sign = 1.0 if long_side else -1.0

        def pct(price: float) -> float:
            return sign * (price - entry) / entry * 100.0

        snap = {
            'mfe_price': mfe_price,
            'mfe_pct': pct(mfe_price),
            'mfe_after_seconds': max(0.0, mfe_ts - state['opened_ts']),
            'mae_price': mae_price,
            'mae_pct': pct(mae_price),  # <= 0
            'mae_after_seconds': max(0.0, mae_ts - state['opened_ts']),
            'updated_at_ts': time.time(),
        }
        if state['leverage'] > 0:
            snap['mfe_roi_pct'] = snap['mfe_pct'] * state['leverage']
            snap['mae_roi_pct'] = snap['mae_pct'] * state['leverage']
        if state['atr']:
            snap['mfe_atr'] = sign * (mfe_price - entry) / state['atr']
            snap['mae_atr'] = sign * (mae_price - entry) / state['atr']
        return snap

    # ---------- запис у БД ----------

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _flush(self, state: dict, extra: Optional[dict] = None) -> None:
        state['flushing'] = True
        state['dirty'] = False
        state['last_flush'] = time.time()
        try:
            snapshot = self._snapshot(state)
            if extra:
                snapshot.update(extra)
            await asyncio.to_thread(self.db.update_trade_excursion, state['order_id'], snapshot)
        except Exception as e:
            state['dirty'] = True  # повторимо на наступному інтервалі
            logger.error(f"ExcursionTracker: failed to save excursion for {state['order_id']}: {e}", exc_info=True)
        finally:
            state['flushing'] = False

    async def _restore(self, state: dict) -> None:
        """Підтягує opened_at і вже збережені екстремуми (після рестарту бота)."""
        try:
            row = await asyncio.to_thread(self.db.get_trade_analytics, state['order_id'])
        except Exception as e:
            logger.warning(f"ExcursionTracker: restore failed for {state['order_id']}: {e}")
            return
        if not row:
            return

        opened_at = row.get('opened_at')
        if opened_at:
            state['opened_ts'] = opened_at.replace(tzinfo=timezone.utc).timestamp()

        saved = row.get('excursion') or {}
        long_side = state['side'] == 'LONG'
        high_key, low_key = ('mfe', 'mae') if long_side else ('mae', 'mfe')
        for key, bound, more_extreme in (
            (high_key, 'high', lambda saved_price, current: saved_price > current),
            (low_key, 'low', lambda saved_price, current: saved_price < current),
        ):
            saved_price = saved.get(f'{key}_price')
            after = saved.get(f'{key}_after_seconds')
            if saved_price and more_extreme(saved_price, state[bound]):
                state[bound] = saved_price
                state[f'{bound}_ts'] = state['opened_ts'] + (after or 0.0)
                state['dirty'] = True

    # ---------- закриття ----------

    async def _on_position_closed(self, event: Event) -> None:
        data = event.data or {}
        order_id = data.get('order_id')
        state = self._state.pop(str(order_id), None) if order_id else None
        if state is None:
            return

        close_price = data.get('close_price')
        try:
            close_price = float(close_price) if close_price is not None else None
        except (TypeError, ValueError):
            close_price = None

        now = time.time()
        if close_price and close_price > 0:
            self._apply_price(state, close_price, now)  # проскальзування стопу -> у MAE

        extra = {'final': True}
        if close_price and close_price > 0:
            sign = 1.0 if state['side'] == 'LONG' else -1.0
            exit_pct = sign * (close_price - state['entry']) / state['entry'] * 100.0
            mfe_pct = self._snapshot(state)['mfe_pct']
            extra['exit_pct'] = exit_pct
            # скільки від піка прибутку віддали до виходу (у п.п. руху ціни)
            extra['gave_back_pct'] = mfe_pct - exit_pct
            if mfe_pct > 0:
                extra['captured_pct_of_mfe'] = exit_pct / mfe_pct * 100.0

        await self._flush(state, extra)

    async def flush_all(self) -> None:
        """На зупинці бота: записати все, що ще не збережено."""
        for state in list(self._state.values()):
            if state['dirty'] and not state['flushing']:
                await self._flush(state)
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
