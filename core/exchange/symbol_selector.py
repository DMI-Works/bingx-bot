import asyncio
import logging
import time
from dataclasses import dataclass
from typing import List, Dict, Any, Set, Optional

from .bingx_client import BingXClient
from ..common.market_data import held_symbols, to_float
from ..events import Event, EventBus, EventType

logger = logging.getLogger(__name__)

# Не-крипто контракти BingX (форекс, сировина, акції, індекси): торгуються не
# цілодобово й майже не рухаються — у спільному пулі лише займають слоти.
DEFAULT_EXCLUDED_PREFIXES = ('NCFX', 'NCCO', 'NCSK', 'NCSI')


def _parse_prefixes(raw) -> tuple:
    if raw is None:
        return DEFAULT_EXCLUDED_PREFIXES
    if isinstance(raw, str):
        raw = raw.split(',')
    return tuple(p.strip().upper() for p in raw if p and p.strip())


@dataclass
class TickerCheck:
    """Результат перевірки 24h-тикера фільтрами trading.filters."""
    symbol: str
    quote_volume: float
    last_price: float
    change_pct: float
    passes_volume: bool
    passes_price: bool
    passes_spread: bool
    # (high - low) / low за 24h, % — міра "живості" монети для ранжування
    range_pct: float = 0.0
    spread_pct: Optional[float] = None
    passes_market: bool = True  # не з excluded_symbol_prefixes

    @property
    def passes(self) -> bool:
        return self.passes_volume and self.passes_price and self.passes_spread and self.passes_market

    def as_market_context(self) -> Dict[str, Any]:
        """Знімок ринку монети для trade_analytics.market_context."""
        return {
            'quote_volume_24h': self.quote_volume,
            'change_24h_pct': self.change_pct,
            'range_24h_pct': self.range_pct,
            'spread_pct': self.spread_pct,
        }


