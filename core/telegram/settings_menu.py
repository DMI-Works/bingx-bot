"""
Універсальний хелпер для побудови вкладених inline-меню в Telegram-боті.

Дозволяє одним і тим самим механізмом описувати:
  - сторінки зі списком кнопок (навігація вглиб/назад, пагінація);
  - toggle/checkbox параметри (bool);
  - вибір значення зі списку варіантів (choice/enum);
  - введення довільного числа/тексту через наступне повідомлення користувача;
  - вмикання/вимикання стратегії цілком (окремий прапорець enabled,
    не пов'язаний з jsonparams).

Є два режими:

  1. АВТО (за замовчуванням) - список стратегій береться прямо з БД
     (StrategySettingsStore.list_strategies()), а тип кожного параметра
     (bool / число / текст) визначається автоматично за поточним значенням.
     Нічого декларувати не треба: де завгодно у своєму коді викликаєте
     store.seed_defaults("MyStrategy", {"period": 14, "enabled": True, ...})
     - і ця стратегія та її параметри самі з'являться в меню /settings.

  2. РУЧНИЙ ОВЕРРАЙД - якщо для якогось параметра потрібен явний список
     варіантів (choice) чи людська назва замість технічного ключа, можна
     передати StrategySchema саме для цієї стратегії; решта стратегій без
     оверрайду однаково працюватимуть в авто-режимі.

ВАЖЛИВО ПРО МИТТЄВЕ ЗАСТОСУВАННЯ:
  Сам по собі SettingsMenu лише читає/пише StrategySettingsStore (тобто
  БД). Якщо живі інстанси стратегій створюються один раз при старті бота
  і більше ніхто їх не оновлює, то зміни з меню (тумблер enabled, зміна
  параметра, reset) набудуть сили лише після рестарту бота.

  Щоб зміни діяли одразу, передайте в конструктор strategy_manager
  (StrategyManager з strategies/manager.py) — SettingsMenu буде викликати
  його set_enabled()/apply_params() одразу після запису в БД, і він
  синхронізує live-об'єкти стратегій без рестарту. Якщо strategy_manager
  не передано, SettingsMenu продовжує працювати як раніше (зміни лише в
  БД, застосовуються після рестарту) — це збережено для зворотної
  сумісності.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

CB_PREFIX = "sm"     # settings menu — префікс усіх callback_data цього модуля
SEP = ":"            # роздільник частин callback_data
ROWS_PER_PAGE = 6    # скільки кнопок-параметрів на сторінці стратегії


def _cb(*parts: Any) -> str:
    """Складає callback_data з частин. Telegram обмежує його 64 байтами —
    тримайте strategy_name і param_key короткими (без пробілів/кирилиці)."""
    data = SEP.join(str(p) for p in parts)
    if len(data.encode()) > 64:
        logger.warning(f"callback_data занадто довгий ({len(data)} байт): {data}")
    return data

PARAM_CATALOG: Dict[str, Tuple[str, str]] = {
    # --- спільні для більшості стратегій ---
    "timeframe_seconds": (
        "Таймфрейм (сек)",
        "Розмір однієї свічки в секундах, на якій рахуються сигнали стратегії.",
    ),
    "position_size": (
        "Розмір позиції (USDT)",
        "Сума в USDT, від якої виділяється 1/10 (10% [ 100$ -> 10$ ] ) на одну угоду цієї стратегії.",
    ),
    "leverage": (
        "Плече",
        "Кредитне плече, з яким відкриваються угоди цієї стратегії.",
    ),
    "cooldown_seconds": (
        "Кулдаун між угодами (сек)",
        "Мінімальний час після закриття угоди по символу, перш ніж стратегія може знову відкрити по ньому позицію.",
    ),
    "stop_loss_percent": (
        "Стоп-лосс (% ROI)",
        "На скільки відсотків ROI (з урахуванням плеча) ціна може піти проти позиції, перш ніж вона закриється по стопу.",
    ),
    "stop_loss_buffer_percent": (
        "Стоп-лосс (% ROI)",
        "На скільки відсотків ROI (з урахуванням плеча) ціна може піти проти позиції, перш ніж вона закриється по стопу.",
    ),
    "take_profit_percent": (
        "Тейк-профіт (% ROI)",
        "На скільки відсотків ROI (з урахуванням плеча) має вирости прибуток, щоб позиція закрилась по тейку.",
    ),
    "take_profit_levels": (
        "Рівні тейк-профіту",
        "Список рівнів фіксації прибутку (ціна у % та частка позиції для закриття на кожному рівні). "
        "Редагування списку через це меню недоступне — змінюйте в конфігурації бота.",
    ),

    # --- RejectionBlockStrategy (price-action патерн) ---
    "wick_to_body_ratio": (
        "Тінь / тіло",
        "У скільки разів домінантна тінь свічки має бути більшою за її тіло, щоб свічка вважалась патерном.",
    ),
    "min_wick_ratio": (
        "Мін. частка домінантної тіні",
        "Яку мінімальну частку від усього діапазону свічки має займати домінантна тінь (0.6 = 60%).",
    ),
    "opposite_wick_max_ratio": (
        "Макс. частка протилежної тіні",
        "Наскільки великою може бути протилежна (не домінантна) тінь відносно домінантної, щоб патерн ще зарахувався.",
    ),
    "overlap_tolerance_percent": (
        "Допуск перекриття тіней (%)",
        "Наскільки (у % від ціни) тінь поточної свічки може не доходити до тіні попередньої й все одно вважатись 'перекриттям'.",
    ),
    "min_body_percent": (
        "Мін. розмір тіла свічки (%)",
        "Мінімальний розмір тіла свічки у % від ціни — відсіює свічки-голки на порожньому об'ємі.",
    ),

    # --- SimpleMovingAverageStrategy ---
    "sma_period": (
        "Період SMA",
        "Кількість свічок, за якими рахується просте ковзне середнє (SMA).",
    ),
    "threshold_percent": (
        "Поріг сигналу (%)",
        "Наскільки далеко (у %) ціна має відхилитись від SMA, щоб це вважалось сигналом на вхід.",
    ),
    "confirmation_candles": (
        "Свічок підтвердження",
        "Скільки послідовних свічок в один бік потрібно для підтвердження сигналу, перш ніж відкрити позицію.",
    ),
    "atr_period": (
        "Період ATR",
        "Кількість свічок, за якими рахується ATR (середній істинний діапазон) для розрахунку ризику.",
    ),
    "use_atr_risk": (
        "Ризик за ATR",
        "Якщо увімкнено — стоп і тейки рахуються від ATR (волатильності), а не від фіксованих відсотків.",
    ),
    "atr_stop_multiplier": (
        "Множник ATR для стопу",
        "На скільки ATR (помножених на це число) стоп-лосс віддаляється від ціни входу.",
    ),
    "atr_tp_multipliers": (
        "Множники ATR для тейків",
        "Список множників ATR для кожного рівня тейк-профіту (напр. [2.0, 3.5] — другий тейк далі першого). "
        "Редагування списку через це меню недоступне — змінюйте в конфігурації бота.",
    ),
    "tp_close_percents": (
        "% закриття по рівнях тейку",
        "Яку частку позиції закривати на кожному рівні тейк-профіту (напр. [50, 50] — по половині на кожному). "
        "Редагування списку через це меню недоступне — змінюйте в конфігурації бота.",
    ),
}


def _prettify_key(key: str) -> str:
    """Fallback-перетворення технічного ключа параметра на людську назву,
    коли його немає в PARAM_CATALOG: "atr_stop_multiplier" -> "Atr stop multiplier".
    Не такий гарний, як ручний запис у каталозі, але кращий за сирий ключ
    і не дає меню зламатись на новому/незадокументованому параметрі."""
    return key.replace("_", " ").strip().capitalize() or key


def _catalog_lookup(key: str) -> Tuple[str, Optional[str]]:
    """Повертає (людська назва, опис) для ключа параметра: з PARAM_CATALOG,
    якщо він там є, інакше (_prettify_key(key), None)."""
    entry = PARAM_CATALOG.get(key)
    if entry:
        return entry
    return _prettify_key(key), None


# ---------------------------------------------------------------------------
# Декларативний опис параметрів (для ручного оверрайду)
# ---------------------------------------------------------------------------

@dataclass
class ParamSpec:
    """Опис одного параметра стратегії для меню."""
    key: str
    label: str
    kind: str  # "bool" | "choice" | "number" | "text" | "readonly"
    choices: Optional[Sequence[Any]] = None            # для kind="choice"
    min_value: Optional[float] = None                   # для kind="number"
    max_value: Optional[float] = None                   # для kind="number"
    fmt: Callable[[Any], str] = str                     # як показати значення в кнопці/тексті
    parse: Callable[[str], Any] = lambda s: s           # як розпарсити текст користувача (number/text)
    description: Optional[str] = None                   # короткий опис параметра для користувача (може бути None)

    def render_value(self, value: Any) -> str:
        try:
            return self.fmt(value)
        except Exception:
            return str(value)


@dataclass
class StrategySchema:
    """Набір параметрів однієї стратегії + людська назва для заголовків."""
    title: str
    params: List[ParamSpec] = field(default_factory=list)

    def get(self, key: str) -> Optional[ParamSpec]:
        return next((p for p in self.params if p.key == key), None)


def _infer_param_spec(key: str, value: Any) -> Optional[ParamSpec]:

    label, description = _catalog_lookup(key)

    if isinstance(value, bool):
        return ParamSpec(key=key, label=label, kind="bool", description=description)
    if isinstance(value, int):
        return ParamSpec(key=key, label=label, kind="number", parse=int, description=description)
    if isinstance(value, float):
        return ParamSpec(
            key=key, label=label, kind="number", parse=float,
            fmt=lambda v: f"{float(v):.4f}", description=description,
        )
    if isinstance(value, str):
        return ParamSpec(key=key, label=label, kind="text", parse=str, description=description)
    return None


# ---------------------------------------------------------------------------
# Рушій меню
# ---------------------------------------------------------------------------

class SettingsMenu:

    def __init__(
        self,
        store: "StrategySettingsStore",
        schemas: Optional[Dict[str, StrategySchema]] = None,
        strategy_manager: Optional["StrategyManager"] = None,
    ):
        self.store = store
        self.schemas = schemas or {}
        self.strategy_manager = strategy_manager
        # chat_id -> (strategy_name, param_key, message_id меню, яке треба оновити після вводу)
        self._awaiting: Dict[int, Tuple[str, str, int]] = {}

    # ---------- реєстрація ----------

    def register(self, application: Application) -> None:
        application.add_handler(CallbackQueryHandler(self._on_callback, pattern=f"^{CB_PREFIX}{SEP}"))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text_input))

    def root_callback(self) -> str:
        return _cb(CB_PREFIX, "root", 0)

    # ---------- побудова схеми (ручна або авто) ----------

    def _get_schema(self, strategy_name: str) -> StrategySchema:
        """Повертає схему для стратегії: якщо для неї є ручний оверрайд - бере
        його; інакше будує схему на льоту з поточних значень у БД, підставляючи
        людські назви/описи з PARAM_CATALOG."""
        if strategy_name in self.schemas:
            return self.schemas[strategy_name]

        current = self.store.get_params(strategy_name) or {}
        params: List[ParamSpec] = []
        for key, value in current.items():
            spec = _infer_param_spec(key, value)
            if spec is None:
               
                label, description = _catalog_lookup(key)
                spec = ParamSpec(key=key, label=label, kind="readonly", fmt=lambda v: str(v), description=description)
            params.append(spec)

        return StrategySchema(title=strategy_name, params=params)

    # ---------- обробка натискань кнопок ----------

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        try:
            await query.answer()
        except Exception:
            pass  # callback застарів — не критично

        parts = query.data.split(SEP)
        action = parts[1]  # parts[0] == CB_PREFIX

        chat_id = update.effective_chat.id
        if action != "num":
            self._awaiting.pop(chat_id, None)

        if action == "root":
            page = int(parts[2])
            text, kb = self._render_root(page)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

        elif action == "strat":
            strategy, page = parts[2], int(parts[3])
            text, kb = self._render_strategy(strategy, page)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

        elif action == "tglstrat":
            # тумблер "стратегія увімкнена/вимкнена" цілком (не параметр,
            # окремий прапорець enabled у БД)
            strategy = parts[2]
            new_state = not self.store.is_enabled(strategy)
            if self.strategy_manager:
                # оновлює і БД, і live-інстанс одразу (без рестарту бота)
                self.strategy_manager.set_enabled(strategy, new_state)
            else:
                self.store.set_enabled(strategy, new_state)
            text, kb = self._render_strategy(strategy, 0)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

        elif action == "tgl":
            strategy, key = parts[2], parts[3]
            self._toggle_bool(strategy, key)
            text, kb = self._render_strategy(strategy, 0)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

        elif action == "sel":
            strategy, key = parts[2], parts[3]
            text, kb = self._render_choice(strategy, key)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

        elif action == "val":
            strategy, key, idx = parts[2], parts[3], int(parts[4])
            spec = self._get_schema(strategy).get(key)
            value = spec.choices[idx]
            self._set_param(strategy, key, value)
            text, kb = self._render_strategy(strategy, 0)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

        elif action == "num":
            strategy, key = parts[2], parts[3]
            await self._prompt_input(update, context, strategy, key)

        elif action == "reset":
            strategy = parts[2]
            reset_params = self.store.reset_to_default(strategy)
            if self.strategy_manager:
                self.strategy_manager.apply_params(strategy, reset_params)
            text, kb = self._render_strategy(strategy, 0)
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")

        elif action == "noop":
            pass  # декоративна кнопка (напр. "стор. 2/3" або readonly-параметр) — ігноруємо тап

        else:
            logger.warning(f"Невідома дія меню налаштувань: {query.data}")

    # ---------- рендер сторінок ----------

    def _render_root(self, page: int) -> Tuple[str, InlineKeyboardMarkup]:
        strategies = self.store.list_strategies()  # реальні стратегії, що вже засіяні через seed_defaults()
        names = [s["strategy_name"] for s in strategies]
        enabled_map = {s["strategy_name"]: s["enabled"] for s in strategies}

        if not names:
            text = (
                "<b>⚙️ Налаштування стратегій</b>\n\n"
                "📭 У базі ще немає жодної стратегії з параметрами.\n"
                "Викличте <code>strategy_settings_store.seed_defaults(...)</code> "
                "там, де створюєте реальні стратегії."
            )
            return text, InlineKeyboardMarkup([])

        page_items, total_pages, page = self._paginate(names, page)

        rows = []
        for n in page_items:
            title = self.schemas[n].title if n in self.schemas else n
            status_mark = "🟢" if enabled_map.get(n) else "🔴"
            modified_mark = " ✏️" if self.store.is_modified(n) else ""
            rows.append([InlineKeyboardButton(
                f"{status_mark} {title}{modified_mark}",
                callback_data=_cb(CB_PREFIX, "strat", n, 0)
            )])
        rows.append(self._pagination_row(("root",), page, total_pages))

        text = "<b>⚙️ Налаштування стратегій</b>\n🟢 — увімкнена, 🔴 — вимкнена\nОберіть стратегію:"
        return text, InlineKeyboardMarkup(rows)

    def _render_strategy(self, strategy: str, page: int) -> Tuple[str, InlineKeyboardMarkup]:
        schema = self._get_schema(strategy)
        current = self.store.get_params(strategy) or {}
        is_enabled = self.store.is_enabled(strategy)

        page_items, total_pages, page = self._paginate(schema.params, page, size=ROWS_PER_PAGE)

        rows: List[List[InlineKeyboardButton]] = []

        toggle_label = "🟢 Стратегія увімкнена (натисніть, щоб вимкнути)" if is_enabled \
            else "🔴 Стратегія вимкнена (натисніть, щоб увімкнути)"
        rows.append([InlineKeyboardButton(toggle_label, callback_data=_cb(CB_PREFIX, "tglstrat", strategy))])

        desc_lines: List[str] = []
        for spec in page_items:
            value = current.get(spec.key)
            if spec.kind == "bool":
                label = f"{'✅' if value else '⬜️'} {spec.label}"
                cb = _cb(CB_PREFIX, "tgl", strategy, spec.key)
            elif spec.kind == "choice":
                label = f"{spec.label}: {spec.render_value(value)}"
                cb = _cb(CB_PREFIX, "sel", strategy, spec.key)
            elif spec.kind == "readonly":
                label = f"👁 {spec.label}: {spec.render_value(value)}"
                cb = _cb(CB_PREFIX, "noop")
            else:  # "number" / "text"
                label = f"{spec.label}: {spec.render_value(value)}"
                cb = _cb(CB_PREFIX, "num", strategy, spec.key)
            rows.append([InlineKeyboardButton(label, callback_data=cb)])
            if spec.description:
                desc_lines.append(f"• <b>{spec.label}</b> — {spec.description}")

        if not schema.params:
            rows.append([InlineKeyboardButton("📭 Немає параметрів", callback_data=_cb(CB_PREFIX, "noop"))])

        rows.append(self._pagination_row(("strat", strategy), page, total_pages))
        rows.append([InlineKeyboardButton("🔄 Скинути до заводських", callback_data=_cb(CB_PREFIX, "reset", strategy))])
        rows.append([InlineKeyboardButton("⬅️ До списку стратегій", callback_data=_cb(CB_PREFIX, "root", 0))])

        status_mark = "🟢" if is_enabled else "🔴"
        modified_mark = " ✏️" if self.store.is_modified(strategy) else ""
        desc_block = ("\n\n" + "\n".join(desc_lines)) if desc_lines else ""
        text = (
            f"<b>⚙️ {status_mark} {schema.title}{modified_mark}</b>\n"
            f"Сторінка {page + 1}/{total_pages}"
            f"{desc_block}"
        )
        return text, InlineKeyboardMarkup(rows)

    def _render_choice(self, strategy: str, key: str) -> Tuple[str, InlineKeyboardMarkup]:
        schema = self._get_schema(strategy)
        spec = schema.get(key)
        current = self.store.get_params(strategy) or {}
        current_value = current.get(key)

        rows = []
        for idx, choice in enumerate(spec.choices):
            mark = "✅ " if choice == current_value else ""
            rows.append([InlineKeyboardButton(f"{mark}{choice}", callback_data=_cb(CB_PREFIX, "val", strategy, key, idx))])

        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=_cb(CB_PREFIX, "strat", strategy, 0))])
        desc_line = f"<i>{spec.description}</i>\n\n" if spec.description else ""
        text = f"<b>{spec.label}</b>\n{desc_line}Оберіть значення:"
        return text, InlineKeyboardMarkup(rows)

    # ---------- пагінація (спільна для будь-якого списку) ----------

    @staticmethod
    def _paginate(items: Sequence[Any], page: int, size: int = ROWS_PER_PAGE):
        total_pages = max(1, (len(items) + size - 1) // size)
        page = max(0, min(page, total_pages - 1))
        start = page * size
        return items[start:start + size], total_pages, page

    @staticmethod
    def _pagination_row(target_action_prefix: Tuple[str, ...], page: int, total_pages: int) -> List[InlineKeyboardButton]:
        if total_pages <= 1:
            return [InlineKeyboardButton(f"стор. {page + 1}/{total_pages}", callback_data=_cb(CB_PREFIX, "noop"))]

        row = []
        if page > 0:
            row.append(InlineKeyboardButton("⬅️", callback_data=_cb(CB_PREFIX, *target_action_prefix, page - 1)))
        row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=_cb(CB_PREFIX, "noop")))
        if page < total_pages - 1:
            row.append(InlineKeyboardButton("➡️", callback_data=_cb(CB_PREFIX, *target_action_prefix, page + 1)))
        return row

    # ---------- зміна значень ----------

    def _toggle_bool(self, strategy: str, key: str) -> None:
        current = self.store.get_params(strategy) or {}
        current[key] = not bool(current.get(key))
        self.store.update_params(strategy, current)
        if self.strategy_manager:
            self.strategy_manager.apply_params(strategy, current)

    def _set_param(self, strategy: str, key: str, value: Any) -> None:
        current = self.store.get_params(strategy) or {}
        current[key] = value
        self.store.update_params(strategy, current)
        if self.strategy_manager:
            self.strategy_manager.apply_params(strategy, current)

    async def _prompt_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE, strategy: str, key: str) -> None:
        query = update.callback_query
        spec = self._get_schema(strategy).get(key)
        current = self.store.get_params(strategy) or {}

        rng = ""
        if spec.min_value is not None or spec.max_value is not None:
            rng = f" (діапазон: {spec.min_value} … {spec.max_value})"

        desc_line = f"<i>{spec.description}</i>\n" if spec.description else ""
        text = (
            f"✏️ Введіть нове значення для <b>{spec.label}</b>{rng}\n"
            f"{desc_line}"
            f"Поточне: <code>{spec.render_value(current.get(key))}</code>\n\n"
            f"Надішліть значення наступним повідомленням."
        )
        back_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Назад", callback_data=_cb(CB_PREFIX, "strat", strategy, 0))
        ]])
        await query.edit_message_text(text, reply_markup=back_kb, parse_mode="HTML")

        chat_id = update.effective_chat.id
        self._awaiting[chat_id] = (strategy, key, query.message.message_id)

    async def _on_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        pending = self._awaiting.get(chat_id)
        if not pending:
            return  # ми не чекаємо введення від цього юзера — нехай обробляють інші хендлери

        strategy, key, menu_message_id = pending
        spec = self._get_schema(strategy).get(key)
        raw = update.message.text.strip()

        try:
            value = spec.parse(raw)
            if spec.min_value is not None and value < spec.min_value:
                raise ValueError(f"мінімум {spec.min_value}")
            if spec.max_value is not None and value > spec.max_value:
                raise ValueError(f"максимум {spec.max_value}")
        except Exception as e:
            await update.message.reply_text(f"❌ Невірне значення: {e}. Спробуйте ще раз.")
            return

        self._set_param(strategy, key, value)
        del self._awaiting[chat_id]

        text, kb = self._render_strategy(strategy, 0)
        try:
            await context.bot.edit_message_text(
                text, chat_id=chat_id, message_id=menu_message_id, reply_markup=kb, parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Не вдалося оновити повідомлення меню: {e}")
            await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

        await update.message.reply_text(f"✅ Збережено: {spec.label} = {spec.render_value(value)}")