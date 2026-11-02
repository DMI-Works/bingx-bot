"""
Батарея кандидатных ВХОДОВ на исторических свечах (data/klines/*.csv от
fetch_klines.py). Отвечает на два вопроса:
  1. Есть ли хоть один вход с ПОЛОЖИТЕЛЬНЫМ ожиданием ПОСЛЕ комиссий?
  2. На КАКИХ монетах он прибылен (а какие торговать вслепую не стоит)?

Принципы честности:
  - всё считается в % движения ЦЕНЫ за вычетом round-trip комиссии (FEE_PCT);
    это не зависит от плеча (плечо лишь масштабирует и прибыль, и риск).
  - walk-forward: первые IN_SAMPLE_FRAC истории — in-sample, остаток —
    out-of-sample (OOS). Заголовок — ВСЕГДА OOS: на нём нельзя "подогнать".
  - параметры входов фиксированы (не перебираются под данные) — иначе любой
    мусор "заработает" на истории.
  - выход единый бракет по ATR (SL n*ATR, TP m*ATR, тайм-стоп T баров),
    проверка внутрибарно по high/low, при двойном касании — считаем SL (хуже).
  - одна позиция на монету за раз (нет наложения сделок).

Запуск:
  python scripts/backtest_entries.py                 # 15m
  python scripts/backtest_entries.py --interval 5m
"""
import argparse
import csv
from collections import defaultdict
from pathlib import Path

KL_DIR = Path(__file__).resolve().parent.parent / "data" / "klines"
FEE_PCT = 0.10          # round-trip taker (2 * 0.05%) в % цены
IN_SAMPLE_FRAC = 0.60
TIME_STOP_BARS = 16     # 4ч на 15m
WARMUP = 50


# ---------- загрузка ----------

def load(path):
    bars = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            bars.append({
                "t": int(r["time_ms"]),
                "o": float(r["open"]), "h": float(r["high"]),
                "l": float(r["low"]), "c": float(r["close"]),
                "v": float(r["volume"] or 0),
            })
    bars.sort(key=lambda b: b["t"])
    return bars


# ---------- индикаторы ----------

def ema(vals, period):
    out = [None] * len(vals)
    k = 2 / (period + 1)
    e = None
    for i, v in enumerate(vals):
        e = v if e is None else v * k + e * (1 - k)
        out[i] = e
    return out


def atr(bars, period=14):
    out = [None] * len(bars)
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            tr = b["h"] - b["l"]
        else:
            pc = bars[i - 1]["c"]
            tr = max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc))
        trs.append(tr)
        if i >= period:
            out[i] = sum(trs[i - period + 1:i + 1]) / period
    return out


def rsi(closes, period=14):
    out = [None] * len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
        if i >= period:
            ag = sum(gains[i - period:i]) / period
            al = sum(losses[i - period:i]) / period
            out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def sma(vals, period, i):
    return sum(vals[i - period + 1:i + 1]) / period


def std(vals, period, i, mean):
    var = sum((vals[j] - mean) ** 2 for j in range(i - period + 1, i + 1)) / period
    return var ** 0.5


# ---------- сигналы входа (возвращают 'LONG'/'SHORT'/None на закрытии бара i) ----------

def sig_donchian(bars, ctx, i, N=20):
    if i < N:
        return None
    hi = max(b["h"] for b in bars[i - N:i])
    lo = min(b["l"] for b in bars[i - N:i])
    if bars[i]["c"] > hi:
        return "LONG"
    if bars[i]["c"] < lo:
        return "SHORT"
    return None


def sig_ema_cross(bars, ctx, i, fast=9, slow=21):
    ef, es = ctx["ema_f"], ctx["ema_s"]
    if i < slow + 1 or ef[i - 1] is None:
        return None
    if ef[i - 1] <= es[i - 1] and ef[i] > es[i]:
        return "LONG"
    if ef[i - 1] >= es[i - 1] and ef[i] < es[i]:
        return "SHORT"
    return None


