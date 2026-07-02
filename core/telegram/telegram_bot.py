import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from typing import Optional

from ..events import EventBus, Event, EventType
from ..state import PositionManager, SettingsManager
from ..execution import OrderManager


logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(
        self,
        token: str,
        chat_id: str,
        event_bus: EventBus,
        position_manager: PositionManager,
        order_manager: OrderManager,
        settings_manager: SettingsManager
    ):
        self.token = token
        self.chat_id = chat_id
        self.event_bus = event_bus
        self.position_manager = position_manager
        self.order_manager = order_manager
        self.settings_manager = settings_manager

        self.application: Optional[Application] = None
        self.notifications_enabled = True

        self._subscribe_to_events()
        logger.info("TelegramBot initialized")

    def _subscribe_to_events(self) -> None:
        self.event_bus.subscribe(EventType.POSITION_OPENED, self._on_position_opened)
        self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_closed)
        self.event_bus.subscribe(EventType.STOP_LOSS_TRIGGERED, self._on_stop_loss_triggered)
        self.event_bus.subscribe(EventType.TAKE_PROFIT_TRIGGERED, self._on_take_profit_triggered)
        self.event_bus.subscribe(EventType.ERROR, self._on_error)
        self.event_bus.subscribe(EventType.CRITICAL_ERROR, self._on_critical_error)

    async def start(self) -> None:
        self.application = Application.builder().token(self.token).build()

        self.application.add_handler(CommandHandler("start", self._cmd_start))
        self.application.add_handler(CommandHandler("status", self._cmd_status))
        self.application.add_handler(CommandHandler("positions", self._cmd_positions))
        self.application.add_handler(CommandHandler("settings", self._cmd_settings))
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

    async def _reply(self, update: Update, text: str, **kwargs):
        if update.message:
            return await update.message.reply_text(text, **kwargs)

        if update.callback_query:
            return await update.callback_query.message.reply_text(text, **kwargs)
            
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = [
            [InlineKeyboardButton("📊 Status", callback_data="status")],
            [InlineKeyboardButton("📈 Positions", callback_data="positions")],
            [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("🚨 Emergency Stop", callback_data="emergency")]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await self._reply(update, "Trading Bot Control Panel", reply_markup=reply_markup)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        positions = self.position_manager.get_open_positions()
        orders = self.order_manager.get_open_orders()
        trading_enabled = self.settings_manager.get_trading_enabled()

        status_text = f"""
<b>📊 Bot Status</b>

Trading: {'✅ Enabled' if trading_enabled else '❌ Disabled'}
Open Positions: {len(positions)}
Open Orders: {len(orders)}
Total Margin Used: ${self.position_manager.get_total_margin_used():.2f}
Total Unrealized PnL: ${self.position_manager.get_total_unrealized_pnl():.2f}
"""

        await self._reply(update, status_text, parse_mode='HTML')

    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        positions = self.position_manager.get_open_positions()

        if not positions:
            await self._reply(update, "No open positions")
            return

        text = "<b>📈 Open Positions</b>\n\n"

        for pos in positions:
            text += f"""
<b>{pos.symbol}</b> {pos.side.value}
Entry: ${pos.entry_price:.4f}
Quantity: {pos.quantity}
Leverage: {pos.leverage}x
Unrealized PnL: ${pos.unrealized_pnl:.2f}
ROI: {pos.roi:.2f}%
Stop Loss: ${pos.stop_loss_price:.4f if pos.stop_loss_price else 'N/A'}
---
"""

        await self._reply(update, text, parse_mode='HTML')

    async def _cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = [
            [InlineKeyboardButton("🔄 Toggle Trading", callback_data="toggle_trading")],
            [InlineKeyboardButton("📋 Whitelist", callback_data="whitelist")],
            [InlineKeyboardButton("🛡️ Risk Settings", callback_data="risk_settings")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back")]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await self._reply(update, "⚙️ Settings", reply_markup=reply_markup)

    async def _cmd_emergency(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = [
            [InlineKeyboardButton("🚨 Stop Trading Only", callback_data="emergency_stop_only")],
            [InlineKeyboardButton("🚨 Stop & Close Positions", callback_data="emergency_stop_close")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await self._reply(update, "⚠️ Emergency Stop - Choose action:", reply_markup=reply_markup)

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        if query.data == "status":
            await self._cmd_status(update, context)
        elif query.data == "positions":
            await self._cmd_positions(update, context)
        elif query.data == "settings":
            await self._cmd_settings(update, context)
        elif query.data == "toggle_trading":
            enabled = self.settings_manager.get_trading_enabled()
            await self.settings_manager.set_trading_enabled(not enabled)
            await query.edit_message_text(f"Trading {'Disabled' if enabled else 'Enabled'}")
        elif query.data == "emergency_stop_only":
            await self.settings_manager.activate_emergency_stop(close_positions=False)
            await query.edit_message_text("🚨 Emergency Stop Activated - Trading Disabled")
        elif query.data == "emergency_stop_close":
            await self.settings_manager.activate_emergency_stop(close_positions=True)
            await query.edit_message_text("🚨 Emergency Stop Activated - Closing All Positions")

    async def _on_position_opened(self, event: Event) -> None:
        data = event.data
        text = f"""
✅ <b>Position Opened</b>

Symbol: {data['symbol']}
Side: {data['side']}
Entry: ${data['entry_price']:.4f}
Quantity: {data['quantity']}
Leverage: {data['leverage']}x
Margin: ${data['margin']:.2f}
"""
        await self.send_message(text)

    async def _on_position_closed(self, event: Event) -> None:
        data = event.data
        pnl = data.get('realized_pnl', 0)
        emoji = "🟢" if pnl > 0 else "🔴"

        text = f"""
{emoji} <b>Position Closed</b>

Symbol: {data['symbol']}
Side: {data['side']}
Close Price: ${data.get('close_price', 0):.4f}
Realized PnL: ${pnl:.2f}
"""
        await self.send_message(text)

    async def _on_stop_loss_triggered(self, event: Event) -> None:
        text = f"""
🛑 <b>Stop Loss Triggered</b>

Symbol: {event.data.get('symbol')}
Price: ${event.data.get('price', 0):.4f}
"""
        await self.send_message(text)

    async def _on_take_profit_triggered(self, event: Event) -> None:
        text = f"""
🎯 <b>Take Profit Hit</b>

Symbol: {event.data.get('symbol')}
Level: {event.data.get('level', 1)}
Price: ${event.data.get('price', 0):.4f}
"""
        await self.send_message(text)

    async def _on_error(self, event: Event) -> None:
        text = f"""
⚠️ <b>Error</b>

{event.data.get('error', 'Unknown error')}
Context: {event.data.get('context', 'N/A')}
"""
        await self.send_message(text)

    async def _on_critical_error(self, event: Event) -> None:
        text = f"""
🚨 <b>CRITICAL ERROR</b>

{event.data.get('error', 'Unknown critical error')}
"""
        await self.send_message(text)

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            balance = await self.exchange.get_balance()

            text = f"""
    <b>💰 Account Balance</b>

    Available: {balance['availableMargin']} USDT
    Balance: {balance['balance']} USDT
    Equity: {balance['equity']} USDT
    Unrealized PnL: {balance['unrealizedProfit']} USDT
    """

            await self._reply(update, text, parse_mode="HTML")

        except Exception as e:
            logger.exception(e)
            await self._reply(update, f"❌ Failed to get balance\n\n{e}")