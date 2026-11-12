# Ruflo Trading Bot

Асинхронный торговый бот для бессрочных фьючерсов **BingX** (USDT-M perpetual) и Telegram Mini App для мониторинга и настройки бота.

## Возможности

- Event-driven архитектура: все компоненты общаются через общий `EventBus` в одном asyncio event loop.
- Рыночные данные и события аккаунта (исполнения, SL/TP, PnL) приходят по WebSocket BingX.
- Встроенные стратегии: **WallBreakoutStrategy** (пробой «стен» в стакане), **TrendSupertrendStrategy** (EMA-тренд + Supertrend + объём + ATR) и **TsMomentumStrategy** (дневной time-series momentum с vol-target и выходом по развороту сигнала).
- Stop Loss и несколько уровней Take Profit на каждую позицию; SL/TP пересчитываются под фактическую цену входа.
- Трейлинг-стоп в двух режимах: лестница по ROI или `atr_3step` (безубыток с учётом комиссий → шаги по ATR → swing-стоп).
- Риск-менеджмент на уровне аккаунта: лимиты позиций, сайзинг от риска с потолком маржи, блокировка входов после серии стопов, чёрный список монеты после серии убытков.
- Автоматическая ротация пула монет (до 20) с белым и чёрным списками.
- Аналитика сделок: MFE/MAE, история перестановок SL, снимок параметров стратегии на момент входа.
- **MongoDB** для хранения состояния; testnet и live используют разные базы, записи разделены по `user_id`.
- Telegram Mini App: статистика, позиции, аналитика, управление монетами, стратегиями и настройками без перезапуска.

## Структура проекта

```
BotBingx/
├── config/
│   └── config.yaml            # Настройки уровня аккаунта (риск, фильтры, стакан, трейлинг, telegram, webapp)
├── core/
│   ├── analytics/             # ExcursionTracker — MFE/MAE и траектория цены сделки в trade_analytics
│   ├── common/                # Общие чистые хелперы: математика позиции (ROI, маржа, net PnL), разбор данных BingX
│   ├── database/              # Database (MongoDB) + репозитории positions / trade_analytics, StrategySettingsStore
│   ├── diagnostics/           # Генерация карточек PnL и статистики
│   ├── events/                # EventBus, EventType, publish_error
│   ├── exchange/              # BingXClient (REST + WS + user data stream), ошибки BingX, SL/TP-ордера, SymbolSelector
│   ├── risk/                  # RiskManager, TrailingStopManager (+ режим atr_3step и аварийный fallback SL)
│   ├── state/                 # SettingsManager (runtime-настройки, kill switch)
│   ├── strategies/            # Стратегии, реестр, индикаторы, OrderBookAnalyzer, StrategyManager
│   ├── telegram/              # TelegramBot и тексты сообщений — уведомления и аварийная остановка
│   └── trading/               # SimpleTrader — открытие и закрытие позиций, SL/TP, учёт PnL
├── webapp/
│   ├── backend/               # FastAPI: api.py (сборка), routes/ (эндпоинты), services/, deps.py, auth.py; static/ — собранный фронтенд
│   └── frontend/              # React 18 + Vite
├── scripts/
│   └── trade_report.py        # Разбор CSV-экспорта trade_analytics: срезы, симуляция SL/TP
├── tests/                     # unittest
├── Dockerfile
└── main.py                    # Точка входа: собирает и связывает все компоненты
```

## Установка

Нужны Python 3.11, Node.js 20 (для сборки Mini App) и база MongoDB (например, MongoDB Atlas).

```bash
git clone <repository-url>
cd BotBingx
pip install -r requirements.txt
cp .env.example .env
```

Заполните `.env`:

```env
BINGX_API_KEY=...
BINGX_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
WEBAPP_URL=https://<домен Mini App>
MONGO_URI=mongodb+srv://...
MONGO_DB_NAME=...
SECRETS_KEY=...
ADMIN_TELEGRAM_ID=...
```

