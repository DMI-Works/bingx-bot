"""
Каталог людських назв/описів параметрів стратегій.

Раніше жив тільки всередині core/telegram/settings_menu.py (Telegram-меню,
яке більше не використовується — керування перенесене в мініапп). Винесений
сюди, щоб webapp/backend/api.py (FastAPI, без залежності від
python-telegram-bot) міг використовувати ті самі лейбли/описи для вкладки
«Настройки» в мініаппі.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

PARAM_CATALOG: Dict[str, Tuple[str, str]] = {
    # --- TsMomentumStrategy (дневной time-series momentum) ---
    "leverage": (
        "Плече",
        "Кредитне плече, з яким відкриваються угоди цієї стратегії.",
    ),
    "risk_per_trade_percent": (
        "Ризик на угоду (% від equity)",
        "Скільки % equity втрачається, якщо спрацює початковий стоп. Розмір позиції = ризик / дистанція до стопу.",
    ),
    "atr_period": ("Період ATR", "ATR робочого ТФ для початкового стопу і супроводу."),
    "min_quote_volume_24h_usdt": (
        "Мін. 24h оборот монети (USDT)",
        "Тонкі монети не торгуються: на них стоп проскакує. 0 — вимкнути фільтр.",
    ),
    "excluded_symbol_prefixes": (
        "Виключені префікси символів",
        "Через кому. Не-крипто контракти BingX: NCFX (форекс), NCCO (сировина), NCSK (акції), NCSI (індекси).",
    ),
    "max_signal_age_seconds": (
        "Макс. вік сигналу (с)",
        "Скільки секунд після закриття свічки ще дозволено входити. Далі сигнал відкидається.",
    ),
    "recheck_interval_minutes": (
        "Повторна перевірка монет (хв)",
        "Як часто протягом дня шукати вхід по монетах без позиції (сигнал той самий, денний). "
        "Дозволяє знову увійти після стопу, не чекаючи наступної доби. 0 — лише раз на добу.",
    ),
    "reentry_pause_minutes": (
        "Пауза після збитку або безубитку (хв)",
        "Скільки хвилин не входити по монеті після закриття в мінус або по безубитку. "
        "Після прибуткового закриття паузи немає. 0 — без паузи.",
    ),
    "trail_breakeven_roi": (
        "Фіксація: безубиток (% ROI)",
        "ROI позиції ≥ N% → стоп переноситься на рівень «стоп безубитку». 0 — вимкнено.",
    ),
    "trail_breakeven_stop_roi": (
        "Фіксація: стоп безубитку (% ROI)",
        "Куди ставиться стоп, коли спрацював безубиток, у % ROI від входу. Має покривати комісії "
        "(~1% ROI при 10x) і проковзування. Менше за «безубиток».",
    ),
    "trail_start_roi": (
        "Фіксація: старт трейлінгу (% ROI)",
        "ROI ≥ N% → стоп тягнеться за піком ROI з відступом. 0 — вимкнено.",
    ),
    "trail_gap_roi": (
        "Фіксація: відступ від піку (% ROI)",
        "Стоп = пік ROI мінус N%. Напр. пік +50%, відступ 20 → стоп на +30% ROI. Має бути менше за старт.",
    ),
    "timeframe": (
        "Таймфрейм",
        "ТФ, на якому рахується моментум і сверка позиції (за замовчуванням 1d — денний).",
    ),
    "lookback_days": (
        "Вікно моментуму (днів)",
        "За скільки останніх періодів рахується доходність. Знак доходності = напрям угоди (LONG/SHORT). Найкраще за бектестом — 60.",
    ),
    "mode": (
        "Режим",
        "long_short — лонг при зростанні, шорт при падінні; long_flat — лише лонг, інакше поза ринком (без шортів).",
    ),
    "disaster_stop_atr": (
        "Дизастер-стоп (× ATR)",
        "Широкий аварійний стоп (страховка від гепа), а не робочий вихід: вихід — по розвороту сигналу. Він же задає розмір позиції (vol-target).",
    ),
}


def prettify_key(key: str) -> str:
    """Fallback-перетворення технічного ключа параметра на людську назву,
    коли його немає в PARAM_CATALOG: "atr_stop_multiplier" -> "Atr stop multiplier"."""
    return key.replace("_", " ").strip().capitalize() or key


def catalog_lookup(key: str) -> Tuple[str, Optional[str]]:
    """Повертає (людська назва, опис) для ключа параметра: з PARAM_CATALOG,
    якщо він там є, інакше (prettify_key(key), None)."""
    entry = PARAM_CATALOG.get(key)
    if entry:
        return entry
    return prettify_key(key), None


def infer_param_kind(value: Any) -> Optional[str]:
    """Визначає тип параметра ("bool" | "number" | "text" | "list") за поточним
    значенням у БД — той самий принцип, що й _infer_param_spec у
    core/telegram/settings_menu.py, але без прив'язки до ParamSpec/Telegram."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, (list, tuple)):
        return "list"
    return None


def infer_item_kind(value: Any) -> str:
    """Для параметра-масиву повертає тип ЕЛЕМЕНТІВ ("bool" | "number" | "text")
    за першим елементом. Порожній масив / невідомий тип -> "number"
    (на практиці масиви в налаштуваннях — це списки порогів)."""
    if isinstance(value, (list, tuple)):
        for item in value:
            kind = infer_param_kind(item)
            if kind in ("bool", "number", "text"):
                return kind
    return "number"


def _coerce_scalar(kind: str, value: Any) -> Any:
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("Ожидается true/false")
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Ожидается число")
        return value
    if kind == "text":
        if not isinstance(value, str):
            raise ValueError("Ожидается строка")
        return value
    return value


def coerce_param_value(reference: Any, value: Any) -> Any:
    """Проверяет, что приходящее из мініаппу значение имеет тот же тип, что и
    текущее значение параметра (reference), и возвращает его.
    Для массивов проверяется каждый элемент по типу элементов reference.
    Бросает ValueError с человекочитаемым сообщением. Если тип reference
    неизвестен (None и т.п.) — значение пропускается как есть."""
    kind = infer_param_kind(reference)
    if kind is None:
        return value
    if kind == "list":
        if not isinstance(value, list):
            raise ValueError("Ожидается список значений")
        item_kind = infer_item_kind(reference)
        return [_coerce_scalar(item_kind, item) for item in value]
    return _coerce_scalar(kind, value)
