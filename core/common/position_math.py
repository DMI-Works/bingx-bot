"""
Математика позиції: напрямок, ROI, маржа, net PnL.

Єдине місце для формул, які раніше були скопійовані в SimpleTrader,
TrailingStopManager, ExcursionTracker, TelegramBot і аналітиці БД.
Чисті функції — без I/O і без стану.
"""

from typing import Any, Optional


def position_key(symbol: str, side: str) -> str:
    """Ключ позиції в SimpleTrader.open_positions (і в станах трекерів)."""
    return f"{symbol}_{side}"


def side_sign(side: str) -> float:
    """+1 для LONG, -1 для SHORT: множник, який перетворює рух ціни на профіт."""
    return 1.0 if side == 'LONG' else -1.0


def entry_order_side(side: str) -> str:
    """Сторона ордера, що ВІДКРИВАЄ позицію."""
    return 'BUY' if side == 'LONG' else 'SELL'


def close_order_side(side: str) -> str:
    """Сторона ордера, що ЗАКРИВАЄ позицію (SL/TP/ручне закриття)."""
    return 'SELL' if side == 'LONG' else 'BUY'


def price_fraction(entry_price: float, side: str, price: float) -> float:
    """Рух ціни від входу в частках, зі знаком «в бік позиції» (> 0 — профіт)."""
    return side_sign(side) * (price - entry_price) / entry_price


def roi_percent(entry_price: float, side: str, price: float, leverage: float) -> float:
    """ROI у % (як показує біржа): рух ціни × плече."""
    return price_fraction(entry_price, side, price) * leverage * 100.0


def parse_leverage(value: Any) -> Optional[float]:
    """float(плече) або None, якщо значення некоректне або ≤ 0.

    Плече може прилетіти з конфігу як щось не-числове (напр. кортеж через
    зайву кому) — краще явно отримати None, ніж TypeError посеред закриття."""
    try:
        leverage = float(value)
    except (TypeError, ValueError):
        return None
    return leverage if leverage > 0 else None


def calc_margin_usdt(position: dict) -> Optional[float]:
    """Скільки USDT реально вкладено (маржа) = entry_price * quantity / leverage."""
    entry_price = position.get('entry_price')
    quantity = position.get('quantity')
    if not entry_price or not quantity or entry_price <= 0 or quantity <= 0:
        return None
    leverage = parse_leverage(position.get('leverage') or 1)
    if leverage is None:
        return None
    return (entry_price * quantity) / leverage


def calc_roe_percent(position: dict, realized_pnl: float) -> Optional[float]:
    """ROE% = PnL відносно маржі (так само, як біржа рахує ROE% в інтерфейсі).
    None, якщо бракує даних (замість того щоб мовчки підставити 0)."""
    margin = calc_margin_usdt(position)
    if not margin or margin <= 0:
        return None
    return (realized_pnl / margin) * 100


def calc_net_pnl(
    realized_pnl: float,
    commission_open: float = 0.0,
    commission_close: float = 0.0,
    funding_fee: float = 0.0,
) -> float:
    """
    ЄДИНА точка правди для net_pnl у всьому боті (ТЗ TZ_fix_pnl_accounting.md,
    задача 1). Угода про знак: комісії та funding зберігаються ТАКИМИ Ж,
    якими їх віддає біржа — ВІД'ЄМНИМИ, коли це списання. Тому формула — СУМА:

        net_pnl = realized_pnl + commission_open + commission_close + funding_fee

    Раніше було `realized_pnl - commission_total` при вже від'ємному
    commission_total — комісія ПРИБАВЛЯЛАСЬ до прибутку. Приклад зі звірки з
    біржею: realized_pnl=0.384118, комісії по -0.0498, funding 0 ->
    старий код 0.4837 (завищено), новий 0.2845 (вірно).

    funding_fee за замовчуванням 0.0 — SimpleTrader поки не відстежує funding
    по кожній позиції; місце у формулі залишено наперед.
    """
    return (
        (realized_pnl or 0.0)
        + (commission_open or 0.0)
        + (commission_close or 0.0)
        + (funding_fee or 0.0)
    )


def rescale_to_fill(
    stop_loss_price: Optional[float],
    take_profit_levels: Optional[list],
    reference_price: Optional[float],
    entry_price: Optional[float],
):
    """Переносить SL/TP, пораховані стратегією від reference_price, на
    фактичну ціну виконання ринкового ордера (пропорційно). Якщо даних
    немає — повертає рівні без змін. Повертає (stop_loss_price, take_profit_levels, scale|None)."""
    if not (reference_price and entry_price and reference_price > 0):
        return stop_loss_price, take_profit_levels, None
    scale = entry_price / reference_price
    if stop_loss_price:
        stop_loss_price = stop_loss_price * scale
    if take_profit_levels:
        take_profit_levels = [{**lvl, 'price': lvl['price'] * scale} for lvl in take_profit_levels]
    return stop_loss_price, take_profit_levels, scale