| Переменная | Назначение |
|---|---|
| `SECRETS_KEY` | **Обязательна.** Ключ шифрования API-ключей пользователей (Fernet). Если его потерять или сменить, все сохранённые ключи станут непригодны |
| `ADMIN_TELEGRAM_ID` | Личный Telegram id администратора. Если не задан, администратора нет и миграция старых данных пропускается |
| `BINGX_API_KEY`, `BINGX_API_SECRET` | Ключи администратора (live): при первом запуске шифруются в его профиль, также используются клиентом рыночных данных |
| `BINGX_API_KEY_TESTNET`, `BINGX_API_SECRET_TESTNET` | Опциональные testnet-ключи администратора (VST) |
| `TELEGRAM_BOT_TOKEN` | Бот для уведомлений (приходят каждому пользователю в личные сообщения); токен также нужен для проверки `initData` в Mini App |
| `WEBAPP_URL` | HTTPS-адрес Mini App; без него кнопка «Профіль» в `/start` скрыта |
| `MONGO_URI` | **Обязательна.** Проверяется при импорте модуля базы данных |
| `MONGO_DB_NAME` | Имя базы. В режиме testnet к нему добавляется суффикс `_testnet` |
| `PORT` | Порт API и Mini App (задаёт Railway). По умолчанию `webapp.port` = 8000 |

## Запуск

```bash
python main.py
```

В одном процессе запускаются бот, Telegram-бот и API Mini App (FastAPI через uvicorn, порт `$PORT` или 8000).

> ⚠️ **Реальные деньги.** Сначала проверьте стратегии на testnet и начинайте с малых сумм.

### Многопользовательский режим

Рыночные данные и сигналы стратегий считаются один раз, а у каждого пользователя свой торговый контур: свои API-ключи, режим, риск-настройки, позиции, трейлинг и уведомления в его чат.

- Пользователь открывает Mini App через бота и подключает API-ключ BingX. Ключ проверяется запросом баланса и хранится в зашифрованном виде. Новые пользователи начинают на testnet.
- Стратегии (включение и параметры) и общий пул монет меняет только администратор, обычный пользователь раздел «Стратегии» не видит, а вкладка «Монеты» доступна ему только для просмотра. Пользователь задаёт свой риск.
- Режим (testnet/live) у каждого свой: **Профиль → «Переключить в Live/Testnet»**. Перезапускается только контур этого пользователя. Если есть открытые позиции, переключение блокируется (HTTP 409), подтвердить можно принудительно (`force`).
- Администратор видит пользователей на вкладке **Админ** и может заблокировать новые входы. Открытые позиции заблокированного пользователя продолжают сопровождаться.
- Подписка (`core/billing`): первые 7 дней бесплатно, затем каждые 7 дней счёт на % (по умолчанию 10, меняется на вкладке **Админ**) от чистой прибыли на live-счёте. Убыток переносится: процент берётся только после того, как пользователь отыграл минус. Testnet и администратор не тарифицируются. Пока есть неоплаченный счёт, новые входы запрещены, открытые позиции сопровождаются. Сейчас оплату отмечает администратор; автосписание с BingX ещё не подключено.
- При старте `core/users/migration.py` переносит существующие данные на аккаунт администратора. Операция повторно безопасна.
- `exchange.testnet` в YAML используется только как режим администратора при первом запуске, если в базе нет сохранённого режима.

### Docker / Railway

Multi-stage `Dockerfile` собирает фронтенд (node:20-slim), затем запускает `python main.py` на python:3.11-slim. На Railway переменные окружения задаются в настройках сервиса, порт берётся из `PORT`.

## Telegram Mini App

Мини-апп — основной способ управлять ботом. Вкладки:

- **Статистика** — баланс, PnL, открытые позиции, история сделок.
- **Аналитика** — подробная история сделок (MFE/MAE, перестановки SL, исполнения TP, параметры на входе).
- **Монети** — текущий пул символов, кандидаты, белый и чёрный списки.
- **Налаштування** — глобальный переключатель торговли, включение и параметры стратегий, сброс к значениям по умолчанию.
- **Профіль**.

Изменяющие запросы к API проверяют Telegram `initData` (заголовок `X-Telegram-Init-Data`).

