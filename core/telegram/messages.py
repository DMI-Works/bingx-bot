"""
Тексти Telegram-сповіщень (HTML) — чисті функції від даних події.
TelegramBot лише вирішує, коли і що надсилати.
"""

from datetime import datetime
from typing import Iterable, List, Optional

from ..common.formatting import price_decimals
from ..common.position_math import parse_leverage, roi_percent

_STOP_STAGE_LABELS = {
    'breakeven': '🟡 Перенесено в беззбиток',
    'trailing': '🟢 Підтягнуто трейлінгом',
}


def side_emoji(side: str) -> str:
    return "🟢" if side == "LONG" else "🔴"


def position_opened_caption(data: dict) -> str:
    tp_levels = data.get('take_profit_levels') or []
    lines = ["✅ <b>Позицію відкрито</b>", ""]

    if tp_levels:
        lines.append("Тейк:")
        for i, tp in enumerate(tp_levels, start=1):
            prefix = "└" if i == len(tp_levels) else "  ├"
            lines.append(f"{prefix} {i}: 💰{tp['price']:.6f} ({tp.get('close_percent', 0)}%)")

    if data.get('stop_loss_price'):
        lines.append(f"Стоп: ${data['stop_loss_price']:.6f}")

    lines += ["", f"[INFO]: {data.get('positions_info_message', 'N/A')}"]
    return "\n".join(lines)


def take_profit_summary(tp_levels: Optional[list]) -> Optional[str]:
    """Короткий підпис TP для картки відкриття."""
    if not tp_levels:
        return None
    if len(tp_levels) > 1:
        return f"{len(tp_levels)} рівні"
    return f"{tp_levels[0]['price']:.6f}"


def info_caption(data: dict) -> str:
    return f"[INFO]: {data.get('positions_info_message', 'N/A')}"


def stop_loss_triggered(data: dict) -> str:
    return (
        "🛑 <b>Спрацював стоп-лосс</b>\n\n"
        f"Символ: {data.get('symbol')}\n"
        f"Ціна: ${data.get('price', 0):.6f}\n"
        f"[INFO]: {data.get('positions_info_message')}"
    )


def take_profit_triggered(data: dict) -> str:
    return (
        "🎯 <b>Досягнуто тейк-профіт</b>\n\n"
        f"Символ: {data.get('symbol')}\n"
        f"Рівень: {data.get('level', 1)}\n"
        f"Ціна: ${data.get('price', 0):.6f}\n"
        f"[INFO]: {data.get('positions_info_message')}"
    )


def stop_loss_moved(data: dict) -> Optional[str]:
    """None — якщо в події бракує цін (тоді шлемо скорочений варіант)."""
    symbol = data.get('symbol', 'N/A')
    side = data.get('side', 'N/A')
    old_price = data.get('old_stop_price')
    new_price = data.get('new_stop_price')
    entry_price = data.get('entry_price')
    stage_label = _STOP_STAGE_LABELS.get(data.get('stage'), '🔄 Стоп перенесено')

    if old_price is None or new_price is None or entry_price is None:
        return None

    leverage = parse_leverage(data.get('leverage') or 1) or 1.0
    decimals = price_decimals(entry_price)

    text = (
        f"{stage_label}\n\n"
        f"{side_emoji(side)} <b>{symbol}</b> {side}\n"
        f"├ Вхід: <code>${entry_price:.{decimals}f}</code>\n"
        f"├ Старий стоп: <code>{roi_percent(entry_price, side, old_price, leverage):+.2f}% ROI</code>\n"
        f"└ Новий стоп: <code>{roi_percent(entry_price, side, new_price, leverage):+.2f}% ROI</code>"
    )
    if data.get('strategy'):
        text += f"\n[INFO]: Стратегія: {data['strategy']}"
    return text


def stop_loss_moved_short(data: dict) -> str:
    stage_label = _STOP_STAGE_LABELS.get(data.get('stage'), '🔄 Стоп перенесено')
    side = data.get('side', 'N/A')
    return f"{stage_label}\n\n{side_emoji(side)} <b>{data.get('symbol', 'N/A')}</b> {side}"


def symbol_blacklisted(data: dict) -> str:
    symbol = data.get('symbol', 'N/A')
    return (
        f"🚫 <b>{symbol} прибрано з торгівлі</b>\n\n"
        f"{data.get('consecutive_losses')} збиткових угод поспіль по {symbol} "
        f"(ліміт: {data.get('max_consecutive_losses')}).\n"
        "Монету додано до чорного списку й відписано від потоку даних — нові позиції по ній "
        "не відкриваються, доки ти не повернеш її вручну (міні-апп → Монети → чорний список).\n"
        "По інших символах бот торгує як звичайно."
    )


def symbols_rotated(data: dict) -> str:
    added = data.get('added') or []
    removed = data.get('removed') or []
    total = data.get('total_active')

    lines = ["🔄 <b>Ротація монет</b>"]
    if added:
        lines.append("├ Додано: " + ", ".join(f"<code>{s}</code>" for s in added))
    if removed:
        lines.append("├ Прибрано: " + ", ".join(f"<code>{s}</code>" for s in removed))
    if total is not None:
        lines.append(f"└ Активних символів: {total}")
    return "\n".join(lines)


def error(data: dict, title: str, default: str) -> str:
    context = data.get('context')
    err = data.get('error', default)
    text = f"{title}: {context or err}"
    if context and err:
        text += f"\n<code>{err}</code>"
    return text


def startup(
    *,
    testnet: bool,
    enabled_strategies: List[str],
    total_strategies: int,
    balance: Optional[float],
    now: datetime,
) -> str:
    lines = [
        "🚀 <b>Бот запущено та готовий до роботи</b>",
        "",
        f"Режим: {'🧪 Тестовий' if testnet else '🔴 LIVE (реальні кошти)'}",
        f"Стратегій активно: {len(enabled_strategies)}/{total_strategies}",
    ]
    if enabled_strategies:
        lines += ["", "Активні стратегії:"] + [f"  • {name}" for name in enabled_strategies]
    if balance is not None:
        lines += ["", f"💰 Баланс: ${balance:.2f}"]
    lines += ["", f"🕐 {now.strftime('%d.%m.%Y %H:%M:%S')}"]
    return "\n".join(lines)


def enabled_strategy_names(strategies: Iterable) -> List[str]:
    return [s.name for s in strategies if s.is_enabled()]
