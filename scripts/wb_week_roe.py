# -*- coding: utf-8 -*-
"""WallBreakout за 7 дней: асимметрия в ROE (с плечом)."""
import os, sys
from datetime import datetime, timedelta, timezone
from statistics import mean
sys.stdout.reconfigure(encoding="utf-8")
from pymongo import MongoClient
from dotenv import load_dotenv
load_dotenv()
db = MongoClient(os.environ["MONGO_URI"])["trading_bot"]
ta = db["trade_analytics"]

since = datetime.now(timezone.utc) - timedelta(days=7)
rows = [r for r in ta.find({"strategy":"WallBreakoutStrategy","net_pnl":{"$ne":None}})
        if (r.get("closed_at") or r.get("opened_at")) and
           (r.get("closed_at") or r.get("opened_at")).replace(tzinfo=timezone.utc) >= since]
def avg(xs): xs=[x for x in xs if x is not None]; return mean(xs) if xs else 0.0
def exc(r,k): return (r.get("excursion") or {}).get(k)
wins=[r for r in rows if (r.get("net_pnl") or 0)>0]
loss=[r for r in rows if (r.get("net_pnl") or 0)<=0]

print(f"=== WallBreakout за 7 дней (с {since.date()}), n={len(rows)} ===")
print(f"net PnL = {sum(r['net_pnl'] for r in rows):.2f} USDT | WR={len(wins)/len(rows)*100:.0f}%")
print(f"средний leverage = {avg([r.get('leverage') for r in rows]):.0f}x")
print()
print("            n   ROE%выход  ROE%стопа   MFE_ROE%  MAE_ROE%")
for nm,grp in (("WINS",wins),("LOSS",loss)):
    print(f"{nm:5}  {len(grp):3}  "
          f"{avg([r.get('roe_percent') for r in grp]):+8.2f}  "
          f"{avg([r.get('stop_distance_roi_pct') for r in grp]):8.2f}  "
          f"{avg([exc(r,'mfe_roi_pct') for r in grp]):+8.2f}  "
          f"{avg([exc(r,'mae_roi_pct') for r in grp]):+8.2f}")
print()
# распределение ROE убытков
lroe = sorted([r.get('roe_percent') for r in loss if r.get('roe_percent') is not None])
wroe = sorted([r.get('roe_percent') for r in wins if r.get('roe_percent') is not None])
print("ROE% убытков (отсортировано):", [f"{x:.0f}" for x in lroe])
print("ROE% побед   (отсортировано):", [f"{x:.0f}" for x in wroe])
print()
# сколько минусов было с проскальзыванием (факт стопа хуже планового)
print("--- проскальзывание стопа у убытков ---")
print(f"stop_slippage_pct avg (loss) = {avg([r.get('stop_slippage_pct') for r in loss]):.4f}")
print(f"stop_distance_pct avg (цена) = {avg([r.get('stop_distance_pct') for r in rows]):.3f}%  "
      f"→ при {avg([r.get('leverage') for r in rows]):.0f}x = {avg([r.get('stop_distance_roi_pct') for r in rows]):.1f}% ROE")
# разбивка по символам за неделю
from collections import defaultdict
by=defaultdict(list)
for r in rows: by[r['symbol']].append(r)
print("\n--- по символам (неделя) ---")
for k,v in sorted(by.items(), key=lambda x:-abs(sum(r['net_pnl'] for r in x[1]))):
    print(f"{k:16} n={len(v):2} net={sum(r['net_pnl'] for r in v):+6.2f} "
          f"WR={sum(1 for r in v if r['net_pnl']>0)/len(v)*100:.0f}% "
          f"avgROE_win={avg([r['roe_percent'] for r in v if r['net_pnl']>0]):+.1f} "
          f"avgROE_loss={avg([r['roe_percent'] for r in v if r['net_pnl']<=0]):+.1f}")