### Разработка фронтенда

```bash
cd webapp/frontend
npm install
npm run dev     # Vite на :5173, /api проксируется на http://localhost:8000
npm run build   # сборка в webapp/backend/static, FastAPI отдаёт её по /
```

Чтобы проверить приложение внутри Telegram, нужен HTTPS-туннель (cloudflared или ngrok) и кнопка меню в BotFather. Подробнее — в [webapp/INTEGRATION.md](webapp/INTEGRATION.md).

### Telegram-бот

- `/start` — панель: открыть Mini App, экспорт базы, аварийная остановка.
- `/emergency` — аварийная остановка: только запретить новые сделки или запретить и закрыть позиции.

Бот также присылает уведомления об открытии и закрытии позиций, срабатывании и переносе SL/TP, ротации монет, чёрном списке, стенах в стакане и ошибках.

## Конфигурация

`config/config.yaml` содержит только настройки уровня аккаунта:

- `exchange` — `testnet`, `websocket.enabled`.
- `trading.risk` — лимиты позиций и риска (см. ниже).
- `trading.filters` — белый и чёрный списки, `max_symbols`, минимальный объём за 24 ч, максимальный спред, `rotation_interval_seconds`. Кроме того:
  - `excluded_symbol_prefixes` — не-крипто контракты BingX (форекс, сырьё, акции, индексы), которые не попадают в пул;
  - `rank_by` — порядок отбора: `volatility` (по 24h-диапазону, по умолчанию) или `volume`;
  - `quiet_cooldown_seconds` — монета, убранная из пула за отсутствие сигналов, на это время уходит в конец очереди, и ротация пробует другие монеты.
- `trading.trend_filters` — **отдельный** пул символов для `TrendSupertrendStrategy` (те же ключи, что у `trading.filters`). Технически это второй `SymbolSelector`, который подписывает монеты только на `@trade` (без `@depth20`), поэтому `OrderBookAnalyzer` их не видит и `WallBreakoutStrategy` по ним не торгует — пулы полностью изолированы. По умолчанию ранжирование по `volume`, реже ротация и whitelist мейджоров (BTC/ETH/SOL/…), т.к. тренд чище идёт на ликвидных монетах, а не на топе по волатильности. Если блок пуст или отсутствует — трендовый пул не поднимается, и стратегия (если включена) работает на общем пуле `trading.filters`.
- `trading.orderbook` — параметры детекта стен (`wall_multiplier`, `approach_threshold_pct`, `breakout_consumption_pct` и др.).
- `trading.trailing_stop` — буферы трейлинга и `taker_fee_rate` для режима `atr_3step`. Блок должен быть вложен в `trading:`.
- `logging`, `telegram`, `webapp`, `analytics` (`path_step_pct` и `path_max_points` — детализация траектории сделки).

**Параметры стратегий в YAML не хранятся.** Значения по умолчанию берутся из `DEFAULT_PARAMS` каждой стратегии и при первом запуске записываются в MongoDB (коллекция `strategy_settings`). Дальше их меняют через Mini App, перезапуск не нужен.

Глобальный переключатель торговли (`trading.enabled`) хранится в базе и блокирует **только новые входы**. Открытые позиции продолжают сопровождаться (SL, TP, трейлинг).

## Стратегии

