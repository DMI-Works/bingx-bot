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
    # --- спільні / WallBreakoutStrategy (єдина робоча стратегія в реєстрі) ---
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
        "На скільки відсотків ROI (з урахуванням плеча) ціна може піти проти позиції, перш ніж вона закриється по стопу. "
        "Далі позицію веде TrailingStop — окремого тейк-профіту в цієї стратегії немає.",
    ),
    "trail_levels_percent": (
        "Рівні трейлінг-стопу (% ROI)",
        "Пороги прибутку (% ROI), на яких TrailingStop переставляє стоп-лосс у бік прибутку. "
        "Вводяться через кому, напр.: 10, 16, 32, 64. Порядок не важливий, значення ≤ 0 ігноруються.",
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
