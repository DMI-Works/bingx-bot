"""
Проверка скальп-гипотезы на РЕАЛЬНЫХ траекториях цены (excursion.path).

Вопрос, на который отвечает: "если бы мы на тех же входах выходили
по-скальперски — тугой стоп + маленький тейк + быстрый безубыток — какой
был бы payoff и expectancy ПОСЛЕ комиссий?"

Только чтение. Запуск: python scripts/scalp_sim.py [дней]  (по умолчанию 7)
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from pymongo import MongoClient

MONGO_URI = os.environ["MONGO_URI"]
BASE = os.getenv("MONGO_DB_NAME") or "trading_bot"
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7
CUTOFF = datetime.utcnow() - timedelta(days=DAYS)

client = MongoClient(MONGO_URI)


def decode_path(raw):
    pts = []
    for item in (raw or "").split(";"):
        t, _, p = item.partition(":")
        try:
            pts.append((float(t), float(p)))
        except ValueError:
            continue
    return pts


def simulate_path(path, sl_pct, tp_pct, be_trigger=None, be_offset=0.0):
    """Проход по траектории (move = движение цены в % в сторону сделки, + в плюс).
    Возвращает движение цены на выходе в %. Хронологически: что раньше задето."""
    stop = -abs(sl_pct)
    for _, move in path:
        if be_trigger is not None and move >= be_trigger:
            stop = max(stop, be_offset)
        if move <= stop:
            return stop
        if tp_pct is not None and move >= tp_pct:
            return tp_pct
    return path[-1][1] if path else 0.0


def fee_pct(row):
    v = row.get("commission_pct_of_notional")
    try:
        return abs(float(v)) if v is not None else 0.1
    except (TypeError, ValueError):
        return 0.1


def collect(db_name):
    db = client[db_name]
    if "trade_analytics" not in db.list_collection_names():
        return []
    rows = list(db.trade_analytics.find({
        "status": "CLOSED",
        "closed_at": {"$gte": CUTOFF},
    }))
    out = []
    for r in rows:
        exc = r.get("excursion") or {}
        path = decode_path(exc.get("path"))
        if len(path) > 1:
            out.append((r, path))
    return out


def stats(results):
    n = len(results)
    if not n:
        return None
    wins = [x for x in results if x > 0]
    losses = [x for x in results if x <= 0]
    gl = -sum(losses)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0  # <= 0
    return {
        "n": n,
        "wr": len(wins) / n,
        "sum": sum(results),
        "avg": sum(results) / n,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": (avg_win / -avg_loss) if avg_loss < 0 else float("inf"),
        "pf": (sum(wins) / gl) if gl > 0 else float("inf"),
    }


def fnum(x, d=3):
    if x == float("inf"):
        return "∞"
    return f"{x:,.{d}f}"


# --- сбор ---
trades = []
for db_name in (f"{BASE}_testnet", BASE):
    trades += collect(db_name)

if not trades:
    print(f"За последние {DAYS} дней нет закрытых сделок с траекторией (excursion.path).")
    client.close()
    sys.exit(0)

by_strat = defaultdict(list)
for r, p in trades:
    by_strat[r.get("strategy") or "?"].append((r, p))

print("=" * 96)
print(f"СКАЛЬП-СИМУЛЯЦИЯ на реальных траекториях   окно: {DAYS} дн   сделок с путём: {len(trades)}")
print("Результат = движение цены на выходе (%) − round-trip комиссия (%). Всё в % ЦЕНЫ (не ROI).")
print("=" * 96)

# baseline: фактический результат в % цены после комиссий
base = []
for r, p in trades:
    pm = r.get("price_move_pct")
    if pm is None:
        continue
    base.append(pm - fee_pct(r))
s = stats(base)
if s:
    print(f"\nФАКТ (как реально закрывали): n={s['n']}  WR={s['wr']:.0%}  "
          f"ср={fnum(s['avg'])}%  payoff={fnum(s['payoff'],2)}  сумма={fnum(s['sum'],2)}%")

SLS = [0.2, 0.3, 0.4, 0.5]
TPS = [0.3, 0.4, 0.5, 0.75, 1.0]
BES = [None, 0.15, 0.25]


def run_grid(items, title):
    print(f"\n--- {title}  (n={len(items)}) ---")
    print(f"  {'SL%':>4} {'TP%':>4} {'BE%':>5}   {'WR':>4} {'ср.плюс':>8} {'ср.минус':>8} "
          f"{'payoff':>7} {'expect%':>8} {'сумма%':>8}   {'оценка':>6}")
    best = []
    for sl in SLS:
        for tp in TPS:
            for be in BES:
                if be is not None and be >= tp:
                    continue
                res = []
                for r, p in items:
                    fee = fee_pct(r)
                    move = simulate_path(p, sl, tp, be, be_offset=fee)
                    res.append(move - fee)
                st = stats(res)
                if not st:
                    continue
                # цель пользователя: payoff>=1 и положительное ожидание
                good = st["payoff"] >= 1.0 and st["avg"] > 0
                mark = "  <==" if good else ""
                be_s = f"{be:g}" if be is not None else "—"
                print(f"  {sl:>4g} {tp:>4g} {be_s:>5}   {st['wr']:>4.0%} "
                      f"{fnum(st['avg_win']):>8} {fnum(st['avg_loss']):>8} "
                      f"{fnum(st['payoff'],2):>7} {fnum(st['avg']):>8} {fnum(st['sum'],2):>8}{mark}")
                best.append((st["avg"], sl, tp, be, st))
    best.sort(reverse=True)
    if best:
        avg, sl, tp, be, st = best[0]
        be_s = f"{be:g}" if be is not None else "нет"
        print(f"  ЛУЧШИЙ по ожиданию: SL={sl}% TP={tp}% BE={be_s}  ->  "
              f"WR={st['wr']:.0%} payoff={fnum(st['payoff'],2)} expect={fnum(st['avg'])}%/сделку")


run_grid(trades, "ВСЕ стратегии вместе")
for name, items in sorted(by_strat.items(), key=lambda kv: -len(kv[1])):
    if len(items) >= 5:
        run_grid(items, name)

client.close()
