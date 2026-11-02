"""
Загрузчик исторических свечей BingX (perp, USDT-margined) для оффлайн-бэктеста.
Публичный endpoint /openApi/swap/v3/quote/klines (подпись НЕ нужна), данные —
ЖИВОЙ рынок (не testnet), т.к. нам нужен реальный ценовой ряд для исследования.

Что делает:
  1. Берёт 24h-статистику по всем контрактам (/quote/ticker), применяет те же
     фильтры, что и бот (объём >= min_volume_24h, не-крипто префиксы вон),
     ранжирует по 24h-диапазону (волатильность) и берёт топ-N "шумных" монет.
  2. Качает свечи за D дней на заданном интервале, страницами по 1000.
  3. Кладёт в data/klines/{SYMBOL}_{interval}.csv  (time_ms,open,high,low,close,volume)

Запуск:
  python scripts/fetch_klines.py                 # 50 монет, 15m, 30 дней
  python scripts/fetch_klines.py --interval 5m --days 30 --top 60
  python scripts/fetch_klines.py --symbols BTC-USDT,ETH-USDT --interval 15m
"""
import argparse
import asyncio
import csv
import os
import time
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()

HOST = "https://open-api.bingx.com"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "klines"
API_KEY = os.getenv("BINGX_API_KEY", "")

EXCLUDED_PREFIXES = ("NCFX", "NCCO", "NCSK", "NCSI")
MIN_VOLUME_24H = 20_000_000  # как trading.filters.min_volume_24h

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


def headers():
    return {"X-BX-APIKEY": API_KEY} if API_KEY else {}


async def get_json(session, path, params=None):
    async with session.get(f"{HOST}{path}", params=params or {}, headers=headers()) as r:
        return await r.json()


async def pick_universe(session, top, symbols_arg):
    if symbols_arg:
        return [s.strip() for s in symbols_arg.split(",") if s.strip()]

    resp = await get_json(session, "/openApi/swap/v2/quote/ticker")
    data = resp.get("data") or []
    rows = []
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("-USDT"):
            continue
        if any(sym.startswith(p) for p in EXCLUDED_PREFIXES):
            continue
        try:
            qvol = float(t.get("quoteVolume") or t.get("volume") or 0)
            high = float(t.get("highPrice") or 0)
            low = float(t.get("lowPrice") or 0)
            last = float(t.get("lastPrice") or 0)
        except (TypeError, ValueError):
            continue
        if qvol < MIN_VOLUME_24H or low <= 0 or last <= 0:
            continue
        rng_pct = (high - low) / low * 100.0  # 24h диапазон = "шумность"
        rows.append((rng_pct, qvol, sym))
    rows.sort(reverse=True)  # по волатильности
    chosen = [sym for _, _, sym in rows[:top]]
    print(f"Вселенная: {len(rows)} монет прошли фильтр (vol>={MIN_VOLUME_24H:,}), "
          f"берём топ-{len(chosen)} по 24h-диапазону:")
    for rng, qvol, sym in rows[:top]:
        print(f"  {sym:<16} диапазон {rng:5.1f}%  объём {qvol/1e6:7.0f}M")
    return chosen


async def fetch_symbol(session, sym, interval, days, sem):
    step = INTERVAL_MS[interval]
    now = int(time.time() * 1000)
    start = now - days * 86_400_000
    cursor = start
    bars = {}  # time_ms -> row (dedup)
    async with sem:
        while cursor < now:
            params = {"symbol": sym, "interval": interval, "limit": 1000, "startTime": cursor}
            try:
                resp = await get_json(session, "/openApi/swap/v3/quote/klines", params)
            except Exception as e:
                print(f"  ! {sym}: ошибка запроса {e}")
                break
            data = resp.get("data") or []
            if not data:
                break
            # нормализуем порядок
            data = sorted(data, key=lambda k: int(k.get("time", 0)))
            for k in data:
                t = int(k["time"])
                bars[t] = (t, k["open"], k["high"], k["low"], k["close"], k.get("volume", 0))
            last_t = int(data[-1]["time"])
            if last_t <= cursor:
                break
            cursor = last_t + step
            await asyncio.sleep(0.15)  # вежливо к rate-limit
    if not bars:
        print(f"  {sym:<16} пусто")
        return sym, 0
    rows = [bars[t] for t in sorted(bars)]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{sym}_{interval}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time_ms", "open", "high", "low", "close", "volume"])
        w.writerows(rows)
    print(f"  {sym:<16} {len(rows):>5} свечей -> {path.name}")
    return sym, len(rows)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="15m", choices=list(INTERVAL_MS))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--symbols", default="", help="явный список через запятую (обходит фильтр вселенной)")
    ap.add_argument("--concurrency", type=int, default=5)
    args = ap.parse_args()

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40)) as session:
        universe = await pick_universe(session, args.top, args.symbols)
        print(f"\nКачаю {args.interval}, {args.days} дней, {len(universe)} монет...")
        sem = asyncio.Semaphore(args.concurrency)
        results = await asyncio.gather(*[
            fetch_symbol(session, s, args.interval, args.days, sem) for s in universe
        ])
    ok = [s for s, n in results if n > 0]
    print(f"\nГотово: {len(ok)}/{len(universe)} монет сохранено в {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
