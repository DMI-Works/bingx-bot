"""
Чиста (без I/O) агрегація закритих угод для /api/stats і /api/trades.
"""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Tuple

from core.common.market_data import to_float
from core.common.position_metadata import load_metadata

PERIOD_TO_DAYS = {"1D": 1, "1W": 7, "1M": 30, "ALL": None}


def period_cutoff(period: str) -> Optional[datetime]:
    """Єдина точка правди для пігулок періоду (1D/1W/1M/ALL); невідомий період = 1W."""
    days = PERIOD_TO_DAYS.get(period, 7)
    return datetime.utcnow() - timedelta(days=days) if days is not None else None


def closed_at_dt(row) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(row["closed_at"]))
    except (TypeError, ValueError, KeyError):
        return None


def in_period(row, cutoff: Optional[datetime]) -> bool:
    if cutoff is None:
        return True
    closed_at = closed_at_dt(row)
    return closed_at is not None and closed_at >= cutoff


def trade_net_pnl(row):
    """net_pnl закритої позиції; для старих записів без нього — realized_pnl."""
    return row["net_pnl"] if row["net_pnl"] is not None else row["realized_pnl"]


def closed_row_to_trade(row) -> dict:
    meta = load_metadata(row["metadata"])
    return {
        "order_id": row["order_id"],
        "symbol": row["symbol"],
        "side": row["side"],
        "leverage": meta.get("leverage"),
        "entry_price": meta.get("entry_price"),
        "close_price": row["close_price"],
        "stop_loss_price": meta.get("stop_loss_price"),
        "take_profit_levels": meta.get("take_profit_levels"),
        "strategy": meta.get("strategy"),
        "margin_usdt": row["margin_usdt"],
        "commission_usdt": row["commission_usdt"],
        "commission_open": row.get("commission_open"),
        "commission_close": row.get("commission_close"),
        "net_pnl": trade_net_pnl(row),
        "roe_percent": row["roe_percent"],
        "closed_at": row["closed_at"],
    }


def profit_factor(gross_profit: float, gross_loss: float) -> Optional[float]:
    """Валовий прибуток / валовий збиток (gross_loss — модуль). > 1 — у плюсі
    незалежно від win rate. None, якщо збитків не було (фронт покаже «∞»/«—»)."""
    if gross_loss <= 0:
        return None
    return round(gross_profit / gross_loss, 2)


def _round_or_none(value: Optional[float]) -> Optional[float]:
    return round(value, 2) if value is not None else None


class _WinLoss:
    """Лічильники для одного зрізу (увесь період або одна стратегія)."""

    def __init__(self):
        self.wins = 0
        self.losses = 0
        self.breakeven = 0
        self.pnl = 0.0
        self.gross_profit = 0.0
        self.gross_loss = 0.0

    def add(self, net: float) -> None:
        self.pnl += net
        if net > 0:
            self.wins += 1
            self.gross_profit += net
        elif net < 0:
            self.losses += 1
            self.gross_loss += -net
        else:
            self.breakeven += 1


def summarize_closed_trades(rows_newest_first: list, cutoff: Optional[datetime]) -> dict:
    """Зведення закритих угод (рядки positions, як віддає
    get_all_closed_positions — від нових до старих). Кумулятивна крива
    рахується від першої угоди за весь час, а в період потрапляють лише
    точки після cutoff."""
    equity = []
    running = 0.0
    total = _WinLoss()
    by_strategy: dict = defaultdict(_WinLoss)
    symbol_pnl: dict = defaultdict(float)
    best_trade: Optional[float] = None
    worst_trade: Optional[float] = None
    total_commission_usdt = 0.0

    for row in reversed(rows_newest_first):  # хронологічно, для накопичувальної суми
        net = trade_net_pnl(row) or 0.0
        running += net
        if not in_period(row, cutoff):
            continue

        equity.append({"t": str(row["closed_at"]), "v": round(running, 2)})
        strategy_name = load_metadata(row["metadata"]).get("strategy") or "Невідомо"
        by_strategy[strategy_name].add(net)
        symbol_pnl[row["symbol"]] += net
        total.add(net)
        best_trade = net if best_trade is None else max(best_trade, net)
        worst_trade = net if worst_trade is None else min(worst_trade, net)
        total_commission_usdt += row["commission_usdt"] or 0.0

    total_trades = total.wins + total.losses + total.breakeven
    # win rate сам по собі оманливий (5 вінів по $0.20 і один лос на $4 = 83%,
    # але мінус), тому рахуємо ще й гроші
    avg_win = total.gross_profit / total.wins if total.wins else None
    avg_loss = total.gross_loss / total.losses if total.losses else None  # модуль
    payoff_ratio = round(avg_win / avg_loss, 2) if avg_win is not None and avg_loss else None

    return {
        "equity": equity,
        "cumulative_pnl_raw": running,
        "win_rate": round((total.wins / total_trades) * 100, 1) if total_trades else 0.0,
        "total_trades": total_trades,
        "total_net_pnl": round(total.pnl, 2),
        "total_commission_usdt": round(total_commission_usdt, 2),
        "winning_trades": total.wins,
        "losing_trades": total.losses,
        "breakeven_trades": total.breakeven,
        "gross_profit": round(total.gross_profit, 2),
        "gross_loss": round(total.gross_loss, 2),  # модуль
        "profit_factor": profit_factor(total.gross_profit, total.gross_loss),
        "avg_win": _round_or_none(avg_win),
        "avg_loss": _round_or_none(avg_loss),  # модуль
        "payoff_ratio": payoff_ratio,
        "best_trade": _round_or_none(best_trade),
        "worst_trade": _round_or_none(worst_trade),
        "symbol_pnl": [
            {"symbol": s, "pnl": round(v, 2)}
            for s, v in sorted(symbol_pnl.items(), key=lambda kv: abs(kv[1]), reverse=True)
        ],
        "strategy_stats": [
            {
                "strategy": name,
                # беззбиткові угоди (net = 0) рахуються як win — як і раніше
                "win": acc.wins + acc.breakeven,
                "loss": acc.losses,
                "pnl": round(acc.pnl, 2),
                "profit_factor": profit_factor(acc.gross_profit, acc.gross_loss),
            }
            for name, acc in by_strategy.items()
        ],
    }


def sum_funding_fees(income_records: Iterable[dict], cutoff: Optional[datetime]) -> Tuple[float, float]:
    """(усього, за період) по записах FUNDING_FEE з /user/income. Від'ємне = сплачено біржі."""
    cutoff_ts_ms = cutoff.replace(tzinfo=timezone.utc).timestamp() * 1000 if cutoff is not None else None
    total = period = 0.0
    for rec in income_records:
        try:
            amount = float(rec.get('income', 0) or 0)
        except (TypeError, ValueError):
            continue
        total += amount
        rec_time_ms = rec.get('time')
        if cutoff_ts_ms is None or (rec_time_ms is not None and rec_time_ms >= cutoff_ts_ms):
            period += amount
    return total, period


def live_margin(position: dict) -> Optional[float]:
    """Маржа відкритої позиції з /user/positions (поле залежить від режиму маржі)."""
    for key in ("isolatedMargin", "initialMargin", "margin", "positionMargin"):
        if position.get(key) not in (None, ""):
            try:
                return float(position[key])
            except (TypeError, ValueError):
                continue
    return None


def unrealized_pnl(open_positions: Iterable[dict]) -> float:
    return sum(to_float(p.get("unrealizedProfit", 0)) for p in open_positions)
