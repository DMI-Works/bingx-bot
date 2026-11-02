# -*- coding: utf-8 -*-
import os, sys
from statistics import mean
sys.stdout.reconfigure(encoding="utf-8")
from pymongo import MongoClient
from dotenv import load_dotenv
load_dotenv()
db = MongoClient(os.environ["MONGO_URI"])["trading_bot"]
allrows=[r for r in db["trade_analytics"].find({"strategy":"WallBreakoutStrategy","net_pnl":{"$ne":None}})]
def dec(raw):
    o=[]
    for it in (raw or "").split(";"):
        t,_,p=it.partition(":")
        try:o.append((float(t),float(p)))
        except:pass
    return o
def exc(r,k):return (r.get("excursion") or {}).get(k)
SLIP=1.0
def sim(rows, stop_roe, tp_roe):
    tot=0.0
    for r in rows:
        lev=r.get("leverage") or 20
        path=dec(exc(r,"path")); realized=r.get("roe_percent")
        notional=r.get("notional_usdt") or 0
        comm=(r.get("commission_open") or 0)+(r.get("commission_close") or 0)+(r.get("funding_fee") or 0)
        if realized is None or not path: continue
        exit_roe=realized
        for t,m in path:
            roe=m*lev
            if roe<=-stop_roe: exit_roe=-(stop_roe+SLIP); break
            if tp_roe and roe>=tp_roe: exit_roe=tp_roe; break
        margin=notional/lev if lev else 0
        tot+=exit_roe/100.0*margin+comm
    return tot

majors={"BTC-USDT","ETH-USDT","SOL-USDT","BNB-USDT","HYPE-USDT","ZEC-USDT"}
maj=[r for r in allrows if r["symbol"] in majors]
junk=[r for r in allrows if r["symbol"] not in majors]
print(f"ALL n={len(allrows)}  MAJORS n={len(maj)}  OTHER n={len(junk)}")
print(f"факт net: ALL={sum(r['net_pnl'] for r in allrows):.2f}  "
      f"MAJ={sum(r['net_pnl'] for r in maj):.2f}  OTHER={sum(r['net_pnl'] for r in junk):.2f}\n")

print("=== Сетка стоп × тейк (net USDT, модель) — ВСЕ символы ===")
print("        tp=none   tp=10    tp=12    tp=15")
for S in (18,12,10,8):
    cells=[sim(allrows,S,tp) for tp in (0,10,12,15)]
    print(f"stop -{S:2}%  " + "  ".join(f"{c:+7.2f}" for c in cells))

print("\n=== Та же сетка — ТОЛЬКО МАЙОРЫ ===")
print("        tp=none   tp=10    tp=12    tp=15")
for S in (18,12,10,8):
    cells=[sim(maj,S,tp) for tp in (0,10,12,15)]
    print(f"stop -{S:2}%  " + "  ".join(f"{c:+7.2f}" for c in cells))
