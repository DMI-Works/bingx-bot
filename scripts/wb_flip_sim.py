# -*- coding: utf-8 -*-
"""Диагностика WallBreakout (live) + симуляция РАЗВОРОТА на реальных путях."""
import os, sys
sys.stdout.reconfigure(encoding="utf-8")
from statistics import mean
from pymongo import MongoClient
from dotenv import load_dotenv
load_dotenv()

db = MongoClient(os.environ["MONGO_URI"])["trading_bot"]
ta = db["trade_analytics"]
rows = [r for r in ta.find({"strategy":"WallBreakoutStrategy","net_pnl":{"$ne":None}})]

def decode(raw):
    out=[]
    for it in (raw or "").split(";"):
        t,_,p = it.partition(":")
        try: out.append((float(t),float(p)))
        except: pass
    return out

# ---------- 1. ДИАГНОСТИКА: асимметрия и MFE/MAE ----------
wins = [r for r in rows if (r.get("net_pnl") or 0)>0]
loss = [r for r in rows if (r.get("net_pnl") or 0)<=0]
def exc(r,k): return (r.get("excursion") or {}).get(k)
def avg(xs): xs=[x for x in xs if x is not None]; return mean(xs) if xs else 0.0

print("=== ДИАГНОСТИКА WallBreakout (live, n=%d) ===" % len(rows))
print(f"net PnL = {sum(r['net_pnl'] for r in rows):.2f} USDT | WR={len(wins)/len(rows)*100:.1f}%")
print(f"avg WIN = {avg([r['net_pnl'] for r in wins]):+.3f} | avg LOSS = {avg([r['net_pnl'] for r in loss]):+.3f} USDT")
print(f"payoff (avgW/|avgL|) = {avg([r['net_pnl'] for r in wins])/abs(avg([r['net_pnl'] for r in loss])):.3f}")
print(f"stop_distance_pct avg = {avg([r.get('stop_distance_pct') for r in rows]):.3f}%")
print()
print("            MFE%    MAE%   exit%  capt%MFE  gaveback%  stopdist%")
for nm,grp in (("WINS",wins),("LOSS",loss)):
    print(f"{nm:5} n={len(grp):2}  "
          f"{avg([exc(r,'mfe_pct') for r in grp]):5.3f}  "
          f"{avg([exc(r,'mae_pct') for r in grp]):6.3f}  "
          f"{avg([exc(r,'exit_pct') for r in grp]):5.3f}  "
          f"{avg([exc(r,'captured_pct_of_mfe') for r in grp]):6.1f}   "
          f"{avg([exc(r,'gave_back_pct') for r in grp]):7.3f}   "
          f"{avg([r.get('stop_distance_pct') for r in grp]):6.3f}")

# ---------- 2. ТЕОРЕТИЧЕСКОЕ ЗЕРКАЛО (верхняя граница) ----------
# net = realized + comm_open + comm_close + funding. Разворот: realized -> -realized,
# комиссии те же. flip_net = -realized + comm + funding = -net + 2*(comm+funding)
def comm(r): return (r.get("commission_open") or 0)+(r.get("commission_close") or 0)+(r.get("funding_fee") or 0)
flip_theo = [ -(r["net_pnl"]) + 2*comm(r) for r in rows ]
print("\n=== МОДЕЛЬ A: теоретическое зеркало (идеальный выход в зеркальной точке) ===")
print(f"сумма комиссий+funding = {sum(comm(r) for r in rows):.2f} USDT (платятся в ЛЮБУЮ сторону)")
print(f"flip net (теор.) = {sum(flip_theo):.2f} USDT   [= -({sum(r['net_pnl'] for r in rows):.2f}) + 2*({sum(comm(r) for r in rows):.2f})]")

# ---------- 3. СИМУЛЯЦИЯ НА ПУТЯХ (реалистично): жёсткий стоп S ----------
# move_pct в пути = направленно (+ в сторону сделки). Для разворота path' = -move.
# Исходная позиция: adverse = -S (S=stop_distance_pct). Валидируем модель на
# оригинале (выход = последняя точка пути = цена закрытия), потом тот же движок
# на зеркальном пути для разворота.
def sim(path, S):
    """Идём по пути; если move<=-S — стоп на -S; иначе выход в последней точке."""
    for t,m in path:
        if m <= -S:
            return -S
    return path[-1][1] if path else 0.0

def to_usdt(r, exit_pct):
    # pnl ≈ (exit_move%/100)*notional + комиссии (комиссии те же при развороте)
    return exit_pct/100.0*(r.get("notional_usdt") or 0) + comm(r)

val_err=[]; orig_sum=0; flip_sum=0
for r in rows:
    S = r.get("stop_distance_pct")
    path = decode(exc(r,"path"))
    if not S or not path: continue
    # валидация: оригинал
    o = to_usdt(r, sim(path, S))
    orig_sum += o
    val_err.append(o - r["net_pnl"])
    # разворот: зеркальный путь
    fpath = [(t,-m) for t,m in path]
    flip_sum += to_usdt(r, sim(fpath, S))

print("\n=== МОДЕЛЬ B: симуляция на реальных путях (жёсткий стоп, без трейлинга) ===")
print(f"валидация (sim оригинала vs факт): sum={orig_sum:.2f} vs факт -14.99, "
      f"ошибка на сделку avg={avg(val_err):+.3f} USDT")
print(f"flip net (путь, жёсткий стоп) = {flip_sum:.2f} USDT")
print("  (завышает плюсы разворота: без трейлинга победители едут до конца пути)")
