"""
Розбір угод з CSV-експорту колекції trade_analytics (MongoDB Compass ->
Export -> CSV). Лише stdlib, БД і біржа не потрібні:

    python scripts/trade_report.py trading_bot_testnet.trade_analytics.csv
    python scripts/trade_report.py export.csv --strategy WallBreakoutStrategy

Що показує:
  1. Підсумок по стратегіях: WR, profit factor, очікування на угоду, частка комісій.
  2. Розрізи, які вже давали сигнал: LONG/SHORT, повторний вхід у монету
     <15 хв після попередньої угоди, проскальзування стопу, монети.
  3. Симуляцію виходів:
     - за MFE (є в усіх угодах): додатковий TP X% ROI поверх поточних
       виходів. Точна: якщо MFE угоди >= X, TP спрацював би (поки ціна туди
       йшла, угода була відкрита); інакше угода пройшла б так само, як була.
     - за траєкторією excursion.path (угоди після її появи): будь-які SL/TP
       і перенос у безубиток, у % руху ціни.

WR сам по собі нічого не каже: важливо очікування (середній результат угоди).
Поруч із кожним варіантом виводиться WR, потрібний для виходу в нуль.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

REENTRY_WINDOW_SECONDS = 900
BIG_STOP_SLIPPAGE_PCT = 0.3


def to_float(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


def parse_time(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((value or '')[:19])
    except ValueError:
        return None


def decode_path(raw: Optional[str]) -> List[Tuple[float, float]]:
    """"сек:pct;сек:pct" (ExcursionTracker.encode_path) -> [(сек, pct)]."""
    points = []
    for item in (raw or '').split(';'):
        t, _, p = item.partition(':')
        try:
            points.append((float(t), float(p)))
        except ValueError:
            continue
    return points


def load_closed(path: str, strategy: Optional[str] = None) -> List[dict]:
    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = [r for r in csv.DictReader(f) if (r.get('status') or '').upper() == 'CLOSED']
    if strategy:
        rows = [r for r in rows if r.get('strategy') == strategy]
    rows.sort(key=lambda r: r.get('opened_at') or '')
    return rows


# ---------- статистика ----------

def summarize(pnls: Sequence[float]) -> dict:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_loss = -sum(losses)
    return {
        'n': len(pnls),
        'wr': len(wins) / len(pnls) if pnls else 0.0,
        'sum': sum(pnls),
        'avg': sum(pnls) / len(pnls) if pnls else 0.0,
        'avg_win': sum(wins) / len(wins) if wins else 0.0,
        'avg_loss': sum(losses) / len(losses) if losses else 0.0,
        'pf': sum(wins) / gross_loss if gross_loss > 0 else float('inf'),
    }


def fmt_summary(label: str, pnls: Sequence[float]) -> str:
    s = summarize(pnls)
    if not s['n']:
        return f"  {label:<28} n=0"
    pf = '∞' if s['pf'] == float('inf') else f"{s['pf']:.2f}"
    return (
        f"  {label:<28} n={s['n']:<4} WR={s['wr']:>4.0%}  сума={s['sum']:>+9.2f}  "
        f"сер={s['avg']:>+7.3f}  сер.плюс={s['avg_win']:>+7.3f}  сер.мінус={s['avg_loss']:>+7.3f}  PF={pf}"
    )


def net_pnl(row: dict) -> float:
    return to_float(row.get('net_pnl')) or 0.0


def is_reentry(rows: List[dict]) -> Dict[int, bool]:
    """id(row) -> чи відкрита угода < REENTRY_WINDOW_SECONDS після закриття
    попередньої угоди по тій самій монеті."""
    last_close: Dict[str, datetime] = {}
    result = {}
    for row in rows:
        opened = parse_time(row.get('opened_at'))
        prev = last_close.get(row.get('symbol'))
        result[id(row)] = bool(opened and prev and (opened - prev).total_seconds() < REENTRY_WINDOW_SECONDS)
        closed = parse_time(row.get('closed_at'))
        if closed:
            last_close[row.get('symbol')] = closed
    return result


# ---------- симуляція виходів ----------

def fee_roi_pct(row: dict) -> float:
    """Комісії угоди у % ROI (відносно маржі)."""
    commission = to_float(row.get('commission_usdt'))
    margin = to_float(row.get('margin_usdt'))
    if commission is None or not margin:
        return 0.0
    return abs(commission) / margin * 100.0


def net_roe_pct(row: dict) -> float:
    """Фактичний результат угоди у % ROI після комісій (roe_percent у
    trade_analytics рахується з realized_pnl, тобто ДО комісій)."""
    margin = to_float(row.get('margin_usdt'))
    return net_pnl(row) / margin * 100.0 if margin else 0.0


def simulate_mfe_tp(rows: List[dict], tp_roi: float) -> List[float]:
    """Додатковий TP tp_roi% ROI поверх фактичних виходів, % ROI після
    комісій. MFE >= TP -> TP спрацював би першим (угода була відкрита, поки
    ціна туди йшла); інакше TP не досягнуто і угода пройшла як фактично."""
    out = []
    for row in rows:
        mfe_roi = to_float(row.get('excursion.mfe_roi_pct')) or 0.0
        out.append(tp_roi - fee_roi_pct(row) if mfe_roi >= tp_roi else net_roe_pct(row))
    return out


def simulate_path(
    path: Sequence[Tuple[float, float]],
    sl_pct: float,
    tp_pct: Optional[float],
    be_trigger_pct: Optional[float] = None,
    be_offset_pct: float = 0.0,
) -> Tuple[float, bool]:
    """Прохід траєкторією (рух ціни у %, + = у бік угоди).
    Повертає (результат у % ціни, чи спрацював SL/TP). Якщо ні SL, ні TP
    не спрацювали до кінця траєкторії — вихід за останньою точкою (реальне
    закриття угоди), resolved=False."""
    stop = -abs(sl_pct)
    for _, move in path:
        if be_trigger_pct is not None and move >= be_trigger_pct:
            stop = max(stop, be_offset_pct)
        if move <= stop:
            return stop, True
        if tp_pct is not None and move >= tp_pct:
            return tp_pct, True
    return (path[-1][1] if path else 0.0), False


def fee_price_pct(row: dict) -> float:
    value = to_float(row.get('commission_pct_of_notional'))
    return abs(value) if value is not None else 0.1


def path_grid(rows: List[dict]) -> List[str]:
    with_path = [(r, decode_path(r.get('excursion.path'))) for r in rows]
    with_path = [(r, p) for r, p in with_path if len(p) > 1]
    if not with_path:
        return ["  траєкторій (excursion.path) ще немає — з'являться в нових угодах"]

    lines = [f"  угод із траєкторією: {len(with_path)}  (результат у % ціни після комісій)"]
    lines.append(f"  {'SL%':>5} {'TP%':>5} {'BE при%':>7}  {'WR':>4}  {'сер%':>7}  {'WR для 0':>8}  невирішено")
    for sl in (0.5, 0.75, 1.0, 1.5, 2.0):
        for tp in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, None):
            for be in (None, 0.3, 0.5):
                if be is not None and tp is not None and be >= tp:
                    continue
                results, unresolved = [], 0
                for row, path in with_path:
                    fee = fee_price_pct(row)
                    move, resolved = simulate_path(path, sl, tp, be, be_offset_pct=fee)
                    unresolved += not resolved
                    results.append(move - fee)
                s = summarize(results)
                avg_fee = sum(fee_price_pct(r) for r, _ in with_path) / len(with_path)
                need = (sl + avg_fee) / (sl + tp) if tp else float('nan')
                tp_s = f"{tp:g}" if tp else '—'
                be_s = f"{be:g}" if be is not None else '—'
                lines.append(
                    f"  {sl:>5g} {tp_s:>5} {be_s:>7}  {s['wr']:>4.0%}  {s['avg']:>+7.3f}  "
                    f"{need:>8.0%}  {unresolved}"
                )
    return lines


# ---------- звіт ----------

def report(rows: List[dict]) -> List[str]:
    out = []
    by_strategy: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        by_strategy[row.get('strategy') or '—'].append(row)

    out.append("== Підсумок (net_pnl, USDT) ==")
    for name, items in sorted(by_strategy.items()):
        out.append(fmt_summary(name, [net_pnl(r) for r in items]))
        fees = sum(to_float(r.get('commission_usdt')) or 0.0 for r in items)
        out.append(f"  {'':<28} комісії={fees:+.2f}")

    for name, items in sorted(by_strategy.items()):
        out.append("")
        out.append(f"== {name}: розрізи ==")
        for side in ('LONG', 'SHORT'):
            out.append(fmt_summary(side, [net_pnl(r) for r in items if r.get('side') == side]))
        reentry = is_reentry(items)
        out.append(fmt_summary(f"повторний вхід <{REENTRY_WINDOW_SECONDS // 60} хв",
                               [net_pnl(r) for r in items if reentry[id(r)]]))
        out.append(fmt_summary("свіжий вхід", [net_pnl(r) for r in items if not reentry[id(r)]]))
        slipped = [r for r in items if (to_float(r.get('stop_slippage_pct')) or 0.0) > BIG_STOP_SLIPPAGE_PCT]
        out.append(fmt_summary(f"прослизання стопу >{BIG_STOP_SLIPPAGE_PCT}%", [net_pnl(r) for r in slipped]))

        per_symbol: Dict[str, List[float]] = defaultdict(list)
        for r in items:
            per_symbol[r.get('symbol')].append(net_pnl(r))
        worst = sorted(per_symbol.items(), key=lambda kv: sum(kv[1]))[:5]
        out.append("  найгірші монети: " + ", ".join(f"{s} {sum(v):+.2f} ({len(v)})" for s, v in worst))

        out.append("")
        out.append(f"== {name}: + фіксований TP за MFE (% ROI після комісій) ==")
        out.append(f"  {'TP %ROI':>7}  {'WR':>4}  {'сер %ROI':>8}")
        out.append("  " + f"{'факт':>7}  {summarize([net_pnl(r) for r in items])['wr']:>4.0%}  "
                   f"{summarize([net_roe_pct(r) for r in items])['avg']:>+8.2f}")
        for tp in (5, 10, 15, 20, 30, 40):
            s = summarize(simulate_mfe_tp(items, tp))
            out.append(f"  {tp:>7}  {s['wr']:>4.0%}  {s['avg']:>+8.2f}")

        out.append("")
        out.append(f"== {name}: симуляція за траєкторією ==")
        out.extend(path_grid(items))
    return out


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Розбір угод з CSV-експорту trade_analytics")
    parser.add_argument('csv_path')
    parser.add_argument('--strategy', help="лише одна стратегія (напр. WallBreakoutStrategy)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    rows = load_closed(args.csv_path, args.strategy)
    if not rows:
        print("Закритих угод не знайдено")
        return 1
    print("\n".join(report(rows)))
    return 0


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sys.exit(main())
