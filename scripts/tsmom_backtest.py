# -*- coding: utf-8 -*-
"""
Time-Series Momentum (TSMOM) на дневных свечах (data/klines/*_1d.csv).

Академически устойчивый эффект (сильнее кросс-секционного). Проверяем ЧЕСТНО
на нашей вселенной:
  сигнал  = знак прошлой доходности за L дней (close[t-1]/close[t-1-L]-1);
  сайзинг = обратно волатильности (target_vol / realized_vol), cap;
  ребаланс= ежедневный; комиссия берётся с ИЗМЕНЕНИЯ позиции (|Δw|*fee one-way);
  портфель= равновзвешенный по активным монетам.
Walk-forward: годовая доходность/Sharpe по третям истории — плюс должен быть не
в одной трети, иначе это один бычий период, а не эдж.

Запуск: PYTHONIOENCODING=utf-8 python scripts/tsmom_backtest.py
"""
import csv
from pathlib import Path
from statistics import mean, pstdev

KL = Path(__file__).resolve().parent.parent / "data" / "klines"
FEE_ONEWAY = 0.0005      # taker одной стороны
TARGET_VOL = 0.02        # целевая дневная волатильность на монету (2%)
VOL_WIN = 30             # окно realized vol
POS_CAP = 2.0            # макс плечо-вес на монету
WARMUP_EXTRA = 5


def load_closes(path):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append((int(r["time_ms"]), float(r["close"])))
    rows.sort()
    return rows


def returns(closes):
    return [None] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]


def realized_vol(rets, i, win):
    seg = [r for r in rets[max(1, i - win):i] if r is not None]
    if len(seg) < win // 2:
        return None
    return pstdev(seg) or None


def backtest(data, L, mode, vol_target=True):
    """data: {sym: (times[], closes[], rets[])}. Возвращает список дневных net-доходностей
    портфеля (после комиссий) и их временные доли."""
    # общий временной индекс по объединению дат
    all_times = sorted({t for sym in data for t in data[sym][0]})
    idx = {sym: {t: k for k, t in enumerate(data[sym][0])} for sym in data}
    prev_w = {sym: 0.0 for sym in data}
    out = []
    start = L + max(VOL_WIN, L) + WARMUP_EXTRA
    for di, t in enumerate(all_times):
        if di < start:
            continue
        contribs = []
        day_cost = 0.0
        active = 0
        for sym, (times, closes, rets) in data.items():
            k = idx[sym].get(t)
            if k is None or k < start:
                # нет данных по монете на эту дату — закрываем её позицию (стоимость)
                if prev_w[sym] != 0.0:
                    day_cost += abs(prev_w[sym]) * FEE_ONEWAY
                    prev_w[sym] = 0.0
                continue
            # сигнал по инфо на закрытии предыдущего дня (k-1)
            if k - 1 - L < 0 or closes[k - 1 - L] <= 0:
                continue
            past = closes[k - 1] / closes[k - 1 - L] - 1
            sig = 1.0 if past > 0 else (-1.0 if past < 0 else 0.0)
            if mode == "long_flat" and sig < 0:
                sig = 0.0
            if vol_target:
                rv = realized_vol(rets, k, VOL_WIN)
                size = min(POS_CAP, TARGET_VOL / rv) if rv else 0.0
            else:
                size = 1.0
            w = sig * size
            day_cost += abs(w - prev_w[sym]) * FEE_ONEWAY
            prev_w[sym] = w
            r = rets[k]
            if r is not None:
                contribs.append(w * r)
                active += 1
        if active == 0:
            continue
        gross = mean(contribs)
        net = gross - day_cost / active
        out.append((di / len(all_times), net))
    return out


def ann_stats(nets):
    if not nets:
        return None
    m = mean(nets); sd = pstdev(nets) or 1e-9
    return {"n": len(nets), "ann_ret": m * 365 * 100, "sharpe": m / sd * (365 ** 0.5),
            "sum": sum(nets) * 100, "wr": sum(1 for x in nets if x > 0) / len(nets) * 100}


def thirds(trades):
    b = [[], [], []]
    for fr, net in trades:
        b[min(2, int(fr * 3))].append(net)
    return [ann_stats(x) for x in b]


def main():
    files = sorted(KL.glob("*_1d.csv"))
    data = {}
    for p in files:
        sym = p.name.replace("_1d.csv", "")
        rows = load_closes(p)
        if len(rows) < 200:
            continue
        times = [t for t, _ in rows]; closes = [c for _, c in rows]
        data[sym] = (times, closes, returns(closes))
    print("=" * 92)
    print(f"TSMOM дневной | монет: {len(data)} | vol-target {TARGET_VOL:.0%}/день, cap {POS_CAP}x, "
          f"комиссия {FEE_ONEWAY*100:.2f}%/сторону")
    print("годовая дох-ть и Sharpe; трети = годовая дох-ть по 1/2/3 периоду (плюс во всех = эдж)")
    print("=" * 92)
    for mode in ("long_short", "long_flat"):
        print(f"\n### режим {mode} ###")
        print(f"  {'L(дн)':>6} | {'ann%':>7} {'Sharpe':>6} {'WR%':>5} | трети ann%: T1 / T2 / T3")
        for L in (5, 10, 15, 20, 30, 45, 60, 90):
            trades = backtest(data, L, mode)
            s = ann_stats([net for _, net in trades])
            th = thirds(trades)
            if not s:
                continue
            tline = " / ".join(f"{t['ann_ret']:+6.0f}" if t else "   -" for t in th)
            print(f"  {L:>6} | {s['ann_ret']:+7.1f} {s['sharpe']:>6.2f} {s['wr']:>5.1f} | {tline}")


if __name__ == "__main__":
    main()
