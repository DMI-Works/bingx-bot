# -*- coding: utf-8 -*-
"""Симуляция: ужать стоп по ROE на реальных путях WallBreakout (неделя)."""
import os, sys
from statistics import mean
sys.stdout.reconfigure(encoding="utf-8")
from pymongo import MongoClient
from dotenv import load_dotenv
load_dotenv()
db = MongoClient(os.environ["MONGO_URI"])["trading_bot"]
rows=[r for r in db["trade_analytics"].find({"strategy":"WallBreakoutStrategy","net_pnl":{"$ne":None}})]
def dec(raw):
    o=[]
    for it in (raw or "").split(";"):
        t,_,p=it.partition(":")
        try:o.append((float(t),float(p)))
        except:pass
    return o
def exc(r,k):return (r.get("excursion") or {}).get(k)

SLIP=1.0  # ~1% ROE проскальзывание на стопе (из факта)
def sim(stop_roe):
    total_roe=0; total_usdt=0; conv=0
    for r in rows:
        lev=r.get("leverage") or 20
        path=dec(exc(r,"path"))
        realized_roe=r.get("roe_percent")
        notional=r.get("notional_usdt") or 0
        comm=(r.get("commission_open") or 0)+(r.get("commission_close") or 0)+(r.get("funding_fee") or 0)
        if realized_roe is None or not path:
            continue
        # идём по пути в ROE; первый прокол -stop_roe -> стоп
        exit_roe=realized_roe
        for t,m in path:
            if m*lev <= -stop_roe:
                exit_roe=-(stop_roe+SLIP)
                if realized_roe>0: conv+=1   # был победой, стал убытком
                break
        total_roe+=exit_roe
        # usdt: roe -> pnl через маржу; margin = notional/lev
        margin=notional/lev if lev else 0
        total_usdt += exit_roe/100.0*margin + comm
    return total_roe/len(rows), total_usdt, conv

print("Факт: net = -14.99 USDT, стоп ~17.4%%+слип = ~18.4%% ROE\n")
print("стоп ROE | ср.ROE/сделку | net USDT(модель) | побед->убыток")
# базовая модель (текущий стоп) для сверки
for X in (17,14,12,10,9,8,7,6,5):
    aroe,usdt,conv=sim(X)
    print(f"  -{X:4}%  |   {aroe:+7.2f}%     |   {usdt:+7.2f}        |   {conv}")
