import os, sys
sys.stdout.reconfigure(encoding="utf-8")
from pymongo import MongoClient
from dotenv import load_dotenv
load_dotenv()
db = MongoClient(os.environ["MONGO_URI"])["trading_bot"]
ta = db["trade_analytics"]
rows = [r for r in ta.find({"strategy":"WallBreakoutStrategy","net_pnl":{"$ne":None}})]
print("WallBreakout closed:", len(rows))
s = rows[0]
print("\nSAMPLE KEYS:", sorted(s.keys()))
print("\nexcursion keys:", sorted((s.get("excursion") or {}).keys()))
for k in ("entry_price","stop_loss_price_initial","leverage","net_pnl","realized_pnl",
          "commission_open","commission_close","funding_fee","r_multiple","mfe_pct","mae_pct",
          "side","close_reason"):
    print(f"  {k} = {s.get(k)}")
ex = s.get("excursion") or {}
for k in ("mfe_pct","mae_pct","exit_pct","gave_back_pct","captured_pct_of_mfe","path_step_pct"):
    print(f"  excursion.{k} = {ex.get(k)}")
p = (ex.get("path") or "")
print("  path head:", p[:120], "...len", len(p))