def sig_meanrev_z(bars, ctx, i, N=20, z=2.0):
    if i < N:
        return None
    closes = ctx["closes"]
    m = sma(closes, N, i)
    s = std(closes, N, i, m)
    if s == 0:
        return None
    zc = (closes[i] - m) / s
    if zc >= z:
        return "SHORT"   # перелёт вверх -> фейд
    if zc <= -z:
        return "LONG"
    return None


def sig_rsi_rev(bars, ctx, i, lo=30, hi=70):
    r = ctx["rsi"]
    if r[i] is None:
        return None
    if r[i] < lo:
        return "LONG"
    if r[i] > hi:
        return "SHORT"
    return None


def sig_donchian_regime(bars, ctx, i, N=20):
    """Пробой Дончиана С ФИЛЬТРОМ РЕЖИМА: только по направлению медленного
    тренда (EMA100) и только при расширении волатильности (ATR14 > ATR50).
    Гипотеза: пробои работают лишь в трендовом/волатильном режиме."""
    base = sig_donchian(bars, ctx, i, N)
    if not base:
        return None
    es, a, a_slow = ctx["ema_slow"][i], ctx["atr"][i], ctx["atr_slow"][i]
    if es is None or a is None or a_slow is None or a <= a_slow:
        return None
    if base == "LONG" and bars[i]["c"] > es:
        return "LONG"
    if base == "SHORT" and bars[i]["c"] < es:
        return "SHORT"
    return None


def sig_atr_impulse(bars, ctx, i, k=1.0, vol_mult=1.5):
    a = ctx["atr"][i]
    if a is None or i < 20:
        return None
    body = bars[i]["c"] - bars[i]["o"]
    avg_v = sum(b["v"] for b in bars[i - 20:i]) / 20
    if avg_v <= 0 or abs(body) < k * a or bars[i]["v"] < vol_mult * avg_v:
        return None
    return "LONG" if body > 0 else "SHORT"


SIGNALS = {
    "donchian20": sig_donchian,
    "donch_regime": sig_donchian_regime,
    "ema9_21": sig_ema_cross,
    "meanrev_z2": sig_meanrev_z,
    "rsi_rev": sig_rsi_rev,
    "atr_impulse": sig_atr_impulse,
}


# ---------- симуляция выхода ----------

def simulate_trade(bars, ctx, i, side, sl_atr, tp_atr):
    """Бракет по ATR от закрытия бара i. Возвращает (net_pct, exit_index)."""
    a = ctx["atr"][i]
    if a is None or a <= 0:
        return None
    entry = bars[i]["c"]
    sign = 1 if side == "LONG" else -1
    sl = entry - sign * sl_atr * a
    tp = entry + sign * tp_atr * a
    end = min(i + TIME_STOP_BARS, len(bars) - 1)
    exit_price = bars[end]["c"]
    exit_idx = end
    for j in range(i + 1, end + 1):
        hi, lo = bars[j]["h"], bars[j]["l"]
        if side == "LONG":
            if lo <= sl:   # стоп первым при двойном касании
                exit_price, exit_idx = sl, j; break
            if hi >= tp:
                exit_price, exit_idx = tp, j; break
        else:
            if hi >= sl:
                exit_price, exit_idx = sl, j; break
            if lo <= tp:
                exit_price, exit_idx = tp, j; break
    net = sign * (exit_price - entry) / entry * 100.0 - FEE_PCT
    return net, exit_idx


def run_signal(bars, ctx, sig_fn, sl_atr, tp_atr, split_idx):
    """Прогон одного сигнала по одной монете. Нет наложения сделок.
    Возвращает (in_sample_results, oos_results) — списки net% сделок."""
    is_res, oos_res = [], []
    free_from = WARMUP
    for i in range(WARMUP, len(bars) - 1):
        if i < free_from:
            continue
        side = sig_fn(bars, ctx, i)
        if not side:
            continue
        out = simulate_trade(bars, ctx, i, side, sl_atr, tp_atr)
        if out is None:
            continue
        net, exit_idx = out
        free_from = exit_idx + 1
        (is_res if i < split_idx else oos_res).append(net)
    return is_res, oos_res


