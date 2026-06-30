# Ruflo Trading Bot

Высоконадежный модульный торговый бот для криптобирж с поддержкой BingX, Binance, Bybit.

## Основные возможности

- ✅ Отказоустойчивая архитектура с восстановлением состояния
- ✅ WebSocket соединение с автоматическим переключением на REST
- ✅ Гарантированное исполнение ордеров с повторными попытками
- ✅ Автоматическое создание Stop Loss для всех позиций
- ✅ Поддержка нескольких Take Profit уровней
- ✅ Управление рисками (макс позиции, серия убытков, cooldown)
- ✅ Event-driven архитектура для всех операций
- ✅ SQLite база данных для хранения состояния
- ✅ Telegram бот для управления и уведомлений
- ✅ Настройки изменяются без перезапуска

## Архитектура

```
ruflo-trading-bot/
├── config/                 # Конфигурация
├── core/
│   ├── exchange/          # Подключение к биржам
│   ├── execution/         # Execution Engine и Order Manager
│   ├── strategies/        # Торговые стратегии
│   ├── events/            # Event Bus
│   ├── risk/              # Risk Manager
│   ├── state/             # Position Manager, Recovery Engine, Settings
│   ├── database/          # SQLite база данных
│   └── telegram/          # Telegram Bot
└── main.py                # Точка входа
```

## Установка

1. Клонировать репозиторий:
```bash
git clone <repository-url>
cd ruflo-trading-bot
```

2. Установить зависимости:
```bash
pip install -r requirements.txt
```

3. Создать `.env` файл из примера:
```bash
cp .env.example .env
```

4. Заполнить `.env` файл вашими API ключами:
```env
BINGX_API_KEY=your_api_key_here
BINGX_API_SECRET=your_api_secret_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

## Конфигурация

Основная конфигурация находится в `config/config.yaml`:

- **Exchange**: настройки API, WebSocket, REST
- **Trading**: размер позиции, плечо, направление торговли
- **Risk**: лимиты позиций, риск-менеджмент
- **Stop Loss**: фиксированный %, ATR, структурный
- **Take Profit**: несколько уровней, trailing
- **Filters**: белый/черный список монет, фильтры по цене/объему
- **Telegram**: уведомления и управление

## Запуск

### Testnet (рекомендуется для тестирования)
```bash
python main.py
```

По умолчанию бот запускается в testnet режиме. Измените `testnet: false` в `config/config.yaml` для реальной торговли.

### Production
**⚠️ ВНИМАНИЕ: Реальная торговля с реальными деньгами!**

1. Убедитесь, что API ключи для production
2. Измените `testnet: false` в `config/config.yaml`
3. Протестируйте все стратегии на testnet
4. Запустите с малыми суммами

```bash
python main.py
```

## Использование

### Telegram команды

- `/start` - Главное меню
- `/status` - Статус бота и позиций
- `/positions` - Список открытых позиций
- `/settings` - Настройки
- `/emergency` - Аварийная остановка

### Управление настройками

Все настройки можно изменять через:
1. Telegram бот (рекомендуется)
2. Прямое изменение `config/config.yaml` (требуется перезапуск)
3. База данных SQLite (для продвинутых пользователей)

### Добавление монет в торговлю

В `config/config.yaml`:
```yaml
trading:
  filters:
    whitelist_symbols: ["BTC-USDT", "ETH-USDT"]
```

Или через Telegram: Settings → Whitelist

## Стратегии

### Встроенная стратегия: Simple Moving Average

Пример стратегии на основе SMA:
- Long: когда цена > SMA
- Short: когда цена < SMA
- Автоматический Stop Loss и Take Profit

### Создание собственной стратегии

1. Наследуйтесь от `BaseStrategy`
2. Реализуйте метод `analyze()`
3. Верните сигнал с `action`, `symbol`, `side`, `quantity`
4. Стратегия **НЕ** имеет прямого доступа к API биржи

Пример:
```python
from core.strategies import BaseStrategy

class MyStrategy(BaseStrategy):
    async def analyze(self, symbol: str, price: float):
        # Ваша логика
        if условие_для_входа:
            return {
                'action': 'OPEN',
                'symbol': symbol,
                'side': 'LONG',
                'quantity': 0.01,
                'stop_loss_price': price * 0.98,
                'take_profit_levels': [
                    {'price': price * 1.03, 'close_percent': 100}
                ]
            }
        return None
```

## Критические требования

- ✅ Ни один ордер не должен быть потерян
- ✅ Ни один ордер не должен быть создан дважды (идемпотентность)
- ✅ Любая открытая позиция обязана иметь Stop Loss
- ✅ Все изменения состояния приходят через WebSocket
- ✅ REST используется только как резерв
- ✅ После любого сбоя состояние полностью восстанавливается

## База данных

SQLite база хранит:
- Ордера (история и статусы)
- Позиции (открытые и закрытые)
- Сделки
- Баланс
- Комиссии
- PnL и ROI
- Логи ошибок и событий
- Настройки

База находится в `data/trading_bot.db`

## Recovery Engine

При каждом запуске бот:
1. Получает открытые позиции с биржи
2. Получает открытые ордера
3. Получает баланс
4. Сверяет с локальной БД
5. Восстанавливает состояние
6. Проверяет наличие Stop Loss для всех позиций

## Risk Manager

Контролирует:
- Максимум открытых позиций
- Максимум позиций на монету
- Максимальный общий риск
- Серию убыточных сделок
- Cooldown после сделки

## Emergency Stop

Аварийная остановка через Telegram:
- Запрещает новые сделки
- Отменяет ожидающие ордера
- Опционально закрывает все позиции
- Отключает стратегии

## Логирование

Логи сохраняются в `logs/trading_bot.log`:
- Все операции с ордерами
- Открытие/закрытие позиций
- Ошибки и исключения
- WebSocket события
- Recovery процесс

## Мониторинг

Через Telegram бот:
- Реальное время открытие/закрытие позиций
- Stop Loss и Take Profit события
- Ошибки и критические события
- WebSocket переподключения
- Статистика торговли

## Разработка

### Структура Event Bus

Все модули взаимодействуют через события:
```
PriceUpdated → Strategy → SignalGenerated → ExecutionEngine 
→ OrderFilled → PositionOpened → Telegram
```

### Добавление нового события

1. Добавьте тип в `EventType` (`core/events/event_types.py`)
2. Публикуйте событие через `event_bus.publish()`
3. Подписывайтесь через `event_bus.subscribe()`

## Поддержка бирж

### Текущая поддержка:
- ✅ BingX (полная поддержка)

### Планируется:
- 🔄 Binance
- 🔄 Bybit

## Безопасность

- Используйте API ключи только с правами на торговлю (без вывода)
- IP whitelist на бирже
- Testnet для тестирования
- Начинайте с малых сумм
- Мониторьте через Telegram

## FAQ

**Q: Бот потерял соединение, что с позициями?**  
A: Recovery Engine при следующем запуске восстановит все позиции и ордера с биржи.

**Q: Позиция открылась без Stop Loss?**  
A: Это невозможно - Execution Engine создает SL с повторными попытками до подтверждения.

**Q: Можно ли изменить настройки без перезапуска?**  
A: Да, через Telegram бот или изменением в БД через SettingsManager.

**Q: Что если WebSocket отключится?**  
A: Автоматическое переключение на REST API, попытки переподключения.

## Лицензия

MIT

## Disclaimer

Используйте бота на свой страх и риск. Разработчики не несут ответственности за финансовые потери.
