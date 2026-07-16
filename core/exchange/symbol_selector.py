import asyncio
import logging
from typing import List, Dict, Any, Set, Optional

from .bingx_client import BingXClient

logger = logging.getLogger(__name__)


class SymbolSelector:
    def __init__(self, exchange: BingXClient, filters: Dict[str, Any]):
        self.exchange = exchange
        self.filters = filters
        self._refresh_task: Optional[asyncio.Task] = None
        self.current_symbols: Set[str] = set()

    async def select(self) -> List[str]:
        """
        Формує список символів для торгівлі:
        - завжди включає whitelist_symbols з конфігу (ваш пріоритетний список)
        - завжди включає символи відкритих позицій (щоб бот не втратив керування ними)
        - додає символи, що проходять фільтри 24h об'єму/спреду/ціни
        - обмежує загальну кількість символів (max_symbols), пріоритет — за об'ємом
        """
        blacklist = set(self.filters.get('blacklist_symbols', []))
        whitelist = set(self.filters.get('whitelist_symbols', []))
        min_volume_24h = self.filters.get('min_volume_24h', 0)
        max_spread_percent = self.filters.get('max_spread_percent', None)
        min_price = self.filters.get('min_price', {}) or {}
        max_price = self.filters.get('max_price', {}) or {}
        max_symbols = self.filters.get('max_symbols', None)

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

        if max_symbols is not None:
            filtered_symbols = {c[0] for c in candidates[:max_symbols]}
        else:
            filtered_symbols = {c[0] for c in candidates}

        priority_symbols = whitelist | held_symbols
        selected = priority_symbols | filtered_symbols



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