- **WallBreakoutStrategy** — торгует пробои стен, которые находит `OrderBookAnalyzer` по стакану `@depth20`. Сам анализатор сделок не открывает, а только публикует `WALL_DETECTED` и `WALL_BREAKOUT`. Уровни трейлинга по ROI (`trail_levels_percent`) задаются параметрами этой стратегии. `reentry_cooldown_seconds` (по умолчанию 900) запрещает новый вход в монету после закрытия сделки по ней: повторные входы в «пилу» дали большую часть убытка на тестнете.
- **TrendSupertrendStrategy** — фильтр тренда по EMA, вход по Supertrend с подтверждением объёмом, стоп по ATR, размер позиции от `risk_percent`. Использует трейлинг `atr_3step`. Работает на собственном пуле символов (`trading.trend_filters`), изолированном от пула WallBreakout.
- **TsMomentumStrategy** — дневной **time-series momentum** (академически устойчивый эффект, сильнее кросс-секционного). На закрытии дневной свечи и повторно каждые `recheck_interval_minutes` (по умолчанию 60; после закрытия в минус или по безубытку монета у этого пользователя ждёт `reentry_pause_minutes`, по умолчанию 30): знак доходности за `lookback_days` (по умолчанию 60) задаёт направление (LONG при росте, SHORT при падении — режим `long_short`; либо `long_flat` без шортов). Позиция держится **до разворота сигнала** (не по ценовому стопу): при смене знака позиция закрывается рыночным reduce-only ордером (`SimpleTrader.close_position`), противоположная открывается на следующей дневной сверке. Размер — **vol-target** (через риск-сайзинг: дистанция стопа ∝ ATR → размер ∝ 1/волатильность), плечо ограничено потолком (по умолчанию 10x). Тейк-профита нет; широкий дизастер-стоп (`disaster_stop_atr`·ATR) — только страховка от гэпа. Свой **стабильный** (почти не ротирующийся) пул `trading.tsmom_filters`.
  - Проверено бэктестом на наших данных (`scripts/tsmom_backtest.py`, 1d, ~2.7 года): L=60 long/short, vol-target → +16%/год, Sharpe 0.64, положительно во всех трёх третях истории. **Ожидание умеренное** (Sharpe ~0.6), доходность по третям затухает (компрессия трендов с 2024) — это риск-премия, не «грааль». По умолчанию выключена; обкатывать на testnet. Источники: [SSRN 4675565 (momentum under realistic assumptions)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4675565), [SSRN 4825389 (volume-weighted TSMOM)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4825389).

Все зарегистрированные стратегии создаются при запуске; выключенные просто игнорируют тики. Стратегии не имеют доступа к торговле на бирже: они только генерируют сигналы, а позиции открывает `SimpleTrader`.

### Добавление своей стратегии

1. Создайте класс-наследник `BaseStrategy` в `core/strategies/` и пометьте его `@register_strategy('MyStrategy')`.
2. Определите `DEFAULT_PARAMS`, classmethod `build_config(cls, app_config)` (обычно `deepcopy(DEFAULT_PARAMS)`) и `async analyze(symbol, price) -> Optional[dict]`.
3. Если параметры кешируются в атрибутах, переопределите `update_config()`: он вызывается при изменении параметров из Mini App.
4. Импортируйте стратегию в `core/strategies/__init__.py`, иначе реестр её не увидит.
5. Добавьте подписи и описания параметров в `core/strategies/param_catalog.py` для экрана настроек.

Если в конструкторе есть параметр `bingx_client`, клиент биржи будет передан автоматически (например, для загрузки свечей).

Пример сигнала:

```python
return {
    'action': 'OPEN',
    'symbol': symbol,
    'side': 'LONG',                  # или 'SHORT'
    'quantity': qty,
    'leverage': 10,
    'stop_loss_price': sl,
    'take_profit_levels': [{'price': tp, 'close_percent': 100}],
    'strategy': self.name,
    'reference_price': price,        # SL/TP пересчитаются к фактической цене входа
    # необязательно:
    'risk_percent': 0.5,             # сайзинг от риска (с учётом max_margin_percent_per_trade)
    'trail_meta': {'mode': 'atr_3step', ...},
    'analytics_context': {...},      # попадёт в trade_analytics
}
```

## Риск-менеджмент

`RiskManager` (`trading.risk`) действует для всего аккаунта и всех стратегий:

