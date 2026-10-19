# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Async Python trading bot for BingX perpetual futures ("Ruflo Trading Bot"), plus a Telegram Mini App (FastAPI backend + React/Vite frontend) used to monitor and configure it. Code comments, log messages and UI strings are in Ukrainian/Russian. Follow that in new code.

`README.md` (Russian) is the user-facing description. Keep it in sync when you change setup steps, env vars, config keys or strategies. Storage is **MongoDB** (`pymongo`), and the working strategies are `WallBreakoutStrategy` and `TrendSupertrendStrategy`.

## Commands

```bash
pip install -r requirements.txt
python main.py                      # runs bot + webapp API (port $PORT or webapp.port, default 8000)

# Tests (stdlib unittest; pytest also works if installed)
python -m unittest discover tests -v
python -m unittest tests.test_trend_supertrend -v
python -m unittest tests.test_trend_supertrend.TestIndicators.test_atr_wilder -v

# Mini App frontend (webapp/frontend, yarn.lock present; npm also used in Dockerfile)
cd webapp/frontend && npm install
npm run dev      # Vite on :5173, proxies /api -> http://localhost:8000
npm run build    # outputs into webapp/backend/static (served by FastAPI at /)
```

No linter or formatter is configured. Deployment is the multi-stage `Dockerfile` (builds the frontend, then runs `python main.py`), targeting Railway (`PORT` env var).

## Environment

`.env` (see `.env.example`): `BINGX_API_KEY`, `BINGX_API_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `WEBAPP_URL`, `MONGO_URI`, `MONGO_DB_NAME`.

- `core/database/database.py` checks `MONGO_URI` **at import time** and raises if it is missing. Anything that imports `core.risk`, `core.trading` or `core.database` needs it set. Tests do `os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")` before importing and never open a real connection. Copy that pattern in new tests.
- The DB name gets the suffix `_testnet` when `exchange.testnet: true` in `config/config.yaml`, so testnet and live data sit in separate databases. `trade_analytics` rows also carry a `mode` field.
- `main.py` calls `load_dotenv()` before importing `webapp.backend.api`, because `auth.py` reads `TELEGRAM_BOT_TOKEN` at module import.

## Architecture

Everything runs in one asyncio event loop started by `main.py`, which builds every component and wires them together explicitly. The FastAPI app runs in that same loop as a `uvicorn.Server` task. It gets the live objects (`db`, `exchange_client`, `settings_manager`, `strategy_settings`, `strategy_manager`, `symbol_selector`, `signal_tracker`) through `app.state`, so it never creates its own DB or exchange client.

### Event bus

Components communicate through `core/events/EventBus` (`subscribe` / `publish(Event(type, data, source))`). `EventType` lives in `core/events/event_types.py`, and many members are commented out as unused. The main flow:

```
BingX WS (market) --PRICE_UPDATED / ORDERBOOK_UPDATED--> strategies, OrderBookAnalyzer, TrailingStopManager, ExcursionTracker
strategy.analyze() --SIGNAL_GENERATED--> SimpleTrader.open_position()  (gated by RiskManager + global trading.enabled)
BingX WS (user data stream) --ORDER_FILLED / HANDLE_ACCOUNT_INFO_UPDATE--> SimpleTrader (fills, SL/TP hits, PnL accounting)
SimpleTrader --POSITION_OPENED/CLOSED, STOP_LOSS_*, ERROR/CRITICAL_ERROR--> TelegramBot, RiskManager, TrailingStopManager
```

### Key components

