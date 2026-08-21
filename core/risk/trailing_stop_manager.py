"""
Керування SL у рантаймі: єдиний стоп-лосс позиції переставляється
СХОДИНКАМИ (у % від ціни входу) у бік прибутку, поки ціна не досягла
чергового порогу — і ніколи не рухається назад.

ВАЖЛИВО (з'ясовано емпірично, BingX API error 110406 "Position SL order
already exists"): на BingX SL — це НЕ довільний reduce-only ордер, а
прив'язаний до позиції ОДИН слот. Одночасно не можна мати два SL-ордери на
одну позицію — тому ідея "статичний недоторканий + окремий динамічний
поруч" на цій біржі фізично неможлива. Є лише один SL, і єдиний спосіб
його "посунути" — cancel старого + create нового (публічного "amend
price" для swap-ордерів BingX не надає).

Тому дизайн такий: SL, який виставила стратегія при вході
(position['sl_order_id'] / position['stop_loss_price']), лишається
недоторканим ДОКИ ціна не дійде до першого порогу зі сходинки. Як тільки
поріг досягнуто — цей самий (єдиний) SL переставляється (cancel + create)
на нову ціну і від цього моменту вважається "керованим" цим модулем. Далі
він рухається ЛИШЕ вперед, на кожен наступний ДОСЯГНУТИЙ поріг.

Сходинка (приклад): trail_levels_percent = [2.0, 8.0]
    - рух ціни в наш бік досяг +2% від входу -> SL переставляється на
      +(2% - буфер) від входу;
    - рух досяг +8% -> SL переставляється на +(8% - буфер) від входу.

Буфер (dynamic_stop_buffer_percent) віднімається від рівня перед
розрахунком фактичної ціни стопу: поки тик обробляється і запит іде до
біржі, ціна встигає "втекти" далі, і без запасу цільова ціна може
виявитись вже позаду ринку (біржа відбиває ордер як застарілий, помилка
110412 "current price").

Правила руху:
    - тільки вперед, ніколи в мінус і ніколи нижче вже застосованого рівня;
    - якщо переміщення SL не вдалось (rate limit, збій API) — рівень НЕ
      позначається застосованим, і на наступному тіку буде здійснена нова
      спроба (з коротким кулдауном, щоб не спамити біржу при постійному
      збої).

"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..events import EventBus, Event, EventType
from ..exchange.bingx_client import BingXAPIError

logger = logging.getLogger(__name__)

# Коди помилок BingX, які означають "ордера/позиції більше не існує" —
# ретраїти нема сенсу, просто пропускаємо тик.
_GONE_ERROR_CODES = (100404, 100400, 109400, 109420)
_GONE_ERROR_SUBSTRINGS = ('not exist', 'not found')


@dataclass
class _TrailState:
    """Стан сходинки для однієї позиції (position_key). Живе лише в пам'яті
    цього модуля — єдине джерело правди про сам SL-ордер (id, ціна) лишається
    в SimpleTrader.open_positions (position['sl_order_id'] / ['stop_loss_price']),
    цей модуль лише читає й (при переміщенні) оновлює ці поля."""
    last_applied_level_index: int = -1     # індекс останнього застосованого порогу в trail_levels_percent; -1 = жодного ще не застосовано
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TrailingStopManager:
    def __init__(
        self,
        event_bus: EventBus,
        exchange,          # BingXClient
        db,                 # Database
        trader,              # SimpleTrader — читає й оновлює його open_positions напряму
        config: Optional[dict] = None,
    ):
        self.event_bus = event_bus
        self.exchange = exchange
        self.db = db
        self.trader = trader

        cfg = config or {}
        self.enabled: bool = cfg.get('enabled', True)

        # Сходинки (у % від ціни входу), на яких SL переставляється в бік
        # прибутку. [2.0, 8.0] = коли рух ціни в наш бік досяг +2% від
        # входу — SL стає на +2% (мінус буфер, див. нижче); коли рух досяг
        # +8% — на +8% (мінус буфер). Автоматично сортується й дедублюється;
        # порожній список = SL стратегії ніколи не чіпається.
        raw_levels = cfg.get('trail_levels_percent', [2.0, 8.0])
        self.trail_levels_percent: List[float] = sorted({float(x) for x in raw_levels if x > 0})

        # Буфер (у % від ціни входу), який ВІДНІМАЄМО від досягнутого рівня
        # ПЕРЕД тим, як рахувати фактичну ціну SL. Наприклад, при рівні 2% і
        # буфері 1% реальний стоп ставиться на +1% від входу, а не на +2% —
        # так стоп завжди лишається з запасом позаду ринку на момент
        # виставлення, навіть якщо ціна встигла "стрибнути" далі, поки тик
        # оброблявся.
        self.stop_buffer_percent: float = cfg.get('dynamic_stop_buffer_percent', 1.0)  # 1%

        # Запасний буфер (частка ціни) для re-anchor: використовується ЛИШЕ
        # якщо біржа відбила цільову ціну як застарілу — тоді SL
        # переставляється від живої ціни на цю відстань.
        self.reanchor_buffer_percent: float = cfg.get('reanchor_buffer_percent', 0.001)  # 0.1%

        # короткий кулдаун після НЕВДАЛОЇ спроби перемістити SL (будь-яка
        # причина, не лише rate limit) — щоб не спамити біржу щотіку, поки
        # проблема не зникне сама (зникла zombie-позиція, минув rate limit тощо)
        self.move_retry_cooldown_seconds: float = cfg.get('move_retry_cooldown_seconds', 15.0)

        self._states: Dict[str, _TrailState] = {}
        # position_key -> unix timestamp, до якого не намагаємось рухати SL
        # цієї позиції — виставляється після будь-якої невдалої спроби
        # переміщення (rate limit, zombie-позиція, будь-яка інша помилка API)
        self._retry_after: Dict[str, float] = {}

        if self.enabled:
            self.event_bus.subscribe(EventType.PRICE_UPDATED, self._on_price_update)
            self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_closed)
            logger.info(
                f"TrailingStopManager initialized: levels={self.trail_levels_percent}%, "
                f"buffer={self.stop_buffer_percent}% (single SL slot, moved via cancel+create on level crossings)"
            )
        else:
            logger.info("TrailingStopManager initialized but disabled via config")

    # ---------- вхідна точка: ціна оновилась ----------

    async def _on_price_update(self, event: Event) -> None:
        """
        event.data — той самий формат, що й у BaseStrategy._on_price_update:
        список трейдів з полями 's' (symbol) і 'p' (price) від WS.
        """
        
        if not self.enabled or not self.trail_levels_percent:
            logger.info(
                f"TrailingStop: no-op tick — enabled={self.enabled}, levels={self.trail_levels_percent}"
            )
            return

        raw = event.data
        logger.info(
                f"TrailingStop: data {raw}"
        )
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

        logger.info(
                f"TrailingStop: price {price}"
        )
        if not symbol or price <= 0:
            return

        # позиція може бути LONG і/або SHORT одночасно (hedge mode) — перевіряємо обидві
        for side in ('LONG', 'SHORT'):
            position_key = f"{symbol}"
            position = self.trader.open_positions.get(position_key)
            logger.info(
                f"TrailingStop: position {position}"
            )
            if not position:
                continue
            await self._process_position(position_key, position, price)

    def _highest_reached_level_index(self, favorable_fraction: float, last_applied_index: int) -> int:
        """Повертає індекс найвищого ЩЕ НЕ застосованого порогу з
        trail_levels_percent, якого досягла поточна сприятлива зміна ціни
        (favorable_fraction — частка від ціни входу). Якщо ціна одним тіком
        проскочила відразу кілька порогів (геп/волатильність) — повертає
        найвищий з них, а не перший. Якщо новий поріг не досягнуто —
        повертає last_applied_index без змін.
        """
        target = last_applied_index
        for i, level_percent in enumerate(self.trail_levels_percent):
            if i <= target:
                continue
            if favorable_fraction >= level_percent / 100.0:
                target = i
        return target

    async def _process_position(self, position_key: str, position: dict, price: float) -> None:
        retry_after = self._retry_after.get(position_key)
        if retry_after is not None:
            if time.time() < retry_after:
                return
            self._retry_after.pop(position_key, None)

        entry_price = position.get('entry_price')
        side = position.get('side')
        if not entry_price or entry_price <= 0 or side not in ('LONG', 'SHORT'):
            logger.debug(
                f"TrailingStop: {position_key} SKIP: invalid entry_price/side "
                f"(entry_price={entry_price}, side={side})"
            )
            return
        if not position.get('sl_order_id'):
            # немає жодного SL на позиції — не наша задача його створювати
            logger.debug(f"TrailingStop: {position_key} SKIP: no sl_order_id on position")
            return

        favorable_fraction = (
            (price - entry_price) / entry_price if side == 'LONG'
            else (entry_price - price) / entry_price
        )
        if favorable_fraction <= 0:
            # позиція в мінусі або рівно на вході — НІКОЛИ не рухаємо SL у
            # цей бік, лише вперед, у прибуток
            return

        state = self._states.setdefault(position_key, _TrailState())

        target_index = self._highest_reached_level_index(favorable_fraction, state.last_applied_level_index)
        if target_index <= state.last_applied_level_index:
            return  # жодного нового порогу не досягнуто

        level_percent = self.trail_levels_percent[target_index]
        # реальну ціну стопу рахуємо не від самого рівня, а від рівня МІНУС
        # буфер — так SL завжди лишається трохи позаду ринку на момент
        # виставлення (детальніше — у docstring модуля). Ніколи не йдемо
        # нижче 0% (тобто гірше за вхід).
        effective_percent = max(level_percent - self.stop_buffer_percent, 0.0)
        desired_stop = (
            entry_price * (1 + effective_percent / 100.0) if side == 'LONG'
            else entry_price * (1 - effective_percent / 100.0)
        )

        async with state.lock:
            # перечитуємо: поки чекали на лок, інший тик міг уже застосувати
            # цей самий або вищий рівень, або позиція могла закритись
            if target_index <= state.last_applied_level_index:
                return
            position = self.trader.open_positions.get(position_key)
            if not position or not position.get('sl_order_id'):
                return

            current_stop = position.get('stop_loss_price')
            if current_stop is not None:
                # SL рухається ЛИШЕ вперед: якщо порахований desired_stop
                # чомусь не кращий за вже виставлений (наприклад, буфер
                # з'їв усю відстань між близькими рівнями) — не чіпаємо
                is_improvement = (
                    desired_stop > current_stop if side == 'LONG'
                    else desired_stop < current_stop
                )
                if not is_improvement:
                    state.last_applied_level_index = target_index  # рівень технічно "пройдено", просто нема чого рухати
                    return

            success = await self._move_stop_loss(position_key, position, desired_stop, level_percent)
            if success:
                state.last_applied_level_index = target_index
            else:
                # НЕ позначаємо рівень застосованим — спробуємо ще раз
                # пізніше (після короткого кулдауну, щоб не спамити біржу)
                self._retry_after[position_key] = time.time() + self.move_retry_cooldown_seconds

    # ---------- реальне переміщення SL на біржі ----------

    async def _move_stop_loss(
        self, position_key: str, position: dict, desired_stop_price: float, level_percent: float
    ) -> bool:
        """Переставляє єдиний SL позиції (cancel старого + create нового).
        Повертає True при успіху, False при будь-якій невдачі (виклик, що
        не позначає рівень застосованим і намагається ще раз пізніше)."""
        symbol = position['symbol']
        side = position['side']
        quantity = position.get('remaining_quantity') or position.get('quantity')
        close_side = 'SELL' if side == 'LONG' else 'BUY'
        position_side = side

        old_sl_order_id = position.get('sl_order_id')
        old_stop_price = position.get('stop_loss_price')

        # 1) відміняємо поточний SL
        try:
            await self.exchange.cancel_order(symbol, old_sl_order_id)
        except BingXAPIError as e:
            if e.code == 109429:
                self._apply_rate_limit_backoff(position_key, e.msg)
                logger.warning(f"TrailingStop: rate limited (109429) cancelling SL for {position_key}")
                return False
            if self._is_gone_error(e):
                # ордер/позиція вже не існує на біржі — або позиція щойно
                # закрилась (SL спрацював), або це zombie-позиція в
                # open_positions. У будь-якому разі виставляти новий SL
                # безглуздо — виходимо без помилки/паніки.
                logger.info(
                    f"TrailingStop: {position_key} SL/position already gone "
                    f"(orderId={old_sl_order_id}): {e.code} {e.msg}, skipping"
                )
                return False
            logger.error(f"TrailingStop: failed to cancel SL for {position_key}: {e.code} {e.msg}")
            return False
        except Exception as e:
            logger.error(f"TrailingStop: unexpected error cancelling SL for {position_key}: {e}", exc_info=True)
            return False

        # 2) створюємо новий SL — з ретраєм на "stale price": якщо біржа
        # каже "current price" (ціна вже пройшла ціль), перераховуємо ціль
        # від живої ціни й пробуємо ще раз (до 3 спроб), ніколи не гірше за
        # стоп, який щойно відмінили.
        current_stop_price = desired_stop_price
        client_order_id = f"sl-{int(time.time() * 1000)}"
        max_attempts = 3
        last_error: Optional[BingXAPIError] = None
        response = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = await self.exchange.create_order(
                    symbol=symbol,
                    side=close_side,
                    order_type='STOP_MARKET',
                    quantity=quantity,
                    stop_price=current_stop_price,
                    position_side=position_side,
                    close_position=True,
                    client_order_id=client_order_id,
                )
                break
            except BingXAPIError as e:
                last_error = e
                if e.code == 109429:
                    self._apply_rate_limit_backoff(position_key, e.msg)
                    logger.error(
                        f"TrailingStop: rate limited (109429) creating new SL for {position_key} "
                        f"AFTER cancelling the old one — POSITION MAY NOW BE WITHOUT A STOP!"
                    )
                    await self._notify_critical(
                        f"⚠️ Rate limit при перевиставленні SL для {symbol} {side} — "
                        f"старий SL вже відмінено, новий не вдалось створити. Перевірте вручну!"
                    )
                    return False
                if self._is_gone_error(e):
                    # позиція закрилась між cancel і create (наприклад,
                    # ринковий ордер/інший процес закрив її) — нема куди
                    # ставити SL, це не помилка
                    logger.info(f"TrailingStop: {position_key} position gone before new SL could be created, skipping")
                    return False
                if e.code == 110406:
                    # "Position SL order already exists" — попри те, що ми
                    # щойно відмінили старий, біржа бачить якийсь SL на
                    # позиції. Це означає, що позиція, ймовірно, ВСЕ Ж
                    # ЗАХИЩЕНА (просто не тим ордером, який ми відстежуємо) —
                    # не панікуємо, але лишаємо слід для ручної перевірки.
                    logger.warning(
                        f"TrailingStop: {position_key} got 110406 (SL already exists) right after our own "
                        f"cancel — some SL order is present on the exchange, but our tracked id is stale. "
                        f"Skipping this attempt; will resync next tick."
                    )
                    return False
                if 'current price' in (e.msg or '').lower() and attempt < max_attempts:
                    try:
                        live_price = await self.exchange.get_ticker_price(symbol)
                        raw_reanchored = (
                            live_price * (1 - self.reanchor_buffer_percent) if side == 'LONG'
                            else live_price * (1 + self.reanchor_buffer_percent)
                        )
                        if old_stop_price is not None:
                            current_stop_price = (
                                max(raw_reanchored, old_stop_price) if side == 'LONG'
                                else min(raw_reanchored, old_stop_price)
                            )
                        else:
                            current_stop_price = raw_reanchored
                        logger.warning(
                            f"TrailingStop: SL for {position_key} rejected as stale "
                            f"(target {desired_stop_price} already passed by price). "
                            f"Re-anchoring to live price {live_price} -> new stop={current_stop_price}, "
                            f"retrying (attempt {attempt + 1})"
                        )
                        continue
                    except Exception as fetch_err:
                        logger.error(f"TrailingStop: failed to fetch live price to re-anchor SL for {symbol}: {fetch_err}")
                        break
                break
            except Exception as e:
                logger.error(f"TrailingStop: unexpected error creating new SL for {position_key}: {e}", exc_info=True)
                await self._notify_critical(
                    f"⚠️ Не вдалося перевиставити SL для {symbol} {side} (старий вже відмінено, "
                    f"позиція, можливо, БЕЗ захисту): {e}"
                )
                return False

        if response is None:
            logger.error(
                f"TrailingStop: failed to create new SL for {position_key} after {max_attempts} attempts: "
                f"{last_error.code if last_error else '?'} {last_error.msg if last_error else ''}. "
                f"POSITION MAY NOW BE WITHOUT A STOP — check manually!"
            )
            await self._notify_critical(
                f"⚠️ Не вдалося перевиставити SL для {symbol} {side} на {desired_stop_price:.6f} "
                f"(рівень {level_percent:g}%, старий SL вже відмінено): "
                f"{last_error.code if last_error else '?'} {last_error.msg if last_error else ''}. "
                f"Перевірте вручну — позиція може бути без захисту!"
            )
            return False

        new_stop_price = current_stop_price
        new_order_id = None
        if 'data' in response and 'order' in response['data']:
            new_order_id = response['data']['order'].get('orderId')

        if not new_order_id:
            logger.error(
                f"TrailingStop: SL created for {position_key} but no orderId in response: {response}."
            )
            await self._notify_critical(
                f"SL для {symbol} {side} перевиставлено, але не вдалось прочитати orderId — перевірте вручну!"
            )

        # 3) оновлюємо СПІЛЬНИЙ стан позиції (той самий словник, що бачить
        # SimpleTrader/TelegramBot) — це єдиний SL позиції, інших полів нема
        position['stop_loss_price'] = new_stop_price
        position['sl_order_id'] = str(new_order_id) if new_order_id else None
        position['sl_client_order_id'] = client_order_id

        try:
            self.db.update_position_metadata(
                order_id=position['order_id'],
                metadata=json.dumps(position),
            )
        except Exception as e:
            logger.error(f"TrailingStop: failed to persist moved SL to DB for {position_key}: {e}", exc_info=True)

        logger.info(
            f"TrailingStop: SL for {position_key} -> {new_stop_price:.6f} "
            f"(trigger_level={level_percent:g}%, buffer={self.stop_buffer_percent:g}%, "
            f"old_order={old_sl_order_id}, new_order={new_order_id})"
        )

        try:
            await self.event_bus.publish(Event(
                type=EventType.STOP_LOSS_MOVED,
                data={
                    'symbol': symbol,
                    'side': side,
                    'stage': f"level_{level_percent:g}pct",
                    'entry_price': position.get('entry_price'),
                    'old_stop_price': old_stop_price,
                    'new_stop_price': new_stop_price,
                    'strategy': position.get('strategy'),
                },
                source="TrailingStopManager",
            ))
        except Exception as e:
            logger.error(f"TrailingStop: failed to publish STOP_LOSS_MOVED event: {e}")

        return True

    # ---------- допоміжне ----------

    @staticmethod
    def _is_gone_error(e: BingXAPIError) -> bool:
        if e.code in _GONE_ERROR_CODES:
            return True
        msg = (e.msg or '').lower()
        return any(s in msg for s in _GONE_ERROR_SUBSTRINGS)

    def _apply_rate_limit_backoff(self, position_key: str, error_msg: Optional[str]) -> None:
        retry_at = self._parse_retry_after(error_msg) or (time.time() + 60.0)
        self._retry_after[position_key] = retry_at

    @staticmethod
    def _parse_retry_after(msg: Optional[str]) -> Optional[float]:
        """Парсить 'can retry after time: 1786999966671' (мс epoch, unix) з
        тіла помилки BingX і повертає unix timestamp у секундах, до якого
        варто утриматись від нових запитів для цього ендпоінту/акаунту."""
        if not msg:
            return None
        m = re.search(r'retry after time:\s*(\d+)', msg)
        if not m:
            return None
        try:
            return int(m.group(1)) / 1000.0
        except ValueError:
            return None

    async def _notify_critical(self, message: str) -> None:
        try:
            await self.event_bus.publish(Event(
                type=EventType.CRITICAL_ERROR,
                data={'context': message, 'error': ''},
            ))
        except Exception as e:
            logger.error(f"TrailingStop: failed to publish critical error notification: {e}")

    # ---------- прибирання стану при закритті позиції ----------

    async def _on_position_closed(self, event: Event) -> None:
        symbol = event.data.get('symbol')
        side = event.data.get('side')
        if not symbol or not side:
            return
        position_key = f"{symbol}_{side}"
        self._states.pop(position_key, None)
        self._retry_after.pop(position_key, None)