class SymbolSelector:
    def __init__(
        self,
        exchange: BingXClient,
        filters: Dict[str, Any],
        signal_tracker=None,
        event_bus: Optional[EventBus] = None,
        settings_manager=None,
    ):
        self.exchange = exchange
        self.filters = filters
        self.signal_tracker = signal_tracker  # SignalActivityTracker | None
        self.event_bus = event_bus  # якщо задано — шле SYMBOLS_ROTATED в ТГ при кожній заміні
        # SettingsManager | None — якщо задано, чорний список з мініаппу
        # (зберігається в БД, керується користувачем в реальному часі)
        # об'єднується з blacklist_symbols із config.yaml при кожному select().
        self.settings_manager = settings_manager
        self._refresh_task: Optional[asyncio.Task] = None
        self.current_symbols: Set[str] = set()

        self._apply_lock = asyncio.Lock()
        self._excluded_prefixes = _parse_prefixes(filters.get('excluded_symbol_prefixes'))
        # symbol -> час, коли монету прибрали як "тиху": до спливу
        # quiet_cooldown_seconds вона не повертається в пул, щоб ротація
        # пробувала інші монети, а не гойдалась між тими самими
        self._quiet_since: Dict[str, float] = {}
        # symbol -> час підписки (молоді підписки ще не можуть бути "тихими")
        self._subscribed_at: Dict[str, float] = {}
        # останні перевірки тикерів (із select()) — для market_context угод
        self._last_checks: Dict[str, TickerCheck] = {}

        if self.event_bus is not None:
            # Монету убрал RiskManager (серия убытков) — сразу отписываемся,
            # не дожидаясь следующей ротации по таймеру.
            self.event_bus.subscribe(EventType.SYMBOL_BLACKLISTED, self._on_symbol_blacklisted)

    async def select(self) -> List[str]:
        """
        Формує список символів для торгівлі:
        - завжди включає whitelist_symbols з конфігу (ваш пріоритетний список)
        - завжди включає символи відкритих позицій (щоб бот не втратив керування
          ними — не можна рвати сокет-підписку, поки живий trailing stop) (п.1)
        - захищає від заміни підписані зараз символи, які НЕ в позиції, але
          давали сигнал за останні `rotation_interval_seconds` — вони
          вважаються "активними" (п.2, дзеркально)
        - "тихі" (без жодного сигналу за цей час) НЕ утримувані символи —
          явні кандидати на заміну свіжими монетами (п.2)
        - додає символи, що проходять фільтри 24h об'єму/спреду/ціни і не є
          не-крипто контрактами (excluded_symbol_prefixes)
        - обмежує загальну кількість символів (max_symbols), пріоритет —
          rank_by: 'volatility' (24h діапазон, за замовчуванням) або 'volume'
        - "тиха" монета не повертається в пул quiet_cooldown_seconds

        rotation_interval_seconds одночасно і період фонового рефрешу
        (SymbolSelector.start_refresh_loop), і поріг "тиші" тут — раніше це
        були два окремих значення (за замовчуванням 3600с і 1800с), які між
        собою не узгоджувались: рефреш раз на годину, а поріг тиші — 30 хв,
        тобто тиха монета фактично чекала заміни ДО 90 хвилин замість
        заявлених 30. Одне значення на обидві ролі прибирає цей розрив.

        Без signal_tracker (None) ротація йде лише за rank_by, без урахування
        активності сигналів.
        """
        blacklist = set(self.filters.get('blacklist_symbols', []))
        if self.settings_manager is not None:
            # чорний список з мініаппу — доповнює (не замінює) статичний
            # з config.yaml, щоб користувач міг додавати монети "на льоту",
            # не чіпаючи файл конфігурації
            blacklist |= set(self.settings_manager.get_blacklist_symbols())
        whitelist = set(self.filters.get('whitelist_symbols', []))
        if self.settings_manager is not None:
            # динамический whitelist з мініаппу ("Добавить в торговлю") —
            # доповнює статичний з config.yaml так само, як і blacklist вище
            whitelist |= set(self.settings_manager.get_whitelist_symbols())
        # blacklist ВСЕГДА приоритетнее whitelist: монета, убранная из торговли
        # (вручную или автоматически после серии убытков), не должна
        # оставаться в подписке только потому, что она есть в whitelist.
        whitelist -= blacklist
        max_symbols = self.filters.get('max_symbols', None)
        rotation_interval_seconds = self.filters.get('rotation_interval_seconds', 1800)

        quiet_cooldown_seconds = self.filters.get('quiet_cooldown_seconds', 7200)
        rank_by = self.filters.get('rank_by', 'volatility')

        held = await self._get_held_symbols()
        tickers = await self._get_tickers()

        now = time.time()
        self._quiet_since = {
            s: t for s, t in self._quiet_since.items() if now - t < quiet_cooldown_seconds
        }

        checks: Dict[str, TickerCheck] = {}
        candidates = []
        for ticker in tickers:
            symbol = ticker.get('symbol')
            if not symbol:
                continue
            check = self.check_ticker(ticker)
            if check is None:
                continue
            checks[symbol] = check
            if symbol in blacklist:
                continue
            if check.passes:
                rank_value = check.range_pct if rank_by == 'volatility' else check.quote_volume
                candidates.append((symbol, rank_value))
        if checks:
            self._last_checks = checks

        candidates.sort(key=lambda c: c[1], reverse=True)
        # монети на паузі після "тиші" — в кінець черги: беруться, лише якщо
        # інших кандидатів не вистачає на всі слоти
        candidates.sort(key=lambda c: c[0] in self._quiet_since)

        # --- захист від ротації ---
        # п.1: whitelist + held (відкриті позиції) — завжди захищені, незалежно
        # від об'єму/сигналів. Виняток — blacklist (див. вище): він вже вирахуваний
        # з whitelist, а held лишається, поки позиція відкрита (SL/TP/trailing).
        protected = set(whitelist) | set(held)

        currently_subscribed = set(getattr(self.exchange, 'subscribed_symbols', set()) or set())
        quiet_unheld: Set[str] = set()

        if self.signal_tracker is not None:
            # п.2: підписані зараз символи БЕЗ позиції, які давали сигнал за
            # останню годину — теж захищені (вони "активні", заміняти їх шкода)
            active_unheld = {
                s for s in currently_subscribed
                if s not in protected
                and s not in blacklist
                and self.signal_tracker.had_signal_within(s, rotation_interval_seconds)
            }
            protected |= active_unheld

            # "тихі" — підписані, не held, не давали сигналу. Раніше вони
            # просто переставали бути protected і на цьому все: якщо тиха
            # монета все ще мала високий об'єм, вона знову проходила
            # volume-фільтр і потрапляла в fresh_candidates, тобто
            # переобиралась сама на себе в тій же ротації — "заміна" була
            # лише в лозі, а не по факту. Тепер вона явно виключається з
            # пулу кандидатів ЦІЄЇ ротації, звільняючи слот для іншої монети.
            # Молода підписка (менше половини rotation_interval, напр. додана
            # позачерговою ротацією після чорного списку) тихою не вважається:
            # монета просто не встигла дати сигнал. Половина, а не весь
            # інтервал — щоб монета з попередньої планової ротації не
            # проскочила як "молода" через мілісекунди різниці.
            quiet_unheld = {
                s for s in currently_subscribed - protected - blacklist
                if now - self._subscribed_at.get(s, 0.0) >= rotation_interval_seconds / 2
            }
            protected |= (currently_subscribed - blacklist) - quiet_unheld
            if quiet_unheld:
                for s in quiet_unheld:
                    self._quiet_since[s] = now
                logger.info(
                    f"[SYMBOLS] Replacing quiet symbols "
                    f"(no signal in {rotation_interval_seconds}s, "
                    f"cooldown {quiet_cooldown_seconds}s): {sorted(quiet_unheld)}"
                )
        else:
            logger.debug("[SYMBOLS] No signal_tracker attached — rotation is volume-only")

        remaining_slots = None
        if max_symbols is not None:
            remaining_slots = max(0, max_symbols - len(protected))

        fresh_candidates = [
            c[0] for c in candidates
            if c[0] not in protected and c[0] not in quiet_unheld
        ]
        if remaining_slots is not None:
            filtered_symbols = set(fresh_candidates[:remaining_slots])
        else:
            filtered_symbols = set(fresh_candidates)

        selected = protected | filtered_symbols

        return sorted(selected)

    def check_ticker(self, ticker: Dict[str, Any]) -> Optional[TickerCheck]:
        """Перевіряє тикер фільтрами об'єму / ціни / спреду з trading.filters.
        Єдина реалізація для select() і webapp /api/symbols/candidates.
        None — якщо цифри в тикері зіпсовані."""
        symbol = ticker.get('symbol')
        try:
            quote_volume = float(ticker.get('quoteVolume', 0))
            last_price = float(ticker.get('lastPrice', 0))
            bid_price = float(ticker.get('bidPrice', 0))
            ask_price = float(ticker.get('askPrice', 0))
        except (TypeError, ValueError):
            return None

        high_price = to_float(ticker.get('highPrice', 0))
        low_price = to_float(ticker.get('lowPrice', 0))
        change_pct = to_float(ticker.get('priceChangePercent', 0))
        # без high/low у тикері — грубо оцінюємо діапазон зміною за 24h
        range_pct = (high_price - low_price) / low_price * 100 if low_price > 0 and high_price > 0 else abs(change_pct)

        passes_market = not (symbol or '').upper().startswith(self._excluded_prefixes) if self._excluded_prefixes else True

        min_price = self.filters.get('min_price', {}) or {}
        max_price = self.filters.get('max_price', {}) or {}
        symbol_min_price = min_price.get(symbol) if isinstance(min_price, dict) else None
        symbol_max_price = max_price.get(symbol) if isinstance(max_price, dict) else None
        passes_price = not (
            (symbol_min_price is not None and last_price < symbol_min_price)
            or (symbol_max_price is not None and last_price > symbol_max_price)
        )

        max_spread_percent = self.filters.get('max_spread_percent', None)
        spread_pct = (ask_price - bid_price) / bid_price * 100 if bid_price > 0 and ask_price > 0 else None
        passes_spread = max_spread_percent is None or spread_pct is None or spread_pct <= max_spread_percent

        return TickerCheck(
            symbol=symbol,
            quote_volume=quote_volume,
            last_price=last_price,
            change_pct=change_pct,
            passes_volume=quote_volume >= self.filters.get('min_volume_24h', 0),
            passes_price=passes_price,
            passes_spread=passes_spread,
            range_pct=range_pct,
            spread_pct=spread_pct,
            passes_market=passes_market,
        )

    def market_context(self, symbol: str) -> Optional[Dict[str, Any]]:
        """24h-знімок монети з останньої ротації (None — монети не було в тикерах)."""
        check = self._last_checks.get(symbol)
        return check.as_market_context() if check else None

    async def apply(self) -> Set[str]:
        async with self._apply_lock:
            return await self._apply_unlocked()

    async def _on_symbol_blacklisted(self, event: Event) -> None:
        symbol = event.data.get('symbol')
        logger.info(f"[SYMBOLS] {symbol} blacklisted ({event.data.get('reason')}) — re-applying symbol selection")
        try:
            await self.apply()
        except Exception as e:
            logger.error(f"[SYMBOLS] Failed to re-apply selection after blacklisting {symbol}: {e}", exc_info=True)

    async def _apply_unlocked(self) -> Set[str]:
        """Обчислює актуальний список символів, підписує нові, відписує зайві.

        Спільний пул: кожен символ у `selected` отримує ОБИДВІ підписки —
        @trade (для стратегій/TrailingStop/ExcursionTracker) і @depth20 (для
        OrderBookAnalyzer). Це свідоме архітектурне рішення — один список
        символів, одна ротація, замість двох незалежних пулів з різними
        лімітами/таймаутами."""
        selected = set(await self.select())
        current = set(self.exchange.subscribed_symbols)

        to_subscribe = selected - current
        to_unsubscribe = current - selected

        subscribed_ok: Set[str] = set()
        unsubscribed_ok: Set[str] = set()

        for symbol in to_subscribe:
            try:
                await self.exchange.subscribe_trades(symbol)
                subscribed_ok.add(symbol)
                self._subscribed_at[symbol] = time.time()
            except Exception as e:
                logger.error(f"Failed to subscribe {symbol}: {e}")
            try:
                await self.exchange.subscribe_depth(symbol)
            except Exception as e:
                logger.error(f"Failed to subscribe depth for {symbol}: {e}")

        for symbol in to_unsubscribe:
            try:
                await self.exchange.unsubscribe_trades(symbol)
                logger.info(f"[SYMBOLS] Unsubscribed: {symbol}")
                unsubscribed_ok.add(symbol)
                self._subscribed_at.pop(symbol, None)
            except Exception as e:
                logger.error(f"Failed to unsubscribe {symbol}: {e}")
            try:
                await self.exchange.unsubscribe_depth(symbol)
            except Exception as e:
                logger.error(f"Failed to unsubscribe depth for {symbol}: {e}")

        if not to_subscribe and not to_unsubscribe:
            logger.info(f"[SYMBOLS] No changes, {len(selected)} symbols active")
        elif self.event_bus is not None and (subscribed_ok or unsubscribed_ok):
            try:
                await self.event_bus.publish(Event(
                    type=EventType.SYMBOLS_ROTATED,
                    data={
                        'added': sorted(subscribed_ok),
                        'removed': sorted(unsubscribed_ok),
                        'total_active': len(selected),
                    },
                    source="SymbolSelector",
                ))
            except Exception as e:
                logger.error(f"[SYMBOLS] Failed to publish SYMBOLS_ROTATED event: {e}")

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
        try:
            return held_symbols(await self.exchange.get_positions())
        except Exception as e:
            logger.error(f"Failed to fetch held positions for symbol selection: {e}")
            return set()

    async def _get_tickers(self) -> List[Dict[str, Any]]:
        try:
            return await self.exchange.get_all_tickers()
        except Exception as e:
            logger.error(f"Failed to fetch tickers for symbol selection: {e}")
            return []