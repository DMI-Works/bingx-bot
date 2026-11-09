"""
SessionManager — торгові контури всіх користувачів і маршрутизація до них.

Спільна шина (MarketEngine) -> сесії:
  SIGNAL_GENERATED  — кожній сесії, де стратегію не вимкнено і користувач має
                      право на нові входи. TARGET (TsMomentum) доходить завжди,
                      але з no_open=True, якщо входи заборонені: закриття по
                      розвороту і оновлення trail_meta мають працювати й тоді.
  PRICE_UPDATED     — лише сесіям з відкритою позицією по цьому символу
                      (трейлінг, MFE/MAE). Тіків багато: 100 користувачів ×
                      усі символи поклали б event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Dict, List, Optional

from ..common.market_data import parse_price_tick
from ..events import Event, EventBus, EventType
from ..users import UsersRepository
from ..users.access import can_open_new
from .user_session import UserSession

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(
        self,
        users: UsersRepository,
        mongo_client,
        config,
        shared_bus: EventBus,
        strategy_settings,
        telegram_gateway=None,
        market_context_provider: Optional[Callable] = None,
        targets_provider: Optional[Callable[[], List[dict]]] = None,
    ):
        self.users = users
        self.mongo_client = mongo_client
        self.config = config
        self.shared_bus = shared_bus
        self.strategy_settings = strategy_settings
        self.telegram_gateway = telegram_gateway
        self.market_context_provider = market_context_provider
        self.targets_provider = targets_provider

        self.sessions: Dict[str, UserSession] = {}
        self._trail_levels = None
        self._lock = asyncio.Lock()

        shared_bus.subscribe(EventType.SIGNAL_GENERATED, self._on_signal)
        shared_bus.subscribe(EventType.PRICE_UPDATED, self._on_price)

    # ---------- життєвий цикл ----------

    async def start_all(self) -> None:
        for user in self.users.list_with_keys():
            await self.start_user(user['_id'])
        logger.info(f"[SESSIONS] started {len(self.sessions)} user session(s)")

    async def stop_all(self) -> None:
        for user_id in list(self.sessions):
            await self.stop_user(user_id)

    async def start_user(self, user_id: str) -> Optional[UserSession]:
        """(Пере)запускає сесію користувача з актуальними ключами і режимом."""
        user_id = str(user_id)
        async with self._lock:
            old = self.sessions.pop(user_id, None)
            if old is not None:
                await old.stop()

            user = self.users.get(user_id)
            if not user:
                return None
            testnet = bool(user.get('testnet', True))
            try:
                keys = self.users.decrypted_keys(user, testnet)
            except Exception as e:
                logger.error(f"[SESSIONS] cannot decrypt keys for {user_id}: {e}")
                return None
            if not keys:
                return None

            session = UserSession(
                user, keys['api_key'], keys['api_secret'], self.mongo_client, self.config,
                self.strategy_settings, telegram_gateway=self.telegram_gateway,
                market_context_provider=self.market_context_provider,
            )
            if self._trail_levels is not None:
                session.trailing.set_levels(self._trail_levels)
            try:
                await session.start()
            except Exception as e:
                logger.error(f"[SESSIONS] failed to start session {user_id}: {e}", exc_info=True)
                await session.stop()
                return None
            self.sessions[user_id] = session

        # поточні цілі TsMomentum — щоб новий/перезапущений користувач не чекав доби
        if self.targets_provider is not None:
            for target in self.targets_provider():
                await self._deliver_signal(session, target)
        return session

    async def stop_user(self, user_id: str) -> None:
        async with self._lock:
            session = self.sessions.pop(str(user_id), None)
        if session is not None:
            await session.stop()

    def refresh_user(self, user_id: str) -> None:
        """Перечитати документ користувача (статус/доступ) у працюючу сесію."""
        session = self.sessions.get(str(user_id))
        user = self.users.get(user_id)
        if session is not None and user is not None:
            session.user = user

    def get(self, user_id: str) -> Optional[UserSession]:
        return self.sessions.get(str(user_id))

    # ---------- спільні дані для MarketEngine ----------

    def held_symbols(self) -> set:
        held = set()
        for session in self.sessions.values():
            held |= session.held_symbols()
        return held

    def set_trail_levels(self, levels) -> None:
        """ROI-лестниця WallBreakout — спільний параметр, застосовується всім."""
        self._trail_levels = levels
        for session in self.sessions.values():
            session.trailing.set_levels(levels)

    # ---------- маршрутизація ----------

    async def _on_signal(self, event: Event) -> None:
        for session in list(self.sessions.values()):
            try:
                await self._deliver_signal(session, event.data, source=event.source)
            except Exception as e:
                logger.error(f"[SESSIONS] signal delivery to {session.user_id} failed: {e}", exc_info=True)

    async def _deliver_signal(self, session: UserSession, signal: dict, source: Optional[str] = None) -> None:
        strategy = signal.get('strategy')
        # session.user, а не запит у БД: сигнал іде всім сесіям одразу, 100
        # синхронних запитів затримали б входи (оновлює refresh_user)
        allowed, reason = can_open_new(session.user)

        if signal.get('action') == 'TARGET':
            data = dict(signal)
            if not allowed:
                data['no_open'] = True
            await session.deliver(Event(type=EventType.SIGNAL_GENERATED, data=data, source=source))
            return

        if not allowed:
            logger.info(f"[SESSIONS] {session.user_id}: сигнал {strategy} пропущено — {reason}")
            return
        await session.deliver(Event(type=EventType.SIGNAL_GENERATED, data=signal, source=source))

    async def _on_price(self, event: Event) -> None:
        tick = parse_price_tick(event.data)
        if tick is None:
            return
        symbol = tick[0]
        for session in list(self.sessions.values()):
            if symbol in session.held_symbols():
                await session.deliver(event)
