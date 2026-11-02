"""Выгрузка и анализ trade_analytics из Mongo для поиска, что корректировать."""
import os
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

uri = os.environ["MONGO_URI"]
dbname = os.environ.get("MONGO_DB_NAME", "trading_bot")
# testnet suffix
import yaml
with open("config/config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
if cfg.get("exchange", {}).get("testnet"):
    dbname = dbname + "_testnet"

print(f"DB: {dbname}")
cli = MongoClient(uri)
db = cli[dbname]

print("collections:", db.list_collection_names())
ta = db["trade_analytics"]
total = ta.count_documents({})
closed = ta.count_documents({"close_price": {"$ne": None}})
print(f"trade_analytics total={total} closed={closed}")

# pull closed trades
rows = list(ta.find({"net_pnl": {"$ne": None}}))
print(f"rows with net_pnl: {len(rows)}")
if not rows:
    # show a sample doc structure
    s = ta.find_one()
    if s:
        print("sample keys:", sorted(s.keys()))
    raise SystemExit

def stats(subset):
    n = len(subset)
    if n == 0:
        return None
    pnls = [r.get("net_pnl", 0.0) or 0.0 for r in subset]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = sum(losses)
    total_pnl = sum(pnls)
    wr = len(wins) / n * 100
    pf = (gross_win / abs(gross_loss)) if gross_loss else float("inf")
    avg_win = gross_win / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 0
    exp = total_pnl / n
    return dict(n=n, wr=wr, pf=pf, total=total_pnl, avg_win=avg_win,
               avg_loss=avg_loss, exp=exp, gw=gross_win, gl=gross_loss)

def show(name, st):
    if not st:
        print(f"{name}: нет данных")
        return
    print(f"{name}: n={st['n']} WR={st['wr']:.1f}% PF={st['pf']:.2f} "
          f"PnL={st['total']:.2f} exp/trade={st['exp']:.3f} "
          f"avgW={st['avg_win']:.3f} avgL={st['avg_loss']:.3f}")

print("\n==== ОБЩЕЕ ====")
show("ALL", stats(rows))

print("\n==== ПО СТРАТЕГИИ ====")
by = defaultdict(list)
for r in rows:
    by[r.get("strategy", "?")].append(r)
for k, v in sorted(by.items(), key=lambda x: -sum(r.get("net_pnl",0) or 0 for r in x[1])):
    show(k, stats(v))

print("\n==== ПО ПРИЧИНЕ ЗАКРЫТИЯ ====")
by = defaultdict(list)
for r in rows:
    by[r.get("close_reason", "?")].append(r)
for k, v in sorted(by.items(), key=lambda x: -len(x[1])):
    show(k, stats(v))

print("\n==== ПО СТОРОНЕ ====")
by = defaultdict(list)
for r in rows:
    by[r.get("side", "?")].append(r)
for k, v in by.items():
    show(k, stats(v))

print("\n==== ПО СИМВОЛУ (топ по объему) ====")
by = defaultdict(list)
for r in rows:
    by[r.get("symbol", "?")].append(r)
items = sorted(by.items(), key=lambda x: -len(x[1]))[:20]
for k, v in items:
    show(k, stats(v))

print("\n==== ПО ЧАСУ UTC ====")
by = defaultdict(list)
for r in rows:
    by[r.get("opened_hour_utc", -1)].append(r)
for k in sorted(by):
    show(f"h{k:02d}" if isinstance(k,int) and k>=0 else str(k), stats(by[k]))

print("\n==== R-MULTIPLE / МЕТРИКИ ВЫХОДА ====")
rmults = [r["r_multiple"] for r in rows if r.get("r_multiple") is not None]
if rmults:
    print(f"r_multiple: n={len(rmults)} avg={sum(rmults)/len(rmults):.3f} "
          f"min={min(rmults):.2f} max={max(rmults):.2f}")
slip = [r["stop_slippage_pct"] for r in rows if r.get("stop_slippage_pct") is not None]
if slip:
    print(f"stop_slippage_pct: n={len(slip)} avg={sum(slip)/len(slip):.4f}")
entry_slip = [r["entry_slippage_pct"] for r in rows if r.get("entry_slippage_pct") is not None]
if entry_slip:
    print(f"entry_slippage_pct: n={len(entry_slip)} avg={sum(entry_slip)/len(entry_slip):.4f}")
comm = [r["commission_pct_of_notional"] for r in rows if r.get("commission_pct_of_notional") is not None]
if comm:
    print(f"commission_pct_of_notional: n={len(comm)} avg={sum(comm)/len(comm):.4f}")
sl_moves = [r.get("sl_moves_count", 0) for r in rows]
if sl_moves:
    print(f"sl_moves_count avg={sum(sl_moves)/len(sl_moves):.2f}")

print("\n==== MFE / MAE ====")
mfe = [r["mfe_pct"] for r in rows if r.get("mfe_pct") is not None]
mae = [r["mae_pct"] for r in rows if r.get("mae_pct") is not None]
if mfe:
    print(f"mfe_pct avg={sum(mfe)/len(mfe):.3f}")
if mae:
    print(f"mae_pct avg={sum(mae)/len(mae):.3f}")

# для выигрышных vs проигрышных — какой был MFE (упущенная прибыль)
w_mfe = [r.get("mfe_pct") for r in rows if (r.get("net_pnl",0) or 0)>0 and r.get("mfe_pct") is not None]
l_mfe = [r.get("mfe_pct") for r in rows if (r.get("net_pnl",0) or 0)<=0 and r.get("mfe_pct") is not None]
if l_mfe:
    print(f"MFE проигрышных (упущено перед разворотом): avg={sum(l_mfe)/len(l_mfe):.3f} "
          f"max={max(l_mfe):.3f}")
if w_mfe:
    print(f"MFE выигрышных: avg={sum(w_mfe)/len(w_mfe):.3f}")

print("\n==== ДИАПАЗОН ДАТ ====")
dates = [r.get("opened_at") for r in rows if r.get("opened_at")]
if dates:
    print(f"от {min(dates)} до {max(dates)}")
