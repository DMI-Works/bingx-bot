"""
Разовый разбор закрытых сделок за последние 24 часа из trade_analytics.
Только чтение. Запуск: python scripts/analyze_24h.py
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta

from dotenv import load_dotenv

load_dotenv()

from pymongo import MongoClient

MONGO_URI = os.environ["MONGO_URI"]
BASE = os.getenv("MONGO_DB_NAME") or "trading_bot"

client = MongoClient(MONGO_URI)

CUTOFF = datetime.utcnow() - timedelta(hours=24)


def fnum(x, d=2):
    return f"{x:,.{d}f}" if isinstance(x, (int, float)) else str(x)


def analyze(db_name, mode):
    db = client[db_name]
    if "trade_analytics" not in db.list_collection_names():
        return None
    rows = list(db.trade_analytics.find({
        "status": "CLOSED",
        "closed_at": {"$gte": CUTOFF},
    }).sort("closed_at", 1))
    if not rows:
        return {"db": db_name, "mode": mode, "n": 0}

    wins = losses = be = 0
    gp = gl = 0.0
    total_net = 0.0
    total_comm = 0.0
    total_funding = 0.0
    by_strat = defaultdict(lambda: {"w": 0, "l": 0, "be": 0, "pnl": 0.0, "gp": 0.0, "gl": 0.0})
    by_symbol = defaultdict(float)
    by_reason = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    r_multiples = []
    slippages = []
    sl_move_counts = []
    durations = []
    best = worst = None
    worst_trades = []

    for r in rows:
        net = r.get("net_pnl")
        if net is None:
            net = r.get("realized_pnl") or 0.0
        total_net += net
        total_comm += r.get("commission_usdt") or 0.0
        total_funding += r.get("funding_fee") or 0.0
        strat = r.get("strategy") or "Неизвестно"
        sym = r.get("symbol")
        reason = r.get("close_reason") or "?"

        if net > 0:
            wins += 1; gp += net; by_strat[strat]["w"] += 1; by_strat[strat]["gp"] += net
        elif net < 0:
            losses += 1; gl += -net; by_strat[strat]["l"] += 1; by_strat[strat]["gl"] += -net
        else:
            be += 1; by_strat[strat]["be"] += 1
        by_strat[strat]["pnl"] += net
        by_symbol[sym] += net
        by_reason[reason]["n"] += 1
        by_reason[reason]["pnl"] += net

        if r.get("r_multiple") is not None:
            r_multiples.append(r["r_multiple"])
        if r.get("stop_slippage_pct") is not None:
            slippages.append(r["stop_slippage_pct"])
        if r.get("sl_moves_count") is not None:
            sl_move_counts.append(r["sl_moves_count"])
        if r.get("duration_seconds") is not None:
            durations.append(r["duration_seconds"])

        best = net if best is None else max(best, net)
        worst = net if worst is None else min(worst, net)
        worst_trades.append((net, sym, strat, reason, r.get("stop_distance_pct"),
                             r.get("sl_moves_count"), r.get("duration_seconds"),
                             r.get("r_multiple")))

    n = wins + losses + be
    avg_win = gp / wins if wins else None
    avg_loss = gl / losses if losses else None
    payoff = (avg_win / avg_loss) if (avg_win and avg_loss) else None
    pf = (gp / gl) if gl > 0 else None
    expectancy = total_net / n if n else 0.0

    print("=" * 70)
    print(f"БАЗА: {db_name}   режим: {mode}")
    print(f"Окно: с {CUTOFF:%Y-%m-%d %H:%M} по {datetime.utcnow():%Y-%m-%d %H:%M} UTC (24ч)")
    print("=" * 70)
    print(f"Всего закрыто:       {n}")
    print(f"  Плюсовых:          {wins}")
    print(f"  Минусовых:         {losses}")
    print(f"  В безубыток (0):   {be}")
    print(f"Win-rate:            {round(wins/n*100,1) if n else 0}%")
    print(f"ИТОГО net PnL:       {fnum(total_net)} USDT")
    print(f"  Комиссии:          {fnum(total_comm)} USDT")
    print(f"  Funding:           {fnum(total_funding)} USDT")
    print(f"Валовый профит:      {fnum(gp)}")
    print(f"Валовый убыток:      {fnum(gl)}")
    print(f"Profit factor:       {fnum(pf) if pf is not None else '∞ (нет убытков)'}")
    print(f"Средний плюс:        {fnum(avg_win) if avg_win else '—'}")
    print(f"Средний минус:       {fnum(avg_loss) if avg_loss else '—'}")
    print(f"Payoff ratio:        {fnum(payoff) if payoff else '—'}  (ср.плюс / ср.минус)")
    print(f"Expectancy/сделку:   {fnum(expectancy)} USDT")
    print(f"Лучшая сделка:       {fnum(best)}")
    print(f"Худшая сделка:       {fnum(worst)}")
    if r_multiples:
        print(f"Средний R-multiple:  {fnum(sum(r_multiples)/len(r_multiples))}  (по {len(r_multiples)} сделкам с планом риска)")
    if sl_move_counts:
        print(f"Ср. переносов SL:    {fnum(sum(sl_move_counts)/len(sl_move_counts))}")
    if durations:
        print(f"Ср. длит. сделки:    {fnum(sum(durations)/len(durations)/60,1)} мин")
    if slippages:
        print(f"Ср. проскальзыв. SL: {fnum(sum(slippages)/len(slippages),3)}% (>0 хуже стопа)")

    print("\n--- По причинам закрытия ---")
    for reason, d in sorted(by_reason.items(), key=lambda kv: kv[1]["pnl"]):
        print(f"  {reason:<24} {d['n']:>3} шт   PnL {fnum(d['pnl']):>12}")

    print("\n--- По стратегиям ---")
    for name, d in sorted(by_strat.items(), key=lambda kv: kv[1]["pnl"]):
        spf = (d["gp"]/d["gl"]) if d["gl"] > 0 else None
        print(f"  {name:<24} W{d['w']} L{d['l']} BE{d['be']}  "
              f"PnL {fnum(d['pnl']):>10}  PF {fnum(spf) if spf else '∞'}")

    print("\n--- По монетам (топ по модулю PnL) ---")
    for sym, v in sorted(by_symbol.items(), key=lambda kv: abs(kv[1]), reverse=True)[:15]:
        print(f"  {sym:<18} {fnum(v):>12}")

    print("\n--- 10 худших сделок ---")
    print(f"  {'PnL':>9}  {'символ':<14} {'стратегия':<20} {'причина':<16} {'SLdist%':>7} {'SLmov':>5} {'мин':>6} {'R':>6}")
    for net, sym, strat, reason, sldist, slmov, dur, rmult in sorted(worst_trades)[:10]:
        print(f"  {fnum(net):>9}  {str(sym):<14} {str(strat):<20} {str(reason):<16} "
              f"{fnum(sldist) if sldist is not None else '—':>7} "
              f"{slmov if slmov is not None else '—':>5} "
              f"{fnum(dur/60,1) if dur is not None else '—':>6} "
              f"{fnum(rmult) if rmult is not None else '—':>6}")
    return {"db": db_name, "mode": mode, "n": n, "net": total_net}


# testnet=true в конфиге -> основная база trading_bot_testnet. Проверим обе.
found_any = False
for db_name, mode in [(f"{BASE}_testnet", "testnet"), (BASE, "live")]:
    res = analyze(db_name, mode)
    if res and res.get("n", 0) > 0:
        found_any = True
        print()

if not found_any:
    print("За последние 24 часа закрытых сделок не найдено ни в testnet, ни в live базе.")
    # Подскажем, когда была последняя закрытая сделка
    for db_name in [f"{BASE}_testnet", BASE]:
        db = client[db_name]
        if "trade_analytics" in db.list_collection_names():
            last = list(db.trade_analytics.find({"status": "CLOSED"}).sort("closed_at", -1).limit(1))
            if last:
                print(f"  {db_name}: последняя закрытая сделка {last[0].get('closed_at')} "
                      f"net_pnl={last[0].get('net_pnl')}")

client.close()
