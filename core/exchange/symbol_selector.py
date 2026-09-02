import asyncio
import logging
from typing import List, Dict, Any, Set, Optional

from .bingx_client import BingXClient

logger = logging.getLogger(__name__)


class SymbolSelector:
    def __init__(self, exchange: BingXClient, filters: Dict[str, Any], signal_tracker=None):
        self.exchange = exchange
        self.filters = filters
        self.signal_tracker = signal_tracker  # SignalActivityTracker | None
        self._refresh_task: Optional[asyncio.Task] = None
        self.current_symbols: Set[str] = set()

    async def select(self) -> List[str]:
        """
        Формує список символів для торгівлі:
        - завжди включає whitelist_symbols з конфігу (ваш пріоритетний список)
        - завжди включає символи відкритих позицій (щоб бот не втратив керування
          ними — не можна рвати сокет-підписку, поки живий trailing stop) (п.1)
        - захищає від заміни підписані зараз символи, які НЕ в позиції, але
          давали сигнал за останні `no_signal_replace_after_seconds` (дефолт
          3600с) — вони вважаються "активними" (п.2, дзеркально)
        - "тихі" (без жодного сигналу за цей час) НЕ утримувані символи —
          явні кандидати на заміну свіжими за об'ємом (п.2)
        - додає символи, що проходять фільтри 24h об'єму/спреду/ціни
        - обмежує загальну кількість символів (max_symbols), пріоритет — за об'ємом

        Без signal_tracker (None) поведінка деградує до старої: ротація йде
        лише за об'ємом, без урахування активності сигналів.
        """
        blacklist = set(self.filters.get('blacklist_symbols', []))
        whitelist = set(self.filters.get('whitelist_symbols', []))
        min_volume_24h = self.filters.get('min_volume_24h', 0)
        max_spread_percent = self.filters.get('max_spread_percent', None)
        min_price = self.filters.get('min_price', {}) or {}
        max_price = self.filters.get('max_price', {}) or {}
        max_symbols = self.filters.get('max_symbols', None)
        no_signal_replace_after_seconds = self.filters.get('no_signal_replace_after_seconds', 3600)

        held_symbols = await self._get_held_symbols()
        tickers = await self._get_tickers()

        candidates = []

        for ticker in tickers:
            symbol = ticker.get('symbol')
            if not symbol or symbol in blacklist:
                continue

            try:
                quote_volume = float(ticker.get('quoteVolume', 0))
                last_price = float(ticker.get('lastPrice', 0))
                bid_price = float(ticker.get('bidPrice', 0))
                ask_price = float(ticker.get('askPrice', 0))
            except (TypeError, ValueError):
                continue

            if quote_volume < min_volume_24h:
                continue

            symbol_min_price = min_price.get(symbol) if isinstance(min_price, dict) else None
            symbol_max_price = max_price.get(symbol) if isinstance(max_price, dict) else None

            if symbol_min_price is not None and last_price < symbol_min_price:
                continue
            if symbol_max_price is not None and last_price > symbol_max_price:
                continue

            if max_spread_percent is not None and bid_price > 0 and ask_price > 0:
                spread_percent = (ask_price - bid_price) / bid_price * 100
                if spread_percent > max_spread_percent:
                    continue

            candidates.append((symbol, quote_volume))

        candidates.sort(key=lambda c: c[1], reverse=True)

        # --- захист від ротації ---
        # п.1: whitelist + held (відкриті позиції) — завжди захищені, незалежно
        # від об'єму/сигналів. Це вже було раніше і працює коректно.
        protected = set(whitelist) | set(held_symbols)

        currently_subscribed = set(getattr(self.exchange, 'subscribed_symbols', set()) or set())

        if self.signal_tracker is not None:
            # п.2: підписані зараз символи БЕЗ позиції, які давали сигнал за
            # останню годину — теж захищені (вони "активні", заміняти їх шкода)
            active_unheld = {
                s for s in currently_subscribed
                if s not in protected
                and self.signal_tracker.had_signal_within(s, no_signal_replace_after_seconds)
            }
            protected |= active_unheld

            # "тихі" — підписані, не held, не давали сигналу — свідомо НЕ
            # додаються в protected, щоб їх могли витіснити свіжі кандидати
            quiet_unheld = currently_subscribed - protected
            if quiet_unheld:
                logger.info(
                    f"[SYMBOLS] Quiet symbols eligible for replacement "
                    f"(no signal in {no_signal_replace_after_seconds}s): {sorted(quiet_unheld)}"
                )
        else:
            logger.debug("[SYMBOLS] No signal_tracker attached — rotation is volume-only")

        remaining_slots = None
        if max_symbols is not None:
            remaining_slots = max(0, max_symbols - len(protected))

        fresh_candidates = [c[0] for c in candidates if c[0] not in protected]
        if remaining_slots is not None:
            filtered_symbols = set(fresh_candidates[:remaining_slots])
        else:
            filtered_symbols = set(fresh_candidates)

        selected = protected | filtered_symbols

        return sorted(selected)

    async def apply(self) -> Set[str]:
        """Обчислює актуальний список символів, підписує нові, відписує зайві."""
        selected = set(await self.select())
        current = set(self.exchange.subscribed_symbols)

        to_subscribe = selected - current
        to_unsubscribe = current - selected

        for symbol in to_subscribe:
            try:
                await self.exchange.subscribe_trades(symbol)
            except Exception as e:
                logger.error(f"Failed to subscribe {symbol}: {e}")

        for symbol in to_unsubscribe:
            try:
                await self.exchange.unsubscribe_trades(symbol)
                logger.info(f"[SYMBOLS] Unsubscribed: {symbol}")
            except Exception as e:
                logger.error(f"Failed to unsubscribe {symbol}: {e}")

        if not to_subscribe and not to_unsubscribe:
            logger.info(f"[SYMBOLS] No changes, {len(selected)} symbols active")

        self.current_symbols = selected
        return selected

    async def start_refresh_loop(self, interval_seconds: int = 3600) -> None:
        """Запускає фонову задачу періодичного оновлення списку символів."""
        if self._refresh_task is not None:
            logger.warning("Symbol refresh loop already running")
            return

        self._refresh_task = asyncio.create_task(self._refresh_loop(interval_seconds))
        logger.info(f"[SYMBOLS] Refresh loop started (every {interval_seconds}s)")

    async def stop_refresh_loop(self) -> None:
        if self._refresh_task is None:
            return

        self._refresh_task.cancel()
        try:
            await self._refresh_task
        except asyncio.CancelledError:
            pass
        self._refresh_task = None
        logger.info("[SYMBOLS] Refresh loop stopped")

    async def _refresh_loop(self, interval_seconds: int) -> None:
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                logger.info("[SYMBOLS] Refreshing symbol selection...")
                await self.apply()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[SYMBOLS] Error during refresh: {e}", exc_info=True)

    async def _get_held_symbols(self) -> Set[str]:
        held_symbols = set()
        try:
            positions = await self.exchange.get_positions()
            for pos in positions:
                amt = float(pos.get('positionAmt', 0))
                if amt != 0:
                    held_symbols.add(pos.get('symbol'))
        except Exception as e:
            logger.error(f"Failed to fetch held positions for symbol selection: {e}")
        return held_symbols

    async def _get_tickers(self) -> List[Dict[str, Any]]:
        try:
            return await self.exchange.get_all_tickers()
        except Exception as e:
            logger.error(f"Failed to fetch tickers for symbol selection: {e}")
            return []