def run_signal_fracs(bars, ctx, sig_fn, sl_atr, tp_atr):
    """Как run_signal, но возвращает [(frac, net)] где frac = позиция входа
    во времени (0..1). Для проверки устойчивости эджа по периодам."""
    out_list = []
    free_from = WARMUP
    n = len(bars)
    for i in range(WARMUP, n - 1):
        if i < free_from:
            continue
        side = sig_fn(bars, ctx, i)
        if not side:
            continue
        out = simulate_trade(bars, ctx, i, side, sl_atr, tp_atr)
        if out is None:
            continue
        net, exit_idx = out
        free_from = exit_idx + 1
        out_list.append((i / n, net))
    return out_list


# ---------- статистика ----------

def stat(res):
    n = len(res)
    if not n:
        return None
    wins = [x for x in res if x > 0]
    losses = [x for x in res if x <= 0]
    gl = -sum(losses)
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    return {
        "n": n, "wr": len(wins) / n, "avg": sum(res) / n, "sum": sum(res),
        "payoff": (aw / -al) if al < 0 else float("inf"),
        "pf": (sum(wins) / gl) if gl > 0 else float("inf"),
    }


def f(x, d=3):
    return "∞" if x == float("inf") else f"{x:,.{d}f}"


# ---------- контекст индикаторов ----------

