"""
Тексти Telegram-сповіщень (HTML) — чисті функції від даних події і мови
користувача (lang: en/ru/uk, None — англійська; каталог у core/common/i18n.py).
TelegramBot лише вирішує, коли і що надсилати.
"""

from datetime import datetime
from typing import Iterable, List, Optional

from ..common.formatting import price_decimals
from ..common.i18n import t
from ..common.position_math import parse_leverage, roi_percent

_STOP_STAGE_KEYS = {'breakeven': 'stage_breakeven', 'trailing': 'stage_trailing'}


def _stage_label(stage, lang) -> str:
    return t(lang, _STOP_STAGE_KEYS.get(stage, 'stage_moved'))


def side_emoji(side: str) -> str:
    return "🟢" if side == "LONG" else "🔴"


def position_opened_caption(data: dict, lang=None) -> str:
    tp_levels = data.get('take_profit_levels') or []
    lines = [t(lang, 'position_opened'), ""]

    if tp_levels:
        lines.append(t(lang, 'take_profit'))
        for i, tp in enumerate(tp_levels, start=1):
            prefix = "└" if i == len(tp_levels) else "  ├"
            lines.append(f"{prefix} {i}: 💰{tp['price']:.6f} ({tp.get('close_percent', 0)}%)")

    if data.get('stop_loss_price'):
        lines.append(f"{t(lang, 'stop')}: ${data['stop_loss_price']:.6f}")

    lines += ["", f"[INFO]: {data.get('positions_info_message', 'N/A')}"]
    return "\n".join(lines)


def take_profit_summary(tp_levels: Optional[list], lang=None) -> Optional[str]:
    """Короткий підпис TP для картки відкриття."""
    if not tp_levels:
        return None
    if len(tp_levels) > 1:
        return t(lang, 'tp_levels', n=len(tp_levels))
    return f"{tp_levels[0]['price']:.6f}"


def info_caption(data: dict) -> str:
    return f"[INFO]: {data.get('positions_info_message', 'N/A')}"


def stop_loss_triggered(data: dict, lang=None) -> str:
    return (
        f"{t(lang, 'sl_triggered')}\n\n"
        f"{t(lang, 'symbol')}: {data.get('symbol')}\n"
        f"{t(lang, 'price')}: ${data.get('price', 0):.6f}\n"
        f"[INFO]: {data.get('positions_info_message')}"
    )


def take_profit_triggered(data: dict, lang=None) -> str:
    return (
        f"{t(lang, 'tp_triggered')}\n\n"
        f"{t(lang, 'symbol')}: {data.get('symbol')}\n"
        f"{t(lang, 'level')}: {data.get('level', 1)}\n"
        f"{t(lang, 'price')}: ${data.get('price', 0):.6f}\n"
        f"[INFO]: {data.get('positions_info_message')}"
    )


def stop_loss_moved(data: dict, lang=None) -> Optional[str]:
    """None — якщо в події бракує цін (тоді шлемо скорочений варіант)."""
    symbol = data.get('symbol', 'N/A')
    side = data.get('side', 'N/A')
    old_price = data.get('old_stop_price')
    new_price = data.get('new_stop_price')
    entry_price = data.get('entry_price')
    stage_label = _stage_label(data.get('stage'), lang)

    if old_price is None or new_price is None or entry_price is None:
        return None

    leverage = parse_leverage(data.get('leverage') or 1) or 1.0
    decimals = price_decimals(entry_price)

    text = (
        f"{stage_label}\n\n"
        f"{side_emoji(side)} <b>{symbol}</b> {side}\n"
        f"├ {t(lang, 'entry')}: <code>${entry_price:.{decimals}f}</code>\n"
        f"├ {t(lang, 'old_stop')}: <code>{roi_percent(entry_price, side, old_price, leverage):+.2f}% ROI</code>\n"
        f"└ {t(lang, 'new_stop')}: <code>{roi_percent(entry_price, side, new_price, leverage):+.2f}% ROI</code>"
    )
    if data.get('strategy'):
        text += f"\n[INFO]: {t(lang, 'strategy')}: {data['strategy']}"
    return text


def stop_loss_moved_short(data: dict, lang=None) -> str:
    stage_label = _stage_label(data.get('stage'), lang)
    side = data.get('side', 'N/A')
    return f"{stage_label}\n\n{side_emoji(side)} <b>{data.get('symbol', 'N/A')}</b> {side}"


def symbol_paused(data: dict, lang=None) -> str:
    return t(
        lang, 'symbol_paused',
        symbol=data.get('symbol', 'N/A'),
        losses=data.get('consecutive_losses'),
        limit=data.get('max_consecutive_losses'),
        hours=data.get('pause_hours'),
    )


def symbols_rotated(data: dict, lang=None) -> str:
    added = data.get('added') or []
    removed = data.get('removed') or []
    total = data.get('total_active')

    lines = [t(lang, 'rotation')]
    if added:
        lines.append(f"├ {t(lang, 'rotation_added')}: " + ", ".join(f"<code>{s}</code>" for s in added))
    if removed:
        lines.append(f"├ {t(lang, 'rotation_removed')}: " + ", ".join(f"<code>{s}</code>" for s in removed))
    if total is not None:
        lines.append(f"└ {t(lang, 'rotation_total')}: {total}")
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
    lang=None,
) -> str:
    lines = [
        t(lang, 'startup_title'),
        "",
        f"{t(lang, 'startup_mode')}: {t(lang, 'mode_test' if testnet else 'mode_live')}",
        f"{t(lang, 'startup_active')}: {len(enabled_strategies)}/{total_strategies}",
    ]
    if enabled_strategies:
        lines += ["", t(lang, 'startup_list')] + [f"  • {name}" for name in enabled_strategies]
    if balance is not None:
        lines += ["", f"💰 {t(lang, 'balance')}: ${balance:.2f}"]
    lines += ["", f"🕐 {now.strftime('%d.%m.%Y %H:%M:%S')}"]
    return "\n".join(lines)


def enabled_strategy_names(strategies: Iterable) -> List[str]:
    return [s.name for s in strategies if s.is_enabled()]


def start_text(lang=None) -> str:
    """Привітання на /start: що це і як почати (детальна інструкція — у Mini App)."""
    return t(lang, 'start_text')
