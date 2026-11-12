"""
Чиста (без I/O) зведена статистика закритих угод одного користувача для
картки в адмінці: результат за періоди, винрейт, комісії, розбивка за стратегіями.
Рядки — документи trade_analytics зі status=CLOSED.
"""

from datetime import datetime, timedelta
from typing import Iterable, Optional


def _net(row: dict) -> float:
    value = row.get("net_pnl")
    if value is None:
        value = row.get("realized_pnl")
    return float(value or 0.0)


def summarize_user_trades(rows: Iterable[dict], now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoffs = {"today": day_start, "d7": now - timedelta(days=7), "d30": now - timedelta(days=30)}

    totals = {"net_pnl": 0.0, "today": 0.0, "d7": 0.0, "d30": 0.0, "fees": 0.0}
    trades = wins = 0
    best = worst = None
    last_trade_at = None
    by_strategy = {}

    for row in rows:
        net = _net(row)
        closed_at = row.get("closed_at")
        trades += 1
        wins += net > 0
        totals["net_pnl"] += net
        totals["fees"] += float(row.get("commission_usdt") or 0.0)
        best = net if best is None else max(best, net)
        worst = net if worst is None else min(worst, net)
        if isinstance(closed_at, datetime):
            for key, cutoff in cutoffs.items():
                if closed_at >= cutoff:
                    totals[key] += net
            if last_trade_at is None or closed_at > last_trade_at:
                last_trade_at = closed_at
        strategy = row.get("strategy") or "—"
        item = by_strategy.setdefault(strategy, {"strategy": strategy, "trades": 0, "wins": 0, "net_pnl": 0.0})
        item["trades"] += 1
        item["wins"] += net > 0
        item["net_pnl"] += net

    return {
        **{key: round(value, 2) for key, value in totals.items()},
        "trades": trades,
        "wins": wins,
        "winrate": round(wins / trades * 100, 1) if trades else None,
        "best": round(best, 2) if best is not None else None,
        "worst": round(worst, 2) if worst is not None else None,
        "last_trade_at": last_trade_at,
        "by_strategy": sorted(
            ({**s, "net_pnl": round(s["net_pnl"], 2)} for s in by_strategy.values()),
            key=lambda s: s["net_pnl"], reverse=True,
        ),
    }
