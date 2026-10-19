import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from typing import Optional

from ..common.market_data import extract_account_balance, to_float
from ..events import EventBus, Event, EventType
from ..state import SettingsManager
from ..diagnostics import generate_pnl_card
from . import messages


LOCAL_TZ = ZoneInfo("Europe/Kyiv")

logger = logging.getLogger(__name__)


class TelegramBot:
    """
    Панель бота звужена навмисно: єдина точка керування угодами — мініапп
    (кнопка "Профіль"). Тут лишається те, що мусить бути доступне миттєво з
    чату, навіть якщо мініапп не відкривається, — аварійна зупинка — та
    проактивні алерти (відкриття/закриття позицій, SL/TP, помилки).
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        event_bus: EventBus,
        settings_manager: SettingsManager,
        exchange_client=None,
        db=None,
    ):
        self.webapp_url = os.getenv('WEBAPP_URL', '')
        self.token = token
        self.chat_id = chat_id
        self.event_bus = event_bus
        self.settings_manager = settings_manager
        self.exchange_client = exchange_client
        self.db = db

        self.application: Optional[Application] = None
        self.notifications_enabled = True

        self._subscribe_to_events()
        logger.info("TelegramBot initialized")

    def _subscribe_to_events(self) -> None:
        handlers = {
            EventType.POSITION_OPENED: self._on_position_opened,
            EventType.POSITION_CLOSED: self._on_position_closed,
            EventType.STOP_LOSS_TRIGGERED: self._on_stop_loss_triggered,
            EventType.STOP_LOSS_MOVED: self._on_stop_loss_moved,
            EventType.TAKE_PROFIT_TRIGGERED: self._on_take_profit_triggered,
            EventType.ERROR: self._on_error,
            EventType.CRITICAL_ERROR: self._on_critical_error,
            EventType.SYMBOLS_ROTATED: self._on_symbols_rotated,
            EventType.SYMBOL_BLACKLISTED: self._on_symbol_blacklisted,
        }
        for event_type, handler in handlers.items():
            self.event_bus.subscribe(event_type, handler)

    async def start(self) -> None:
        self.application = Application.builder().token(self.token).build()

        self.application.add_handler(CommandHandler("start", self._cmd_start))
        self.application.add_handler(CommandHandler("emergency", self._cmd_emergency))
        self.application.add_handler(CallbackQueryHandler(self._handle_callback))

        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()

        logger.info("Telegram bot started")

    async def stop(self) -> None:
        if self.application:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
            logger.info("Telegram bot stopped")

    async def send_message(self, text: str) -> None:
        if self.application and self.notifications_enabled:
            try:
                await self.application.bot.send_message(chat_id=self.chat_id, text=text, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Failed to send Telegram message: {e}")

    async def _send_photo(self, photo, caption: str) -> None:
        await self.application.bot.send_photo(
            chat_id=self.chat_id,
            photo=photo,
            caption=caption,
            parse_mode='HTML'
        )

    async def _reply(self, update: Update, text: str, **kwargs):
        if update.message:
            return await update.message.reply_text(text, **kwargs)
        if update.callback_query:
            return await update.callback_query.message.reply_text(text, **kwargs)

    # ------------------------------------------------------------------
    # Панель: Профіль (мініапп) + Аварійна зупинка
    # ------------------------------------------------------------------

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = []

        if self.webapp_url:
            keyboard.append([InlineKeyboardButton("👤 Профіль", web_app=WebAppInfo(url=self.webapp_url))])
        else:
            logger.warning("WEBAPP_URL не задано — кнопка \"Профіль\" прихована")

        keyboard.append([InlineKeyboardButton("💾 Експорт бази", callback_data="export_db")])
        keyboard.append([InlineKeyboardButton("🚨 Аварійна зупинка", callback_data="emergency")])

        await self._reply(update, "Панель керування торговим ботом", reply_markup=InlineKeyboardMarkup(keyboard))

    async def _cmd_emergency(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = [
            [InlineKeyboardButton("🚨 Тільки зупинити торгівлю", callback_data="emergency_stop_only")],
            [InlineKeyboardButton("🚨 Зупинити і закрити позиції", callback_data="emergency_stop_close")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
        ]
        await self._reply(update, "⚠️ Аварійна зупинка - Виберіть дію:", reply_markup=InlineKeyboardMarkup(keyboard))

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query

        try:
            await query.answer()
        except Exception as e:
            logger.warning(f"Failed to answer callback query (probably expired): {e}")

        if query.data == "emergency":
            await self._cmd_emergency(update, context)
        elif query.data == "export_db":
            await self._cmd_export_db(update, context)
        elif query.data == "cancel":
            await query.edit_message_text("❌ Скасовано")
        elif query.data == "emergency_stop_only":
            await self.settings_manager.activate_emergency_stop(close_positions=False)
            await query.edit_message_text("🚨 Аварійна зупинка активована - Торгівля вимкнена")
        elif query.data == "emergency_stop_close":
            await self.settings_manager.activate_emergency_stop(close_positions=True)
            await query.edit_message_text("🚨 Аварійна зупинка активована - Закриваємо всі позиції")

    async def _cmd_export_db(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Відправляє файл бази як документ у чат. Працювало лише для SQLite:
        у MongoDB-версії файлу бази немає (db_path відсутній)."""
        db_path = getattr(self.db, 'db_path', None)
        if not db_path:
            await self._reply(update, "❌ Експорт файлу бази недоступний: дані зберігаються в MongoDB")
            return

        try:
            if not os.path.exists(db_path):
                await self._reply(update, "❌ Файл бази даних не знайдено")
                return

            file_size = os.path.getsize(db_path)
            max_size = 50 * 1024 * 1024  # ліміт Telegram Bot API — 50 МБ
            if file_size > max_size:
                await self._reply(
                    update,
                    f"❌ Файл завеликий для відправки через Telegram ({file_size / 1024 / 1024:.1f} МБ, ліміт 50 МБ)"
                )
                return

            with open(db_path, 'rb') as db_file:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=db_file,
                    filename=os.path.basename(db_path) or 'trading_bot.db',
                    caption=f'📦 Резервна копія бази даних ({file_size / 1024:.1f} КБ)'
                )

        except Exception as e:
            logger.error(f"Error exporting database: {e}", exc_info=True)
            await self._reply(update, "❌ Помилка під час експорту бази даних")

    # ------------------------------------------------------------------
    # Проактивні алерти — не частина панелі, працюють самі по собі
    # ------------------------------------------------------------------

    async def _on_position_opened(self, event: Event) -> None:
        data = event.data
        photo_buf = generate_pnl_card(
            symbol=data.get('symbol', 'N/A'),
            side=data.get('side', 'N/A'),
            leverage=data.get('leverage') or 1,
            card_type="opened",
            entry_price=data.get('entry_price') or 0.0,
            margin_usdt=data.get('margin_usdt'),
            stop_loss_price=data.get('stop_loss_price'),
            take_profit_summary=messages.take_profit_summary(data.get('take_profit_levels')),
            account_label="User Account",
            closed_at=datetime.now(LOCAL_TZ),
            logo_crop_center=(0.5, 0.28),
        )
        await self._send_photo(photo_buf, messages.position_opened_caption(data))

    async def _on_position_closed(self, event: Event) -> None:
        data = event.data
        photo_buf = generate_pnl_card(
            symbol=data.get('symbol', 'N/A'),
            side=data.get('side', 'N/A'),
            leverage=data.get('leverage') or 1,
            card_type="closed",
            roe_percent=data.get('roe_percent') or 0.0,
            net_pnl=data.get('net_pnl'),
            entry_price=data.get('entry_price') or 0.0,
            close_price=data.get('close_price') or 0.0,
            account_label="N/A",
            closed_at=datetime.now(LOCAL_TZ),
            logo_crop_center=(0.5, 0.28),
        )
        await self._send_photo(photo_buf, messages.info_caption(data))

    async def _on_stop_loss_triggered(self, event: Event) -> None:
        await self.send_message(messages.stop_loss_triggered(event.data))

    async def _on_take_profit_triggered(self, event: Event) -> None:
        await self.send_message(messages.take_profit_triggered(event.data))

    async def _on_stop_loss_moved(self, event: Event) -> None:
        text = messages.stop_loss_moved(event.data)
        if text is None:
            logger.warning(f"STOP_LOSS_MOVED event missing price data: {event.data}")
            text = messages.stop_loss_moved_short(event.data)
        await self.send_message(text)

    async def _on_symbol_blacklisted(self, event: Event) -> None:
        await self.send_message(messages.symbol_blacklisted(event.data))

    async def _on_symbols_rotated(self, event: Event) -> None:
        await self.send_message(messages.symbols_rotated(event.data))

    async def _on_error(self, event: Event) -> None:
        await self.send_message(messages.error(event.data, "⚠️ <b>Помилка</b>", "Невідома помилка"))

    async def _on_critical_error(self, event: Event) -> None:
        await self.send_message(
            messages.error(event.data, "🚨 <b>КРИТИЧНА ПОМИЛКА</b>", "Невідома критична помилка")
        )

    async def notify_startup(self, *, testnet: bool, strategies: list) -> None:
        """Одноразове повідомлення після повного старту бота (біржа, стратегії,
        символи вже готові). Викликається з main.py наприкінці ініціалізації."""
        balance = None
        if self.exchange_client:
            try:
                account = extract_account_balance(await self.exchange_client.get_account_balance())
                if account is not None:
                    balance = to_float(account.get('balance', 0))
            except Exception as e:
                logger.warning(f"notify_startup: не вдалось отримати баланс: {e}")

        await self.send_message(messages.startup(
            testnet=testnet,
            enabled_strategies=messages.enabled_strategy_names(strategies),
            total_strategies=len(strategies),
            balance=balance,
            now=datetime.now(LOCAL_TZ),
        ))