def build_ctx(bars):
    closes = [b["c"] for b in bars]
    return {
        "closes": closes,
        "atr": atr(bars, 14),
        "rsi": rsi(closes, 14),
        "ema_f": ema(closes, 9),
        "ema_s": ema(closes, 21),
        "ema_slow": ema(closes, 100),
        "atr_slow": atr(bars, 50),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="15m")
    args = ap.parse_args()

    files = sorted(KL_DIR.glob(f"*_{args.interval}.csv"))
    if not files:
        print(f"Нет данных в {KL_DIR} для интервала {args.interval}. Запусти fetch_klines.py.")
        return

    data = {}
    for path in files:
        sym = path.name.replace(f"_{args.interval}.csv", "")
        bars = load(path)
        if len(bars) > WARMUP + 40:
            data[sym] = (bars, build_ctx(bars))

    print("=" * 100)
    print(f"БЭКТЕСТ ВХОДОВ   интервал {args.interval}   монет: {len(data)}   комиссия {FEE_PCT}% на сделку")
    print(f"Всё в % ЦЕНЫ после комиссий. Заголовок — OUT-OF-SAMPLE (последние {100*(1-IN_SAMPLE_FRAC):.0f}% истории).")
    print("=" * 100)

    EXITS = [(1.0, 1.5), (1.0, 2.5), (1.5, 2.0)]

    # агрегат по сигналам для каждого пресета выхода
    best_combo = None  # (oos_avg, sig_name, exit) для пер-монетного разбора
    for sl_atr, tp_atr in EXITS:
        print(f"\n### Выход: SL={sl_atr}·ATR  TP={tp_atr}·ATR  (тайм-стоп {TIME_STOP_BARS} баров) ###")
        print(f"  {'сигнал':<14} | {'OOS: n':>6} {'WR':>4} {'payoff':>7} {'expect%':>8} {'сумма%':>8} "
              f"| {'IS: n':>6} {'expect%':>8}")
        rows = []
        for name, fn in SIGNALS.items():
            all_is, all_oos = [], []
            for sym, (bars, ctx) in data.items():
                split_idx = int(len(bars) * IN_SAMPLE_FRAC)
                defn = lambda b, c, i, _fn=fn: _fn(b, c, i)
                is_r, oos_r = run_signal(bars, ctx, defn, sl_atr, tp_atr, split_idx)
                all_is += is_r
                all_oos += oos_r
            s_oos = stat(all_oos)
            s_is = stat(all_is)
            if not s_oos:
                continue
            rows.append((s_oos["avg"], name, (sl_atr, tp_atr), s_oos))
            print(f"  {name:<14} | {s_oos['n']:>6} {s_oos['wr']:>4.0%} {f(s_oos['payoff'],2):>7} "
                  f"{f(s_oos['avg']):>8} {f(s_oos['sum'],1):>8} "
                  f"| {s_is['n'] if s_is else 0:>6} {f(s_is['avg']) if s_is else '—':>8}")
        rows.sort(reverse=True)
        if rows and (best_combo is None or rows[0][0] > best_combo[0]):
            best_combo = rows[0]

    # пер-монетный разбор лучшей комбинации (сигнал+выход) — это ответ "какие монеты торговать"
    if best_combo:
        oos_avg, name, (sl_atr, tp_atr), _ = best_combo
        fn = SIGNALS[name]
        print("\n" + "=" * 100)
        print(f"ПЕР-МОНЕТНО для лучшей связки: вход={name}  выход SL={sl_atr}·ATR TP={tp_atr}·ATR")
        print(f"(OOS, последние {100*(1-IN_SAMPLE_FRAC):.0f}% истории; + в плюсе = монету стоит торговать этим входом)")
        print("=" * 100)
        print(f"  {'монета':<16} {'n':>4} {'WR':>4} {'payoff':>7} {'expect%':>8} {'сумма%':>8}")
        per = []
        for sym, (bars, ctx) in data.items():
            split_idx = int(len(bars) * IN_SAMPLE_FRAC)
            defn = lambda b, c, i, _fn=fn: _fn(b, c, i)
            _, oos_r = run_signal(bars, ctx, defn, sl_atr, tp_atr, split_idx)
            s = stat(oos_r)
            if s:
                per.append((s["avg"], sym, s))
        per.sort(reverse=True)
        for avg, sym, s in per:
            mark = "  <== плюс" if s["avg"] > 0 and s["n"] >= 5 else ""
            print(f"  {sym:<16} {s['n']:>4} {s['wr']:>4.0%} {f(s['payoff'],2):>7} "
                  f"{f(s['avg']):>8} {f(s['sum'],1):>8}{mark}")
        pos = [sym for avg, sym, s in per if s["avg"] > 0 and s["n"] >= 5]
        print(f"\n  Монет с положительным OOS-ожиданием (n>=5): {len(pos)}/{len(per)}")
        if pos:
            print("  " + ", ".join(pos))

        # --- устойчивость по времени: трети истории ---
        # если эдж есть только в одной трети — это регим, а не эдж.
        print("\n" + "-" * 100)
        print("УСТОЙЧИВОСТЬ ПО ВРЕМЕНИ (3 равные трети истории, expect%/сделку после комиссий)")
        print("Если плюс только в одной трети — это трендовый период, а не стабильный эдж.")
        print("-" * 100)

        def thirds(symbols):
            buckets = [[], [], []]
            for sym in symbols:
                bars, ctx = data[sym]
                for frac, net in run_signal_fracs(bars, ctx, fn, sl_atr, tp_atr):
                    buckets[min(2, int(frac * 3))].append(net)
            return buckets

        for label, syms in [("ВСЕ монеты", list(data)), ("только плюсовые монеты", pos)]:
            if not syms:
                continue
            b = thirds(syms)
            parts = []
            for idx, res in enumerate(b):
                s = stat(res)
                parts.append(f"T{idx+1}: n={s['n']:>4} expect={f(s['avg']):>7}" if s else f"T{idx+1}: —")
            print(f"  {label:<24} | " + " | ".join(parts))


if __name__ == "__main__":
    main()
