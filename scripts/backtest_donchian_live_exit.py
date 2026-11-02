# -*- coding: utf-8 -*-
"""
Валидация БОЕВОГО выхода DonchianTrendStrategy на истории (data/klines/*_15m.csv).

Отличие от scripts/backtest_entries.py: там единый ATR-брекет + тайм-стоп. Здесь
воспроизводится РОВНО то, что будет в live:
  вход  = donch_regime (пробой Дончиана N + close по сторону EMA(ema) + ATR>ATR_slow);
  выход = частичный TP: 50% @ tp1·ATR, остаток @ tp2·ATR;
          после TP1 стоп переносится в безубыток (atr_3step шаг 1);
          ТАЙМ-СТОПА НЕТ (в live его нет) — сделка живёт до SL/TP/безубытка.

Всё в % ЦЕНЫ после round-trip комиссии. Walk-forward: заголовок — OOS (последние
40%). Тройки истории — проверка, что плюс не только в одном трендовом периоде.

Запуск: PYTHONIOENCODING=utf-8 python scripts/backtest_donchian_live_exit.py
"""
import argparse
import csv
from pathlib import Path

KL_DIR = Path(__file__).resolve().parent.parent / "data" / "klines"
FEE_PCT = 0.10
IN_SAMPLE_FRAC = 0.60
WARMUP = 120  # EMA100 + разгон

# параметры из плана
N_DONCH = 20
EMA_P = 100
ATR_P = 14
ATR_SLOW_P = 50
SL_ATR = 1.0
TP1_ATR = 1.5
TP2_ATR = 2.5
TP1_FRAC = 0.5


def load(path):
    bars = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            bars.append({"t": int(r["time_ms"]), "o": float(r["open"]), "h": float(r["high"]),
                         "l": float(r["low"]), "c": float(r["close"]), "v": float(r["volume"] or 0)})
    bars.sort(key=lambda b: b["t"])
    return bars


def ema(vals, period):
    out = [None] * len(vals); k = 2 / (period + 1); e = None
    for i, v in enumerate(vals):
        e = v if e is None else v * k + e * (1 - k); out[i] = e
    return out


def atr(bars, period):
    out = [None] * len(bars); trs = []
    for i, b in enumerate(bars):
        tr = b["h"] - b["l"] if i == 0 else max(b["h"] - b["l"], abs(b["h"] - bars[i-1]["c"]), abs(b["l"] - bars[i-1]["c"]))
        trs.append(tr)
        if i >= period:
            out[i] = sum(trs[i - period + 1:i + 1]) / period
    return out


def build_ctx(bars):
    closes = [b["c"] for b in bars]
    return {"ema": ema(closes, EMA_P), "atr": atr(bars, ATR_P), "atr_slow": atr(bars, ATR_SLOW_P)}


def signal(bars, ctx, i):
    """donch_regime на закрытии бара i -> 'LONG'/'SHORT'/None."""
    if i < max(N_DONCH, ATR_SLOW_P) or ctx["ema"][i] is None or ctx["atr"][i] is None or ctx["atr_slow"][i] is None:
        return None
    if ctx["atr"][i] <= ctx["atr_slow"][i]:
        return None  # нет расширения волатильности
    hi = max(b["h"] for b in bars[i - N_DONCH:i])
    lo = min(b["l"] for b in bars[i - N_DONCH:i])
    c = bars[i]["c"]
    if c > hi and c > ctx["ema"][i]:
        return "LONG"
    if c < lo and c < ctx["ema"][i]:
        return "SHORT"
    return None