- `max_open_positions`, `max_positions_per_symbol`, `max_total_risk_percent` — лимиты позиций и общего риска.
- Лимит одновременных позиций меняется в Mini App (Настройки → Торговля, `trading.max_open_positions` в `settings`). Значение из Mini App важнее `max_open_positions` из YAML и действует на следующие входы.
- `use_risk_based_sizing` + `risk_per_trade_percent` — размер позиции = `equity × risk% / |entry − stop|` (по умолчанию выключено).
- `max_margin_percent_per_trade` — потолок маржи одной сделки для сигналов с `risk_percent`, чтобы узкий стоп не раздувал позицию.
- Минимальная маржа сделки (`trading.min_margin_usdt` в коллекции `settings`, по умолчанию 10 USDT) задаётся в Mini App (Настройки → Торговля). Если стратегия посчитала маржу меньше, объём увеличивается до этой суммы. Действует для всех стратегий и важнее `max_margin_percent_per_trade`. 0 — выключено.
- Максимальная маржа сделки (`trading.max_margin_usdt`, по умолчанию 100 USDT, 0 — выключено) задаётся там же. Если маржа по расчёту стратегии больше, объём уменьшается до этой суммы. Она важнее минимальной маржи, а минимум не может быть больше максимума.
- Максимальный стоп (`trading.max_stop_roi_percent`, по умолчанию 30% ROI, максимум 90) тоже задаётся в Mini App. Если стоп стратегии дальше (например, аварийный стоп TsMomentum при 10x), бот подтягивает его ближе, чтобы стоп сработал раньше ликвидации. Плечо при этом не меняется.
- `taker_fee_rate` — в сайзинг закладывается комиссия за вход и выход.
- `stop_loss_streak_limit` / `stop_loss_streak_window_hours` / `stop_loss_block_hours` — после N убыточных стопов подряд за окно новые входы блокируются на заданное время.
- `max_consecutive_losses` — после N убытков подряд по одной монете новые входы по ней блокируются на `symbol_pause_hours` часов (по умолчанию 24), затем торговля возобновляется автоматически.

## База данных

MongoDB, коллекции:

| Коллекция | Содержимое |
|---|---|
| `positions` | Открытые и закрытые позиции. Состояние позиции хранится JSON-строкой в поле `metadata` и восстанавливается при перезапуске |
| `settings` | Runtime-настройки (`trading.enabled`, списки монет и др.), значения хранятся JSON-строками |
| `strategy_settings` | Параметры по умолчанию и текущие параметры стратегий, флаг включения |
| `trade_analytics` | Полная история сделки: MFE/MAE и траектория цены (`excursion.path`), перестановки SL, исполнения TP, снимок параметров, контекст сигнала (`signal_context`) и рынка на входе (`market_context`), режим (`testnet`/`live`) |

Чистый PnL сделки: `realized_pnl + commission_open + commission_close + funding_fee` (комиссии хранятся отрицательными числами).

### Разбор сделок

Экспортируйте `trade_analytics` в CSV (MongoDB Compass → Export Collection → CSV) и запустите:

```bash
python scripts/trade_report.py trading_bot_testnet.trade_analytics.csv
python scripts/trade_report.py export.csv --strategy WallBreakoutStrategy
```

Скрипт показывает винрейт, profit factor и средний результат по стратегиям, срезы (LONG/SHORT, повторный вход, проскальзывание стопа, худшие монеты) и симуляцию выходов: дополнительный тейк по MFE и сетку SL/TP/безубытка по траектории `excursion.path`. Рядом с каждым вариантом указан винрейт, нужный для выхода в ноль: высокий винрейт сам по себе не означает прибыль.

## Тесты

```bash
python -m unittest discover tests -v
python -m unittest tests.test_trend_supertrend -v
python -m unittest tests.test_trend_supertrend.TestIndicators.test_atr_wilder -v
```

Тесты не подключаются к реальной базе, но `MONGO_URI` должна быть задана: тесты сами выставляют фиктивное значение через `os.environ.setdefault`.

## Логирование

Логи пишутся в консоль и в `logs/trading_bot.log` с ротацией (уровень, путь, `max_bytes` и `backup_count` задаются в разделе `logging`).

## Безопасность

- Давайте API-ключам только права на торговлю, без вывода средств.
- Включите IP whitelist на бирже.
- Проверяйте изменения на testnet и начинайте с малых сумм.
- Не коммитьте `.env`.

## Лицензия

MIT

## Disclaimer

Используйте бота на свой страх и риск. Разработчики не несут ответственности за финансовые потери.