- **`core/exchange/`**: `BingXClient` (REST + market WebSocket + user-data stream) and `SymbolSelector`. The selector rotates the shared symbol pool (≤ `max_symbols`) every `rotation_interval_seconds`. It keeps symbols that produced a signal recently (via `SignalActivityTracker`) and honours the whitelist/blacklist. That pool is shared by all strategies and the order-book analyzer.
- **`core/trading/simple_trader.py`**: the only place that opens or closes positions and places SL/TP orders. In-memory `open_positions` is keyed `f"{symbol}_{side}"` and is persisted as JSON in the `positions.metadata` string field. `_restore_open_positions()` reloads it on startup, so new per-position fields need a fallback for old records. Net PnL formula: `realized_pnl + commission_open + commission_close + funding_fee`. Fees are negative numbers, so they are added, not subtracted (see `tests/test_pnl_accounting.py`).
- **`core/risk/risk_manager.py`**: account-level limits (max positions, per-symbol limit, total risk), optional risk-based sizing with a margin cap and round-trip taker fee, and a stop-loss-streak entry block. After `max_consecutive_losses` losses in a row on one coin it **blacklists** that coin (publishes `SYMBOL_BLACKLISTED`), which is not a timed pause.
- **`core/risk/trailing_stop_manager.py`**: moves SL on price ticks. It has two modes:
  - The default ROI ladder, `trail_levels_percent`. These levels come from WallBreakoutStrategy params via `strategy_manager.subscribe_params(...)`, not from `config.yaml`.
  - `atr_3step`, chosen when `position['trail_meta']['mode'] == 'atr_3step'`. TrendSupertrendStrategy sets it in the signal: move to breakeven including fees, then step on ATR, then a swing stop.
- **`core/analytics/excursion_tracker.py`**: read-only. It tracks MFE/MAE per open trade into `trade_analytics`.
- **`core/database/`**:
  - `Database` (Mongo collections `positions`, `balance`, `settings`, `trade_analytics`). `trade_analytics` keeps the full history of a trade (SL moves, TP fills, a snapshot of the strategy params at entry, derived metrics), separate from `positions`, which the rest of the system reads.
  - `StrategySettingsStore` (`strategy_settings` collection): per-strategy default params, current params and an enabled flag.
- **`core/state/settings_manager.py`**: runtime settings stored in the `settings` collection. Includes `trading.enabled`, a global kill switch that gates **only new entries**; SL/TP/trailing keep running. It is seeded to `True` on the first run.
- **`core/telegram/telegram_bot.py`**: notifications plus a few commands. Settings management moved to the Mini App, and the old Telegram settings menu is gone.
- **`webapp/backend/api.py`**: `/api/stats|positions|trades|analytics/*|profile|settings/*|symbols/*`. Mutating endpoints use `Depends(require_telegram_user)`, which validates Telegram `initData` from the `X-Telegram-Init-Data` header in `auth.py`. Built static files are mounted at `/`.

### Strategies

Strategies are registered with `@register_strategy('Name')` (`core/strategies/registry.py`). To be discovered, a strategy must be imported in `core/strategies/__init__.py`. To add one:

1. Subclass `BaseStrategy`. Define `DEFAULT_PARAMS`, a `build_config(cls, app_config)` classmethod (usually `deepcopy(DEFAULT_PARAMS)`), and `async analyze(symbol, price) -> Optional[dict]`.
2. Override `update_config()` if you cache params in attributes. It is called when params change in the Mini App, without a restart.
3. Add human-readable labels and descriptions for new params to `core/strategies/param_catalog.py`, which the Mini App settings tab uses.

`StrategyManager.setup()` creates **every** registered strategy, even disabled ones (a disabled strategy ignores ticks). It seeds defaults into Mongo, and new keys added to `DEFAULT_PARAMS` later are merged into existing rows. After that, live params come from the DB, not from code. A constructor parameter named `bingx_client` is injected automatically by checking the signature.

Strategies have no direct exchange access for trading; they only emit signals. Signal dict keys read by `SimpleTrader._handle_signal`: `action` (`'OPEN'`), `symbol`, `side` (`LONG`/`SHORT`), `quantity`, `leverage`, `stop_loss_price`, `take_profit_levels`, `strategy`, `reference_price` (SL/TP are rescaled to the actual fill price), `risk_percent`, `trail_meta`, `analytics_context`.

`OrderBookAnalyzer` only detects walls and publishes `WALL_DETECTED`/`WALL_BREAKOUT` events. Trades happen only if `WallBreakoutStrategy` is enabled.

## Configuration

`config/config.yaml` holds account-level settings only: `exchange.testnet`, `trading.risk`, `trading.filters`, `trading.orderbook`, `trading.trailing_stop`, `telegram`, `webapp` and `analytics`. Per-strategy params live in each strategy's `DEFAULT_PARAMS` and then in Mongo, edited through the Mini App. `trading.trailing_stop` must stay nested under `trading:`. Comments in the YAML record which keys are actually read; keep them accurate when you change config handling.
