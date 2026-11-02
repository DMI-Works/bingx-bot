"""Симуляция РЕАЛЬНОЙ логики TrailingStopManager (ladder + буфер) по excursion-путям.
Сравнивает текущую лестницу с кандидатами. Всё в % цены после комиссий."""
import os, sys
sys.stdout.reconfigure(encoding="utf-8")
from pymongo import MongoClient
from dotenv import load_dotenv
load_dotenv()
db = MongoClient(os.environ["MONGO_URI"])["trading_bot_testnet"]

LEV = 20
STOP_BUFFER_PCT = 0.5          # dynamic_stop_buffer_percent (config default)
MAX_BUFFER_FRACTION = 0.8

def decode_path(raw):
    pts = []
    for item in (raw or "").split(";"):
        t, _, p = item.partition(":")
        try: pts.append((float(t), float(p)))
        except ValueError: continue
    return pts

rows = list(db["trade_analytics"].find({"strategy": "WallBreakoutStrategy", "net_pnl": {"$ne": None}}))
wp = []
for r in rows:
    path = decode_path((r.get("excursion") or {}).get("path"))
    if len(path) > 1:
        wp.append(dict(path=path, fee=abs(r.get("commission_pct_of_notional") or 0.1)))
print(f"путей: {len(wp)}")

def level_stop_price_pct(L):
    """% цены (favorable) куда встанет стоп на уровне L %ROI — повтор _stop_price_for_level."""
    level_price = L / LEV
    buf = min(STOP_BUFFER_PCT / LEV, level_price * MAX_BUFFER_FRACTION)
    return max(level_price - buf, 0.0)

def sim_ladder(path, sl_price, levels):
    """sl_price: % цены (>0). levels: список %ROI по возрастанию.
    Возврат (move_pct, resolved)."""
    stop = -abs(sl_price)
    reached = -1
    lvl_price = [level_stop_price_pct(L) for L in levels]
    lvl_trig = [L / LEV for L in levels]  # % цены favorable для срабатывания уровня
    for _, m in path:
        # поднять достигнутый уровень
        i = reached
        while i + 1 < len(levels) and m >= lvl_trig[i + 1]:
            i += 1
        if i > reached:
            reached = i
            cand = lvl_price[reached]
            if cand > stop:
                stop = cand
        if m <= stop:
            return stop, True
    return (path[-1][1] if path else 0.0), False

def summ(res):
    n = len(res); w = [x for x in res if x > 0]; l = [x for x in res if x <= 0]
    gl = -sum(l)
    return dict(n=n, wr=len(w)/n*100 if n else 0, sum=sum(res), avg=sum(res)/n if n else 0,
                pf=(sum(w)/gl if gl > 0 else 999),
                aw=sum(w)/len(w) if w else 0, al=sum(l)/len(l) if l else 0)

def run(name, sl_roi, levels):
    sl_price = sl_roi / LEV
    res = []
    for t in wp:
        move, _ = sim_ladder(t["path"], sl_price, levels)
        res.append(move - t["fee"])
    s = summ(res)
    print(f"{name:<40} SL={sl_roi:>3g}%ROI | WR={s['wr']:>4.0f}% avg={s['avg']:>+7.3f} "
          f"sum={s['sum']:>+7.1f} PF={s['pf']:>5.2f} aw={s['aw']:>+.3f} al={s['al']:>+.3f}")
    return s

# текущая живая лестница
cur = [6, 10, 20, 32, 64, 120, 200, 250, 300, 350, 400]
print("\n=== ТЕКУЩАЯ (факт конфиг) ===")
run("current SL=15 ladder[6,10,20,32,64..]", 15, cur)

print("\n=== КАНДИДАТЫ: плотная лестница + разный SL ===")
# плотные шаги ~2%ROI в ранней зоне (где большинство выходов), расширяясь дальше
dense2 = [6, 8, 10, 12, 14, 16, 18, 20, 23, 26, 30, 36, 44, 56, 72, 95, 125, 165, 220, 300]
dense3 = [6, 9, 12, 15, 18, 21, 25, 30, 36, 44, 54, 68, 86, 110, 145, 190, 250, 320]
dense1_5 = [5, 6.5, 8, 9.5, 11, 12.5, 14, 16, 18, 20, 23, 27, 32, 40, 52, 68, 90, 120, 160, 210, 280]
for sl in (10, 12, 15):
    run(f"dense~2%ROI", sl, dense2)
    run(f"dense~3%ROI", sl, dense3)
    run(f"dense~1.5%ROI", sl, dense1_5)
    print()

print("=== буфер влияет: что если stop_buffer_percent меньше (плотнее лок) ===")
for buf in (0.5, 0.3, 0.2):
    STOP_BUFFER_PCT = buf
    run(f"dense~2%ROI buf={buf}", 10, dense2)
STOP_BUFFER_PCT = 0.5