def simulate(bars, ctx, i, side, mode="partial_be", time_stop=0):
    """Выход, воспроизводимый в live. mode:
      'partial_be' — 50%@1.5R + 50%@2.5R, безубыток после TP1 (план пользователя);
      'bracket'    — единый TP=2.5R, без частичного и без безубытка.
    time_stop>0 — закрыть по рынку через N баров (в live его НЕТ; 0 = выкл.).
    Возвращает (net%, exit_idx)."""
    a = ctx["atr"][i]
    if a is None or a <= 0:
        return None
    e = bars[i]["c"]; sign = 1 if side == "LONG" else -1
    R = a
    sl = e - sign * SL_ATR * R
    tp1 = e + sign * TP1_ATR * R
    tp2 = e + sign * TP2_ATR * R
    end = (min(i + time_stop, len(bars) - 1) if time_stop else len(bars) - 1)
    tp1_hit = False; h1 = 0.0
    for j in range(i + 1, end + 1):
        hi, lo = bars[j]["h"], bars[j]["l"]
        hit_sl = lo <= sl if side == "LONG" else hi >= sl
        if hit_sl:
            move = sign * (sl - e) / e * 100.0
            if mode == "partial_be" and tp1_hit:
                return TP1_FRAC * h1 + (1 - TP1_FRAC) * move - FEE_PCT, j
            return move - FEE_PCT, j
        if mode == "partial_be":
            if not tp1_hit:
                hit_tp1 = hi >= tp1 if side == "LONG" else lo <= tp1
                if hit_tp1:
                    tp1_hit = True; h1 = sign * (tp1 - e) / e * 100.0; sl = e; continue
            hit_tp2 = hi >= tp2 if side == "LONG" else lo <= tp2
            if hit_tp2:
                h2 = sign * (tp2 - e) / e * 100.0
                return TP1_FRAC * h1 + (1 - TP1_FRAC) * h2 - FEE_PCT, j
        else:  # bracket: единый TP=2.5R
            hit_tp = hi >= tp2 if side == "LONG" else lo <= tp2
            if hit_tp:
                return sign * (tp2 - e) / e * 100.0 - FEE_PCT, j
    last = bars[end]["c"]; move_last = sign * (last - e) / e * 100.0
    if mode == "partial_be" and tp1_hit:
        return TP1_FRAC * h1 + (1 - TP1_FRAC) * move_last - FEE_PCT, end
    return move_last - FEE_PCT, end


def run(bars, ctx, mode="partial_be", time_stop=0):
    """Без наложения сделок. Возвращает [(frac_time, net%)]."""
    out = []; free = WARMUP; n = len(bars)
    for i in range(WARMUP, n - 1):
        if i < free:
            continue
        side = signal(bars, ctx, i)
        if not side:
            continue
        res = simulate(bars, ctx, i, side, mode=mode, time_stop=time_stop)
        if res is None:
            continue
        net, exit_idx = res
        free = exit_idx + 1
        out.append((i / n, net))
    return out


def stat(res):
    n = len(res)
    if not n:
        return None
    wins = [x for x in res if x > 0]; losses = [x for x in res if x <= 0]
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    return {"n": n, "wr": len(wins) / n, "avg": sum(res) / n, "sum": sum(res),
            "payoff": (aw / -al) if al < 0 else float("inf")}


