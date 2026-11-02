"""Где рождается проскальзывание стопа WallBreakout: корреляция с ликвидностью.
Ищем порог фильтра, отсекающий проскальзывающих, не трогая хорошие сделки."""
import os, sys
sys.stdout.reconfigure(encoding="utf-8")
from pymongo import MongoClient
from dotenv import load_dotenv
load_dotenv()
db = MongoClient(os.environ["MONGO_URI"])["trading_bot_testnet"]

rows = list(db["trade_analytics"].find({"strategy": "WallBreakoutStrategy", "net_pnl": {"$ne": None}}))

def feat(r):
    mc = r.get("market_context") or {}
    sc = r.get("signal_context") or {}
    net = r.get("net_pnl") or 0.0
    planned = r.get("planned_risk_usdt") or 0.0
    rmult = r.get("r_multiple")
    overpay = max(0.0, (-net) - planned) if net < 0 else 0.0  # переплата сверх плана
    return dict(
        sym=r.get("symbol"), net=net, overpay=overpay, rmult=rmult,
        vol=mc.get("quote_volume_24h"), rng=mc.get("range_24h_pct"),
        spread=sc.get("book_spread_pct"),
        depth=min(sc.get("bid_depth_usdt") or 0, sc.get("ask_depth_usdt") or 0),
    )

F = [feat(r) for r in rows]

def bucketize(key, edges, label):
    print(f"\n=== по {label} ===")
    print(f"{'бакет':>18} | {'n':>3} {'WR%':>5} {'PnL':>8} {'avg':>7} {'переплата':>9}")
    prev = None
    bounds = [None] + list(edges) + [None]
    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        sub = [f for f in F if f[key] is not None and
               (lo is None or f[key] >= lo) and (hi is None or f[key] < hi)]
        if not sub: continue
        n = len(sub)
        wins = sum(1 for f in sub if f["net"] > 0)
        pnl = sum(f["net"] for f in sub)
        over = sum(f["overpay"] for f in sub)
        name = f"[{lo if lo is not None else '-inf':>7},{hi if hi is not None else 'inf':>7})"
        if isinstance(lo, (int, float)): name = f"{lo:g}..{hi if hi is not None else '∞'}"
        else: name = f"<{hi}" if lo is None else (f">={lo}")
        print(f"{name:>18} | {n:>3} {wins/n*100:>5.0f} {pnl:>+8.1f} {pnl/n:>+7.3f} {over:>9.1f}")

bucketize("vol", [10e6, 20e6, 40e6, 80e6, 150e6], "24h объём USDT")
bucketize("rng", [10, 20, 30, 50, 80], "24h range % (волатильность)")
bucketize("depth", [5e3, 10e3, 20e3, 40e3], "мин. глубина книги USDT (bid/ask)")
bucketize("spread", [0.02, 0.04, 0.06, 0.1], "спред книги % на сигнале")

# Что даёт отсечение по volume+range: прибыльность оставшихся
print("\n\n===== ЭФФЕКТ ФИЛЬТРА =====")
def effect(cond, name):
    kept = [f for f in F if cond(f)]
    drop = [f for f in F if not cond(f)]
    kp = sum(f["net"] for f in kept); dp = sum(f["net"] for f in drop)
    kn = len(kept); kw = sum(1 for f in kept if f["net"] > 0)
    print(f"{name:<45} оставляем n={kn:>3} WR={kw/kn*100 if kn else 0:>4.0f}% PnL={kp:>+7.1f} | отсекаем n={len(drop):>3} PnL={dp:>+7.1f}")

effect(lambda f: True, "без фильтра (все)")
effect(lambda f: (f["vol"] or 0) >= 40e6, "volume >= 40M")
effect(lambda f: (f["vol"] or 0) >= 50e6, "volume >= 50M")
effect(lambda f: (f["rng"] or 0) <= 30, "range <= 30%")
effect(lambda f: (f["rng"] or 0) <= 25, "range <= 25%")
effect(lambda f: (f["vol"] or 0) >= 40e6 and (f["rng"] or 0) <= 30, "volume>=40M И range<=30%")
effect(lambda f: (f["vol"] or 0) >= 50e6 and (f["rng"] or 0) <= 25, "volume>=50M И range<=25%")
effect(lambda f: (f["depth"] or 0) >= 15e3, "глубина книги >= 15k")
effect(lambda f: (f["depth"] or 0) >= 15e3 and (f["rng"] or 0) <= 30, "глубина>=15k И range<=30%")

# worst symbols for blacklist
print("\n===== символы с наибольшей суммарной переплатой =====")
from collections import defaultdict
bs = defaultdict(lambda: [0.0, 0.0, 0])
for f in F:
    bs[f["sym"]][0] += f["overpay"]; bs[f["sym"]][1] += f["net"]; bs[f["sym"]][2] += 1
for s, (ov, net, n) in sorted(bs.items(), key=lambda x: -x[1][0])[:12]:
    print(f"  {s:<14} переплата={ov:>6.1f}  PnL={net:>+7.1f}  сделок={n}")
