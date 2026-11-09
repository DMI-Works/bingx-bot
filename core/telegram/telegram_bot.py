import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from typing import Optional

from ..common.market_data import extract_account_balance, to_float
from ..events import EventBus, Event, EventType
from ..diagnostics import generate_pnl_card
from . import messages


LOCAL_TZ = ZoneInfo("Europe/Kyiv")

logger = logging.getLogger(__name__)


class TelegramBot:
    """Сповіщення ОДНОГО користувача: підписаний на його шину подій і шле в
    його чат (chat_id = Telegram user id). Сам бот Telegram (Application,
    polling, команди) — один на всіх, див. TelegramGateway."""

    def __init__(
        self,
        chat_id: str,
        event_bus: EventBus,
        gateway: "TelegramGateway",
        exchange_client=None,
    ):
        self.chat_id = chat_id
        self.event_bus = event_bus
        self.gateway = gateway
        self.exchange_client = exchange_client
        self.notifications_enabled = True

        self._subscribe_to_events()
        logger.info(f"TelegramBot notifier initialized (chat={chat_id})")

    @property
    def application(self) -> Optional[Application]:
        return self.gateway.application

    def _subscribe_to_events(self) -> None:
        handlers = {
            EventType.POSITION_OPENED: self._on_position_opened,
            EventType.POSITION_CLOSED: self._on_position_closed,
            EventType.STOP_LOSS_TRIGGERED: self._on_stop_loss_triggered,
            EventType.STOP_LOSS_MOVED: self._on_stop_loss_moved,
            EventType.TAKE_PROFIT_TRIGGERED: self._on_take_profit_triggered,
            EventType.ERROR: self._on_error,
            EventType.CRITICAL_ERROR: self._on_critical_error,
            EventType.SYMBOL_PAUSED: self._on_symbol_paused,
        }
        for event_type, handler in handlers.items():
            self.event_bus.subscribe(event_type, handler)

    async def send_message(self, text: str) -> None:
        if self.application and self.notifications_enabled:
            try:
                await self.application.bot.send_message(chat_id=self.chat_id, text=text, parse_mode='HTML')
            except Exception as e:
                logger.error(f"Failed to send Telegram message to {self.chat_id}: {e}")

    async def _send_photo(self, photo, caption: str) -> None:
        if not self.application:
            return
        try:
            await self.application.bot.send_photo(
                chat_id=self.chat_id, photo=photo, caption=caption, parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Failed to send Telegram photo to {self.chat_id}: {e}")

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

    async def _on_symbol_paused(self, event: Event) -> None:
        await self.send_message(messages.symbol_paused(event.data))

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


class TelegramGateway:
    """Єдиний бот Telegram на процес: polling, команди /start і /emergency.
    Команди діють для того, хто їх надіслав (сесія за Telegram user id)."""

    def __init__(self, token: str, session_lookup, admin_chat_id: Optional[str] = None,
                 shared_event_bus: Optional[EventBus] = None):
        self.token = token
        self.webapp_url = os.getenv('WEBAPP_URL', '')
        self.session_lookup = session_lookup  # user_id(str) -> UserSession | None
        self.admin_chat_id = admin_chat_id
        self.application: Optional[Application] = None
        if shared_event_bus is not None:
            # ротація спільного пулу монет — службова інформація, лише адміну
            shared_event_bus.subscribe(EventType.SYMBOLS_ROTATED, self._on_symbols_rotated)

    async def start(self) -> None:
        self.application = Application.builder().token(self.token).build()
        self.application.add_handler(CommandHandler("start", self._cmd_start))
        self.application.add_handler(CommandHandler("emergency", self._cmd_emergency))
        self.application.add_handler(CallbackQueryHandler(self._handle_callback))
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        logger.info("Telegram gateway started")

    async def stop(self) -> None:
        if self.application:
            await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
            logger.info("Telegram gateway stopped")

    async def send_to(self, chat_id: str, text: str) -> None:
        if not self.application:
            return
        try:
            await self.application.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Failed to send Telegram message to {chat_id}: {e}")

    async def _on_symbols_rotated(self, event: Event) -> None:
        if self.admin_chat_id:
            await self.send_to(self.admin_chat_id, messages.symbols_rotated(event.data))

    @staticmethod
    async def _reply(update: Update, text: str, **kwargs):
        if update.message:
            return await update.message.reply_text(text, **kwargs)
        if update.callback_query:
            return await update.callback_query.message.reply_text(text, **kwargs)

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = []
        if self.webapp_url:
            keyboard.append([InlineKeyboardButton("👤 Профіль", web_app=WebAppInfo(url=self.webapp_url))])
        else:
            logger.warning("WEBAPP_URL не задано — кнопка \"Профіль\" прихована")
        keyboard.append([InlineKeyboardButton("🚨 Аварійна зупинка", callback_data="emergency")])
        await self._reply(
            update,
            "Панель торгового бота. Відкрийте «Профіль», щоб підключити API-ключі BingX і керувати торгівлею.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _cmd_emergency(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = [
            [InlineKeyboardButton("🚨 Тільки зупинити торгівлю", callback_data="emergency_stop_only")],
            [InlineKeyboardButton("🚨 Зупинити і закрити позиції", callback_data="emergency_stop_close")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")],
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
            return
        if query.data == "cancel":
            await query.edit_message_text("❌ Скасовано")
            return
        if query.data not in ("emergency_stop_only", "emergency_stop_close"):
            return

        session = self.session_lookup(str(update.effective_user.id))
        if session is None:
            await query.edit_message_text("ℹ️ У вас немає активної торгової сесії — зупиняти нічого")
            return
        close = query.data == "emergency_stop_close"
        closed = await session.emergency_stop(close_positions=close)
        if close:
            await query.edit_message_text(f"🚨 Торгівлю вимкнено, закриваю позиції: {closed}")
        else:
            await query.edit_message_text("🚨 Аварійна зупинка активована - Торгівля вимкнена")
