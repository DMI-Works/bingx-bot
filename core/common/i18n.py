"""
Мови інтерфейсу: en (за замовчуванням), ru, uk.

Мова зберігається в users.lang (None — ще не обрана → англійська). Тут
лише чиста функція перекладу і каталог текстів бекенду: сповіщення Telegram,
картки угод, повідомлення про помилки Mini App API. Тексти самого Mini App —
у webapp/frontend/src/locales.
"""

from __future__ import annotations

from typing import Dict, Optional

LANGS = ("en", "ru", "uk")
DEFAULT_LANG = "en"
LANG_NAMES = {"en": "🇬🇧 English", "ru": "🇷🇺 Русский", "uk": "🇺🇦 Українська"}


def normalize_lang(lang: Optional[str]) -> str:
    """'ru-RU' → 'ru'; невідома/порожня мова → англійська."""
    code = str(lang or "").strip().lower()[:2]
    if code == "ua":
        code = "uk"
    return code if code in LANGS else DEFAULT_LANG


def t(lang: Optional[str], key: str, **kwargs) -> str:
    entry = CATALOG.get(key)
    if entry is None:
        return key
    text = entry.get(normalize_lang(lang)) or entry[DEFAULT_LANG]
    return text.format(**kwargs) if kwargs else text


CATALOG: Dict[str, Dict[str, str]] = {
    # ---------------- Telegram: сповіщення ----------------
    "stage_breakeven": {"en": "🟡 Stop moved to breakeven", "ru": "🟡 Стоп перенесён в безубыток", "uk": "🟡 Перенесено в беззбиток"},
    "stage_trailing": {"en": "🟢 Stop trailed", "ru": "🟢 Стоп подтянут трейлингом", "uk": "🟢 Підтягнуто трейлінгом"},
    "stage_moved": {"en": "🔄 Stop moved", "ru": "🔄 Стоп перенесён", "uk": "🔄 Стоп перенесено"},
    "position_opened": {"en": "✅ <b>Position opened</b>", "ru": "✅ <b>Позиция открыта</b>", "uk": "✅ <b>Позицію відкрито</b>"},
    "take_profit": {"en": "Take profit:", "ru": "Тейк:", "uk": "Тейк:"},
    "stop": {"en": "Stop", "ru": "Стоп", "uk": "Стоп"},
    "tp_levels": {"en": "{n} levels", "ru": "{n} уровня", "uk": "{n} рівні"},
    "sl_triggered": {"en": "🛑 <b>Stop loss hit</b>", "ru": "🛑 <b>Сработал стоп-лосс</b>", "uk": "🛑 <b>Спрацював стоп-лосс</b>"},
    "tp_triggered": {"en": "🎯 <b>Take profit reached</b>", "ru": "🎯 <b>Достигнут тейк-профит</b>", "uk": "🎯 <b>Досягнуто тейк-профіт</b>"},
    "symbol": {"en": "Symbol", "ru": "Символ", "uk": "Символ"},
    "price": {"en": "Price", "ru": "Цена", "uk": "Ціна"},
    "level": {"en": "Level", "ru": "Уровень", "uk": "Рівень"},
    "entry": {"en": "Entry", "ru": "Вход", "uk": "Вхід"},
    "old_stop": {"en": "Old stop", "ru": "Старый стоп", "uk": "Старий стоп"},
    "new_stop": {"en": "New stop", "ru": "Новый стоп", "uk": "Новий стоп"},
    "strategy": {"en": "Strategy", "ru": "Стратегия", "uk": "Стратегія"},
    "symbol_paused": {
        "en": ("⏸ <b>{symbol} paused</b>\n\n"
               "{losses} losing trades in a row on {symbol} (limit: {limit}).\n"
               "No new positions on this coin for {hours} h, then trading resumes automatically.\n"
               "Other symbols trade as usual."),
        "ru": ("⏸ <b>{symbol} на паузе</b>\n\n"
               "{losses} убыточных сделок подряд по {symbol} (лимит: {limit}).\n"
               "Новые позиции по этой монете не будут открываться {hours} ч, "
               "потом торговля возобновится автоматически.\n"
               "По остальным символам бот торгует как обычно."),
        "uk": ("⏸ <b>{symbol} на паузі</b>\n\n"
               "{losses} збиткових угод поспіль по {symbol} (ліміт: {limit}).\n"
               "Нові позиції по цій монеті не відкриватимуться {hours} год, "
               "потім торгівля відновиться автоматично.\n"
               "По інших символах бот торгує як звичайно."),
    },
    "rotation": {"en": "🔄 <b>Coin rotation</b>", "ru": "🔄 <b>Ротация монет</b>", "uk": "🔄 <b>Ротація монет</b>"},
    "rotation_added": {"en": "Added", "ru": "Добавлено", "uk": "Додано"},
    "rotation_removed": {"en": "Removed", "ru": "Убрано", "uk": "Прибрано"},
    "rotation_total": {"en": "Active symbols", "ru": "Активных символов", "uk": "Активних символів"},
    "error_title": {"en": "⚠️ <b>Error</b>", "ru": "⚠️ <b>Ошибка</b>", "uk": "⚠️ <b>Помилка</b>"},
    "error_default": {"en": "Unknown error", "ru": "Неизвестная ошибка", "uk": "Невідома помилка"},
    "critical_title": {"en": "🚨 <b>CRITICAL ERROR</b>", "ru": "🚨 <b>КРИТИЧЕСКАЯ ОШИБКА</b>", "uk": "🚨 <b>КРИТИЧНА ПОМИЛКА</b>"},
    "critical_default": {"en": "Unknown critical error", "ru": "Неизвестная критическая ошибка", "uk": "Невідома критична помилка"},
    "startup_title": {"en": "🚀 <b>Bot started and ready</b>", "ru": "🚀 <b>Бот запущен и готов к работе</b>", "uk": "🚀 <b>Бот запущено та готовий до роботи</b>"},
    "startup_mode": {"en": "Mode", "ru": "Режим", "uk": "Режим"},
    "mode_test": {"en": "🧪 Testnet", "ru": "🧪 Тестовый", "uk": "🧪 Тестовий"},
    "mode_live": {"en": "🔴 LIVE (real funds)", "ru": "🔴 LIVE (реальные деньги)", "uk": "🔴 LIVE (реальні кошти)"},
    "startup_active": {"en": "Strategies active", "ru": "Стратегий активно", "uk": "Стратегій активно"},
    "startup_list": {"en": "Active strategies:", "ru": "Активные стратегии:", "uk": "Активні стратегії:"},
    "balance": {"en": "Balance", "ru": "Баланс", "uk": "Баланс"},

    # ---------------- Telegram: /start, /emergency ----------------
    "start_text": {
        "en": ("🤖 <b>Velora — trading bot for BingX</b>\n\n"
               "The bot trades perpetual futures on your own BingX account. The money stays with you — "
               "the bot only opens and closes trades, and every trade gets a stop loss right away.\n\n"
               "<b>How to start:</b>\n"
               "1️⃣ Fund your USDT futures account (or the VST demo account for Testnet)\n"
               "2️⃣ Enable hedge mode in the futures settings\n"
               "3️⃣ Create an API key: read + futures trading, <b>no withdrawals</b>\n"
               "4️⃣ Tap “Open app” and paste the key\n\n"
               "📖 A detailed guide with FAQ is inside the app.\n"
               "🚨 /emergency — emergency stop.\n"
               "🌐 /language — change language."),
        "ru": ("🤖 <b>Velora — торговый бот для BingX</b>\n\n"
               "Бот сам торгует бессрочными фьючерсами на вашем счёте BingX. Деньги остаются у вас — "
               "бот только открывает и закрывает сделки, у каждой сразу стоит стоп-лосс.\n\n"
               "<b>Как начать:</b>\n"
               "1️⃣ Пополните фьючерсный счёт USDT (или демо-счёт VST для Testnet)\n"
               "2️⃣ Включите хедж-режим в настройках фьючерсов\n"
               "3️⃣ Создайте API-ключ: чтение + торговля фьючерсами, <b>без вывода средств</b>\n"
               "4️⃣ Нажмите «Открыть приложение» и вставьте ключ\n\n"
               "📖 Подробная инструкция с ответами на вопросы — внутри приложения.\n"
               "🚨 /emergency — аварийная остановка торговли.\n"
               "🌐 /language — сменить язык."),
        "uk": ("🤖 <b>Velora — торговий бот для BingX</b>\n\n"
               "Бот сам торгує безстроковими ф'ючерсами на вашому рахунку BingX. Гроші лишаються у вас — "
               "бот лише відкриває та закриває угоди, у кожної одразу стоїть стоп-лос.\n\n"
               "<b>Як почати:</b>\n"
               "1️⃣ Поповніть ф'ючерсний рахунок USDT (або демо-рахунок VST для Testnet)\n"
               "2️⃣ Увімкніть хедж-режим у налаштуваннях ф'ючерсів\n"
               "3️⃣ Створіть API-ключ: читання + торгівля ф'ючерсами, <b>без виведення коштів</b>\n"
               "4️⃣ Натисніть «Відкрити застосунок» і вставте ключ\n\n"
               "📖 Детальна інструкція з відповідями на питання — всередині застосунку.\n"
               "🚨 /emergency — аварійна зупинка торгівлі.\n"
               "🌐 /language — змінити мову."),
    },
    "choose_language": {
        "en": "🌐 Choose your language:",
        "ru": "🌐 Выберите язык:",
        "uk": "🌐 Оберіть мову:",
    },
    "btn_open_app": {"en": "🚀 Open app", "ru": "🚀 Открыть приложение", "uk": "🚀 Відкрити застосунок"},
    "btn_emergency": {"en": "🚨 Emergency stop", "ru": "🚨 Аварийная остановка", "uk": "🚨 Аварійна зупинка"},
    "btn_language": {"en": "🌐 Language", "ru": "🌐 Язык", "uk": "🌐 Мова"},
    "emergency_prompt": {"en": "⚠️ Emergency stop — choose an action:", "ru": "⚠️ Аварийная остановка — выберите действие:", "uk": "⚠️ Аварійна зупинка — виберіть дію:"},
    "btn_stop_only": {"en": "🚨 Only stop trading", "ru": "🚨 Только остановить торговлю", "uk": "🚨 Тільки зупинити торгівлю"},
    "btn_stop_close": {"en": "🚨 Stop and close positions", "ru": "🚨 Остановить и закрыть позиции", "uk": "🚨 Зупинити і закрити позиції"},
    "btn_cancel": {"en": "❌ Cancel", "ru": "❌ Отмена", "uk": "❌ Скасувати"},
    "cancelled": {"en": "❌ Cancelled", "ru": "❌ Отменено", "uk": "❌ Скасовано"},
    "no_session": {
        "en": "ℹ️ You have no active trading session — nothing to stop",
        "ru": "ℹ️ У вас нет активной торговой сессии — останавливать нечего",
        "uk": "ℹ️ У вас немає активної торгової сесії — зупиняти нічого",
    },
    "stopped_closing": {
        "en": "🚨 Trading disabled, closing positions: {n}",
        "ru": "🚨 Торговля выключена, закрываю позиции: {n}",
        "uk": "🚨 Торгівлю вимкнено, закриваю позиції: {n}",
    },
    "stopped": {
        "en": "🚨 Emergency stop activated — trading disabled",
        "ru": "🚨 Аварийная остановка активирована — торговля выключена",
        "uk": "🚨 Аварійна зупинка активована — торгівля вимкнена",
    },

    "invoice_issued": {
        "en": ("🧾 <b>Weekly invoice: {amount} USDT</b>\n"
               "Net profit charged: {chargeable} USDT, fee {fee}%.\n"
               "New trades are paused until payment. Open positions are still managed."),
        "ru": ("🧾 <b>Счёт за неделю: {amount} USDT</b>\n"
               "Чистая прибыль к оплате: {chargeable} USDT, комиссия {fee}%.\n"
               "Новые сделки остановлены до оплаты. Открытые позиции сопровождаются дальше."),
        "uk": ("🧾 <b>Рахунок за тиждень: {amount} USDT</b>\n"
               "Чистий прибуток до оплати: {chargeable} USDT, комісія {fee}%.\n"
               "Нові угоди зупинено до оплати. Відкриті позиції супроводжуються далі."),
    },

    # ---------------- Картка угоди (картинка) ----------------
    "card_opened": {"en": "Position opened", "ru": "Позиция открыта", "uk": "Позицію відкрито"},
    "card_closed": {"en": "Position closed", "ru": "Позиция закрыта", "uk": "Позицію закрито"},
    "card_margin": {"en": "MARGIN", "ru": "МАРЖА", "uk": "МАРЖА"},
    "card_entry": {"en": "Entry price", "ru": "Цена входа", "uk": "Ціна входу"},
    "card_close": {"en": "Close price", "ru": "Цена закрытия", "uk": "Ціна закриття"},
    "card_sl": {"en": "Stop loss", "ru": "Стоп-лосс", "uk": "Стоп-лосс"},
    "card_tp": {"en": "Take profit", "ru": "Тейк-профит", "uk": "Тейк-профіт"},
    "card_ref": {"en": "Referral code", "ru": "Реферальный код", "uk": "Реферальний код"},

    # ---------------- Mini App API: помилки ----------------
    "err_connect_keys": {"en": "Connect your BingX API keys", "ru": "Подключите API-ключи BingX", "uk": "Підключіть API-ключі BingX"},
    "err_admin_only": {"en": "Admins only", "ru": "Только для администратора", "uk": "Лише для адміністратора"},
    "err_rate_limited": {
        "en": "Too many requests — please try again shortly",
        "ru": "Слишком много запросов — попробуйте чуть позже",
        "uk": "Забагато запитів — спробуйте трохи пізніше",
    },
    "err_position_not_found": {"en": "Position not found among open ones", "ru": "Позиция не найдена среди открытых", "uk": "Позицію не знайдено серед відкритих"},
    "err_exchange_unavailable": {"en": "Exchange unavailable", "ru": "Биржа недоступна", "uk": "Біржа недоступна"},
    "err_positions_fetch": {"en": "Could not fetch positions from the exchange", "ru": "Не удалось получить позиции с биржи", "uk": "Не вдалося отримати позиції з біржі"},
    "err_position_not_managed": {
        "en": "The bot does not manage this position — close it on the exchange",
        "ru": "Бот не ведёт эту позицию — закройте её на бирже",
        "uk": "Бот не веде цю позицію — закрийте її на біржі",
    },
    "err_close_rejected": {"en": "The exchange rejected the close order", "ru": "Биржа не приняла ордер на закрытие", "uk": "Біржа не прийняла ордер на закриття"},
    "err_exchange_request": {"en": "Exchange request failed", "ru": "Запрос к бирже не удался", "uk": "Запит до біржі не вдався"},
    "err_user_not_found": {"en": "User not found", "ru": "Пользователь не найден", "uk": "Користувача не знайдено"},
    "err_block_self": {"en": "You cannot block yourself", "ru": "Нельзя заблокировать самого себя", "uk": "Не можна заблокувати самого себе"},
    "err_fee_range": {"en": "Enter a percentage from 0 to 100", "ru": "Введите процент от 0 до 100", "uk": "Введіть відсоток від 0 до 100"},
    "err_invoice_not_found": {"en": "Invoice not found", "ru": "Счёт не найден", "uk": "Рахунок не знайдено"},
    "err_invoice_closed": {"en": "Invoice already closed or not found", "ru": "Счёт уже закрыт или не найден", "uk": "Рахунок уже закрито або не знайдено"},
    "err_key_withdraw": {
        "en": "This key has withdrawals enabled — create a key without the Withdraw permission",
        "ru": "У ключа включён вывод средств — создайте ключ без права «Вывод» (Withdraw)",
        "uk": "У ключа увімкнено виведення коштів — створіть ключ без права «Виведення» (Withdraw)",
    },
    "err_key_no_futures": {
        "en": "This key cannot trade futures — enable “Perpetual Futures” when creating the key",
        "ru": "У ключа нет права торговли фьючерсами — включите «Бессрочные фьючерсы» при создании ключа",
        "uk": "У ключа немає права торгівлі ф'ючерсами — увімкніть «Безстрокові ф'ючерси» під час створення ключа",
    },
    "err_key_rejected": {"en": "The exchange rejected the key: {code} {msg}", "ru": "Биржа отклонила ключ: {code} {msg}", "uk": "Біржа відхилила ключ: {code} {msg}"},
    "err_exchange_unreachable": {"en": "Could not reach the exchange", "ru": "Не удалось связаться с биржей", "uk": "Не вдалося зв'язатися з біржею"},
    "err_no_balance": {"en": "The exchange returned no balance for this key", "ru": "Биржа не вернула баланс по этому ключу", "uk": "Біржа не повернула баланс за цим ключем"},
    "err_close_positions_first": {"en": "You have open positions — close them first", "ru": "Есть открытые позиции — сначала закройте их", "uk": "Є відкриті позиції — спершу закрийте їх"},
    "err_service_unavailable": {"en": "Service temporarily unavailable", "ru": "Сервис временно недоступен", "uk": "Сервіс тимчасово недоступний"},
    "err_key_saved_not_started": {
        "en": "Key saved, but trading could not be started",
        "ru": "Ключ сохранён, но запустить торговлю не удалось",
        "uk": "Ключ збережено, але запустити торгівлю не вдалося",
    },
    "err_no_keys_for_mode": {"en": "No API keys for {mode} mode", "ru": "Нет API-ключей для режима {mode}", "uk": "Немає API-ключів для режиму {mode}"},
    "err_mode_open_positions": {
        "en": "You have open positions — mode switch is blocked",
        "ru": "Есть открытые позиции — смена режима заблокирована",
        "uk": "Є відкриті позиції — зміну режиму заблоковано",
    },
    "err_mode_start_failed": {"en": "Could not start trading in this mode", "ru": "Не удалось запустить торговлю в этом режиме", "uk": "Не вдалося запустити торгівлю в цьому режимі"},
    "err_margin_min_gt_max": {
        "en": "Minimum margin ({min}) is greater than maximum ({max})",
        "ru": "Минимальная маржа ({min}) больше максимальной ({max})",
        "uk": "Мінімальна маржа ({min}) більша за максимальну ({max})",
    },
    "err_max_stop_range": {
        "en": "Enter a value from 1 to {limit} (liquidation is ~100%)",
        "ru": "Введите значение от 1 до {limit} (ликвидация ~100%)",
        "uk": "Введіть значення від 1 до {limit} (ліквідація ~100%)",
    },
    "err_max_positions_range": {"en": "Enter a whole number from 1 to {limit}", "ru": "Введите целое число от 1 до {limit}", "uk": "Введіть ціле число від 1 до {limit}"},
    "err_invalid_value": {"en": "Invalid value: {error}", "ru": "Недопустимое значение: {error}", "uk": "Неприпустиме значення: {error}"},
    "err_unknown_lang": {"en": "Unknown language", "ru": "Неизвестный язык", "uk": "Невідома мова"},
}
