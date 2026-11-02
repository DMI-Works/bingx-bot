"""Подбор прибыльных параметров WallBreakout по excursion-путям из БД.
Все проценты — в % ДВИЖЕНИЯ ЦЕНЫ (+ = в сторону сделки). Комиссия вычитается.
"""
import os, sys
sys.stdout.reconfigure(encoding="utf-8")
from pymongo import MongoClient
from dotenv import load_dotenv
load_dotenv()
db = MongoClient(os.environ["MONGO_URI"])["trading_bot_testnet"]

LEV = 20  # текущее плечо, для перевода price% <-> ROI%

def decode_path(raw):
    pts = []
    for item in (raw or "").split(";"):
        t, _, p = item.partition(":")
        try: pts.append((float(t), float(p)))
        except ValueError: continue
    return pts

rows = list(db["trade_analytics"].find({"strategy": "WallBreakoutStrategy", "net_pnl": {"$ne": None}}))
trades = []
for r in rows:
    exc = r.get("excursion") or {}
    path = decode_path(exc.get("path"))
    fee = abs(r.get("commission_pct_of_notional") or 0.1)
    trades.append(dict(path=path, fee=fee, net=r.get("net_pnl"),
                       margin=r.get("margin_usdt"), side=r.get("side"),
                       has_path=len(path) > 1))
wp = [t for t in trades if t["has_path"]]
print(f"сделок всего={len(trades)}  с путём={len(wp)}")

def summ(res):
    n = len(res)
    if not n: return None
    w = [x for x in res if x > 0]; l = [x for x in res if x <= 0]
    gl = -sum(l)
    return dict(n=n, wr=len(w)/n*100, sum=sum(res), avg=sum(res)/n,
                aw=sum(w)/len(w) if w else 0, al=sum(l)/len(l) if l else 0,
                pf=(sum(w)/gl if gl > 0 else 999))

def sim_fixed(path, sl, tp, be_trig=None, be_off=0.0):
    """SL/TP/безубыток. Возврат (move_pct, resolved)."""
    stop = -abs(sl)
    for _, m in path:
        if be_trig is not None and m >= be_trig:
            stop = max(stop, be_off)
        if m <= stop:
            return stop, True
        if tp is not None and m >= tp:
            return tp, True
    return (path[-1][1] if path else 0.0), False

def sim_trail(path, sl, act, give_back, be_off=0.0):
    """Трейлинг: пока не достигли act — стоп на -sl. После act — стоп = peak-give_back
    (но не ниже безубытка be_off). Возврат (move_pct, resolved)."""
    stop = -abs(sl)
    peak = None
    for _, m in path:
        if m >= act:
            peak = m if peak is None else max(peak, m)
            stop = max(stop, peak - give_back, be_off)
        if m <= stop:
            return stop, True
    return (path[-1][1] if path else 0.0), False

print("\n===== ФИКС SL/TP/БЕЗУБЫТОК (% цены, после комиссий) =====")
print(f"{'SL%':>5}{'TP%':>6}{'BE@%':>6} | {'n':>3}{'WR%':>6}{'avg':>8}{'sum':>9}{'PF':>6}{'unres':>6}  ROI:SL/TP")
best = []
for sl in (0.3, 0.4, 0.5, 0.6, 0.75, 1.0):
    for tp in (0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.5, None):
        for be in (None, 0.2, 0.3, 0.4):
            if be is not None and tp is not None and be >= tp: continue
            res = []; unres = 0
            for t in wp:
                move, r = sim_fixed(t["path"], sl, tp, be, be_off=t["fee"])
                unres += not r
                res.append(move - t["fee"])
            s = summ(res)
            best.append((s["avg"], sl, tp, be, s))
            if tp in (0.4,0.5,0.6,None) and be in (None,0.3):
                tps = f"{tp:g}" if tp else "—"; bes = f"{be:g}" if be else "—"
                print(f"{sl:>5g}{tps:>6}{bes:>6} | {s['n']:>3}{s['wr']:>6.0f}{s['avg']:>+8.3f}{s['sum']:>+9.1f}{s['pf']:>6.2f}{unres:>6}  {sl*LEV:g}/{tp*LEV if tp else 0:g}")

print("\n--- ТОП-12 фикс-конфигов по средней сделке ---")
for avg, sl, tp, be, s in sorted(best, key=lambda x: -x[0])[:12]:
    tps = f"{tp:g}" if tp else "—"; bes = f"{be:g}" if be else "—"
    print(f"SL={sl:g} TP={tps} BE={bes:>3} | WR={s['wr']:.0f}% avg={s['avg']:+.3f} sum={s['sum']:+.1f} PF={s['pf']:.2f}  | ROI: SL={sl*LEV:g}% TP={tp*LEV if tp else 0:g}%")

print("\n===== ТРЕЙЛИНГ (активация @act, отдача give_back от пика) =====")
print(f"{'SL%':>5}{'act%':>6}{'give%':>7} | {'WR%':>6}{'avg':>8}{'sum':>9}{'PF':>6}  ROI:SL/act/give")
btrail = []
for sl in (0.3, 0.4, 0.5, 0.75):
    for act in (0.2, 0.3, 0.4, 0.5):
        for gb in (0.1, 0.15, 0.2, 0.3):
            res = []
            for t in wp:
                move, r = sim_trail(t["path"], sl, act, gb, be_off=t["fee"])
                res.append(move - t["fee"])
            s = summ(res)
            btrail.append((s["avg"], sl, act, gb, s))
print("--- ТОП-12 трейлинг-конфигов ---")
for avg, sl, act, gb, s in sorted(btrail, key=lambda x: -x[0])[:12]:
    print(f"SL={sl:g} act={act:g} give={gb:g} | WR={s['wr']:.0f}% avg={s['avg']:+.3f} sum={s['sum']:+.1f} PF={s['pf']:.2f}"
          f"  | ROI: SL={sl*LEV:g}% act={act*LEV:g}% give={gb*LEV:g}%")

# базлайн: фактический результат этих же сделок (с путём) в % цены
base = []
for t in wp:
    # фактический net в % цены ~ net/notional*100; notional=margin*lev
    if t["margin"]:
        base.append(t["net"] / (t["margin"] * LEV) * 100)
s = summ(base)
print(f"\nФАКТ (эти же {s['n']} сделок): WR={s['wr']:.0f}% avg={s['avg']:+.3f}%цены sum={s['sum']:+.1f} PF={s['pf']:.2f}")
