# -*- coding: utf-8 -*-
"""
TsMomentum: сетка «стоп + фиксация прибыли» на дневных свечах, портфельно.

В отличие от tsmom_backtest.py (ежедневный ребаланс весов) здесь торговля как
у бота: вход по открытию дня в сторону знака доходности за L дней, стоп
k*ATR, выход по стопу или развороту сигнала, перенос стопа в плюс и трейл —
в кратных дневного ATR на момент входа. Портфель: не больше max_pos позиций,
при нехватке слотов — самые сильные |momentum|; риск r% equity на сделку
(размер = риск / дистанция стопа), equity компаундится.

Консервативно: стоп проверяется раньше нового пика в той же свече; новый стоп
действует со следующего дня; гэп через стоп исполняется по открытию.
Фандинг не учитывается.

Данные: data/klines/*_1d.csv (python scripts/fetch_klines.py --symbols ... --interval 1d --days 1500)
Запуск: python scripts/tsmom_exit_grid.py
"""
import csv
import itertools
from pathlib import Path

KL = Path(__file__).resolve().parent.parent / "data" / "klines"
FEE = 0.0005 * 2 + 0.0004   # taker туда-обратно + проскальзывание
ATR_N = 14


def load():
    data = {}
    for p in sorted(KL.glob("*_1d.csv")):
        rows = []
        with open(p, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append((int(r["time_ms"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
        rows.sort()
        data[p.stem.replace("_1d", "")] = rows
    return data


def atr_series(rows):
    out = [None] * len(rows)
    trs = []
    for i in range(1, len(rows)):
        h, l, pc = rows[i][2], rows[i][3], rows[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if len(trs) >= ATR_N:
            out[i] = sum(trs[-ATR_N:]) / ATR_N
    return out


def simulate(data, L=60, stop_atr=3.0, lock=None, trail=None, risk=0.02, max_pos=4,
             pause_days=0, min_mom=0.0, start_ms=None, pause_after_lock_days=0):
    """lock=(trigger_atr, stop_atr_profit), trail=(start_atr, gap_atr) — от входа, в ATR.
    pause_days — пауза после стопа в минус/ноль, pause_after_lock_days — после стопа в прибыль."""
    times = sorted({r[0] for rows in data.values() for r in rows})
    idx = {s: {r[0]: i for i, r in enumerate(rows)} for s, rows in data.items()}
    atrs = {s: atr_series(rows) for s, rows in data.items()}
    eq = 1.0
    peak_eq, max_dd = 1.0, 0.0
    pos = {}       # sym -> dict
    paused = {}    # sym -> time до которого нельзя входить
    trades = []
    yearly = {}
    for t in times:
        if start_ms and t < start_ms:
            continue
        # 1) сигналы на сегодня (по закрытым свечам до t)
        sig = {}
        for s, rows in data.items():
            i = idx[s].get(t)
            if i is None or i < L + ATR_N + 2:
                continue
            mom = rows[i - 1][4] / rows[i - 1 - L][4] - 1
            a = atrs[s][i - 1]
            if a:
                sig[s] = (mom, a)
        # 2) управление открытыми
        for s in list(pos):
            i = idx[s].get(t)
            if i is None:
                continue
            o, h, l, c = data[s][i][1:]
            p = pos[s]
            sg = p["sg"]
            exit_px = reason = None
            mom = sig.get(s, (0, 0))[0]
            want = 1 if mom > min_mom else (-1 if mom < -min_mom else 0)
            if want != sg:
                exit_px, reason = o, "flip"
            elif (l <= p["stop"] if sg > 0 else h >= p["stop"]):
                exit_px = (min(o, p["stop"]) if sg > 0 else max(o, p["stop"]))
                reason = "stop"
            if exit_px is not None:
                r_mult = ((exit_px / p["e"] - 1) * sg - FEE) / p["dist"]
                pnl = p["risk_eq"] * r_mult
                eq += pnl
                trades.append((r_mult, reason, t - p["t"]))
                y = str(__import__("datetime").datetime.utcfromtimestamp(t / 1000).year)
                yearly[y] = yearly.get(y, 0) + pnl
                del pos[s]
                if reason == "stop" and r_mult <= 0.1:
                    paused[s] = t + pause_days * 86_400_000
                elif reason == "stop" and pause_after_lock_days:
                    # закрылись по фиксации прибыли: не перезаходить сразу в растянутый тренд
                    paused[s] = t + pause_after_lock_days * 86_400_000
                continue
            # пик и новый стоп на завтра
            fav = h if sg > 0 else l
            move_atr = (fav / p["e"] - 1) * sg * p["e"] / p["a"]
            p["peak"] = max(p["peak"], move_atr)
            new = None
            if lock and p["peak"] >= lock[0]:
                new = lock[1]
            if trail and p["peak"] >= trail[0]:
                new = max(new if new is not None else -99, p["peak"] - trail[1])
            if new is not None:
                cand = p["e"] + sg * new * p["a"]
                if (cand - p["stop"]) * sg > 0:
                    p["stop"] = cand
        # 3) входы
        free = max_pos - len(pos)
        if free > 0:
            cands = []
            for s, (mom, a) in sig.items():
                if s in pos or t < paused.get(s, 0) or abs(mom) <= min_mom:
                    continue
                cands.append((abs(mom), s, mom, a))
            for _, s, mom, a in sorted(cands, reverse=True)[:free]:
                i = idx[s][t]
                o = data[s][i][1]
                sg = 1 if mom > 0 else -1
                dist = stop_atr * a / o
                pos[s] = {"sg": sg, "e": o, "a": a, "dist": dist, "stop": o - sg * stop_atr * a,
                          "peak": 0.0, "risk_eq": eq * risk, "t": t}
        # просадка по закрытому equity
        peak_eq = max(peak_eq, eq)
        max_dd = max(max_dd, 1 - eq / peak_eq)
    n = len(trades)
    wins = sum(1 for r, *_ in trades if r > 0)
    days = (times[-1] - (start_ms or times[0])) / 86_400_000
    cagr = eq ** (365 / days) - 1 if eq > 0 else -1
    return {"eq": eq, "cagr": cagr, "dd": max_dd, "n": n, "wr": wins / max(n, 1),
            "avg_r": sum(r for r, *_ in trades) / max(n, 1),
            "hold_d": sum(d for *_, d in trades) / max(n, 1) / 86_400_000, "yearly": yearly}


def fmt(name, r):
    yr = " ".join(f"{k}:{v * 100:+.0f}%" for k, v in sorted(r["yearly"].items()))
    return (f"{name:44s} x{r['eq']:6.2f} CAGR={r['cagr'] * 100:+6.1f}% DD={r['dd'] * 100:5.1f}% "
            f"n={r['n']:4d} WR={r['wr'] * 100:3.0f}% avgR={r['avg_r']:+.2f} hold={r['hold_d']:4.1f}d | {yr}")


def main():
    data = load()
    print(f"монет: {len(data)}; FEE={FEE}; риск 2%/сделку, max 4 позиции, L=60\n")
    print(fmt("ТЕКУЩИЙ LIVE ≈ стоп 3% цены→ ~0.6ATR, BE 0.3ATR", simulate(data, stop_atr=0.6, lock=(0.3, 0.1), trail=(0.6, 0.3))))
    print(fmt("только разворот, стоп 4 ATR", simulate(data, stop_atr=4)))
    for risk in (0.02, 0.03):
        print(fmt(f"ДЕФОЛТЫ бота (3ATR, трейл 12/7, паузы 1д/14д), риск {risk:.0%}",
                  simulate(data, stop_atr=3, trail=(12, 7), pause_days=1, pause_after_lock_days=14, risk=risk)))
    rows = []
    for k, lock, trail, pause in itertools.product(
        (2.0, 3.0, 4.0),
        (None, (2, 0.5), (3, 1), (4, 1.5), (4, 2), (6, 3)),
        (None, (5, 2.5), (6, 3), (8, 4), (10, 5)),
        (0, 1),
    ):
        r = simulate(data, stop_atr=k, lock=lock, trail=trail, pause_days=pause)
        rows.append((r["cagr"] / max(r["dd"], 0.05), k, lock, trail, pause, r))
    rows.sort(key=lambda x: -x[0])
    print("\nТоп-15 по CAGR/MaxDD:")
    for score, k, lock, trail, pause, r in rows[:15]:
        print(fmt(f"stop={k} lock={lock} trail={trail} pause={pause}", r))
    best = rows[0]
    print("\nЛучший вариант при разном риске:")
    for risk in (0.01, 0.02, 0.03, 0.04, 0.05):
        r = simulate(data, stop_atr=best[1], lock=best[2], trail=best[3], pause_days=best[4], risk=risk)
        print(fmt(f"risk={risk * 100:.0f}%", r))


if __name__ == "__main__":
    main()