def fnum(x, d=3):
    return "inf" if x == float("inf") else f"{x:,.{d}f}"


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--interval", default="15m"); args = ap.parse_args()
    files = sorted(KL_DIR.glob(f"*_{args.interval}.csv"))
    if not files:
        print(f"Нет данных {args.interval} в {KL_DIR}. Запусти fetch_klines.py."); return

    data = {}
    for p in files:
        sym = p.name.replace(f"_{args.interval}.csv", "")
        bars = load(p)
        if len(bars) > WARMUP + 40:
            data[sym] = (bars, build_ctx(bars))

    print("=" * 96)
    print(f"DonchianTrend БОЕВОЙ ВЫХОД  {args.interval}  монет: {len(data)}  комиссия {FEE_PCT}%/сделку")
    print(f"вход donch_regime(N={N_DONCH},EMA{EMA_P},ATR{ATR_P}>ATR{ATR_SLOW_P}); "
          f"выход 50%@{TP1_ATR}R+50%@{TP2_ATR}R, безубыток после TP1, БЕЗ тайм-стопа; SL={SL_ATR}R")
    print("=" * 96)

    # Сравнение live-исполнимых выходов: план пользователя vs чистый брекет,
    # с тайм-стопом и без. В live тайм-стопа НЕТ (строки ts=0) — остальные для
    # понимания, насколько эдж держался именно на нём.
    print("\n### СРАВНЕНИЕ ВЫХОДОВ (OOS, последние 40%) ###")
    configs = [
        ("partial_be + БЕЗ ts (план)", "partial_be", 0),
        ("bracket TP2.5 + БЕЗ ts",     "bracket",    0),
        ("partial_be + ts=16 (4ч)",    "partial_be", 16),
        ("bracket TP2.5 + ts=16 (4ч)", "bracket",    16),
    ]
    split = IN_SAMPLE_FRAC
    for name, mode, ts in configs:
        all_oos, buckets = [], [[], [], []]
        for sym, (bars, ctx) in data.items():
            for fr, net in run(bars, ctx, mode=mode, time_stop=ts):
                if fr >= split:
                    all_oos.append(net)
                buckets[min(2, int(fr * 3))].append(net)
        s = stat(all_oos)
        thirds = " ".join(f"{fnum(stat(b)['avg']):>6}" if stat(b) else "  -" for b in buckets)
        if s:
            print(f"  {name:<28} n={s['n']:>4} WR={s['wr']:>4.0%} payoff={fnum(s['payoff'],2):>5} "
                  f"expect%={fnum(s['avg']):>7} сумма%={fnum(s['sum'],1):>7} | трети: {thirds}")

    # дальше — детальный разбор базового (боевого) конфига
    all_is, all_oos, per = [], [], []
    for sym, (bars, ctx) in data.items():
        trades = run(bars, ctx)
        is_r = [net for fr, net in trades if fr < split]
        oos_r = [net for fr, net in trades if fr >= split]
        all_is += is_r; all_oos += oos_r
        s = stat(oos_r)
        if s:
            per.append((s["avg"], sym, s))

    s_oos, s_is = stat(all_oos), stat(all_is)
    print("\n### АГРЕГАТ (partial_be, боевой) ###")
    for name, s in (("OOS (последние 40%)", s_oos), ("IN-SAMPLE", s_is)):
        if s:
            print(f"  {name:<22} n={s['n']:>4} WR={s['wr']:>4.0%} payoff={fnum(s['payoff'],2):>5} "
                  f"expect%={fnum(s['avg']):>7} сумма%={fnum(s['sum'],1):>8}")

    print("\n### ПО МОНЕТАМ (OOS, + = торговать) ###")
    per.sort(reverse=True)
    for avg, sym, s in per:
        mark = "  <== плюс" if s["avg"] > 0 and s["n"] >= 5 else ""
        print(f"  {sym:<16} n={s['n']:>3} WR={s['wr']:>4.0%} payoff={fnum(s['payoff'],2):>5} "
              f"expect%={fnum(s['avg']):>7} сумма%={fnum(s['sum'],1):>7}{mark}")
    pos = [sym for avg, sym, s in per if s["avg"] > 0 and s["n"] >= 5]
    print(f"\n  Плюсовых монет (n>=5): {len(pos)}/{len(per)}")

    print("\n### УСТОЙЧИВОСТЬ ПО ТРЕТЯМ (expect%/сделку) ###")
    buckets = [[], [], []]
    for sym, (bars, ctx) in data.items():
        for fr, net in run(bars, ctx):
            buckets[min(2, int(fr * 3))].append(net)
    parts = []
    for idx, res in enumerate(buckets):
        s = stat(res)
        parts.append(f"T{idx+1}: n={s['n']:>4} expect={fnum(s['avg']):>7}" if s else f"T{idx+1}: -")
    print("  " + " | ".join(parts))


if __name__ == "__main__":
    main()
