"""
Каталог людських назв/описів параметрів стратегій.

Раніше жив тільки всередині core/telegram/settings_menu.py (Telegram-меню,
яке більше не використовується — керування перенесене в мініапп). Винесений
сюди, щоб webapp/backend/api.py (FastAPI, без залежності від
python-telegram-bot) міг використовувати ті самі лейбли/описи для вкладки
«Настройки» в мініаппі.
"""

from __future__ import annotations

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
    """Визначає тип параметра ("bool" | "number" | "text") за поточним
    значенням у БД — той самий принцип, що й _infer_param_spec у
    core/telegram/settings_menu.py, але без прив'язки до ParamSpec/Telegram."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "text"
    return None
