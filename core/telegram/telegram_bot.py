import os
from pathlib import Path
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
        settings_manager: SettingsManager,
        exchange_client=None
    ):
        self.token = token
        self.chat_id = chat_id
        self.event_bus = event_bus
        self.position_manager = position_manager
        self.order_manager = order_manager
        self.settings_manager = settings_manager
        self.exchange_client = exchange_client

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
        self.application.add_handler(CommandHandler("balance", self._cmd_balance))
        self.application.add_handler(CommandHandler("positions", self._cmd_positions))
        self.application.add_handler(CommandHandler("settings", self._cmd_settings))
        self.application.add_handler(CommandHandler("emergency", self._cmd_emergency))
        self.application.add_handler(CommandHandler("export_db", self._cmd_export_db))
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
            [InlineKeyboardButton("📊 Статус", callback_data="status")],
            [InlineKeyboardButton("💰 Баланс", callback_data="balance")],
            [InlineKeyboardButton("📈 Позиції", callback_data="positions")],
            [InlineKeyboardButton("💾 Експорт бази", callback_data="export_db")],
            [InlineKeyboardButton("⚙️ Налаштування", callback_data="settings")],
            [InlineKeyboardButton("🚨 Аварійна зупинка", callback_data="emergency")]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await self._reply(update, "Панель керування торговим ботом", reply_markup=reply_markup)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        positions = self.position_manager.get_open_positions()
        orders = self.order_manager.get_open_orders()
        trading_enabled = self.settings_manager.get_trading_enabled()

        status_text = f"""
<b>📊 Статус бота</b>

Торгівля: {'✅ Увімкнено' if trading_enabled else '❌ Вимкнено'}
Відкриті позиції: {len(positions)}
Відкриті ордери: {len(orders)}
Всього маржі використано: ${self.position_manager.get_total_margin_used():.2f}
Всього нереалізований PnL: ${self.position_manager.get_total_unrealized_pnl():.2f}
"""

        await self._reply(update, status_text, parse_mode='HTML')

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.exchange_client:
            await self._reply(update, "❌ Клієнт біржі недоступний")
            return

        try:
            balance_data = await self.exchange_client.get_account_balance()
            print(f"Balance Data: {balance_data}")  # Debugging line to check the response structure
            if balance_data.get('code') == 0 and 'data' in balance_data:
                data = balance_data['data']
                balance = data.get('balance', {})

                balance_text = f"""
<b>💰 Баланс рахунку</b>

Доступний баланс: ${float(balance.get('availableMargin', 0)):.2f}
Загальний баланс: ${float(balance.get('balance', 0)):.2f}
Нереалізований PnL: ${float(balance.get('unrealizedProfit', 0)):.2f}
Використана маржа: ${float(balance.get('usedMargin', 0)):.2f}
Капітал: ${float(balance.get('equity', 0)):.2f}
"""
                await self._reply(update, balance_text, parse_mode='HTML')
            else:
                await self._reply(update, f"❌ Не вдалося отримати баланс: {balance_data.get('msg', 'Невідома помилка')}")

        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
            await self._reply(update, f"❌ Помилка: {str(e)}")

    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.exchange_client:
            await self._reply(update, "❌ Клієнт біржі недоступний")
            return

        try:
            positions_data = await self.exchange_client.get_positions()

            if not positions_data:
                await self._reply(update, "📭 Немає відкритих позицій")
                return

            # Фільтруємо тільки активні позиції
            active_positions = [pos for pos in positions_data if float(pos.get('positionAmt', 0)) != 0]

            if not active_positions:
                await self._reply(update, "📭 Немає відкритих позицій")
                return

            text = "<b>📈 Відкриті позиції на BingX</b>\n\n"
            total_unrealized_pnl = 0

            for pos in active_positions:
                symbol = pos.get('symbol', 'N/A')
                position_side = pos.get('positionSide', 'N/A')
                position_amt = float(pos.get('positionAmt', 0))
                entry_price = float(pos.get('avgPrice', 0))
                mark_price = float(pos.get('markPrice', 0))
                unrealized_pnl = float(pos.get('unrealizedProfit', 0))
                leverage = int(pos.get('leverage', 1))
                isolated_margin = float(pos.get('isolatedMargin', 0))

                # Розрахунок ROE%
                if isolated_margin > 0:
                    roe = (unrealized_pnl / isolated_margin) * 100
                else:
                    roe = 0

                total_unrealized_pnl += unrealized_pnl

                # Емодзі для прибутку/збитку
                pnl_emoji = "🟢" if unrealized_pnl >= 0 else "🔴"
                side_emoji = "🟢" if position_side == "LONG" else "🔴"

                text += f"""
{side_emoji} <b>{symbol}</b> {position_side} {leverage}x
├ Вхід: <code>${entry_price:.4f}</code>
├ Поточна: <code>${mark_price:.4f}</code>
├ Кількість: <code>{abs(position_amt)}</code>
├ Маржа: <code>${isolated_margin:.2f}</code>
├ {pnl_emoji} PnL: <b>${unrealized_pnl:+.2f}</b>
└ ROE: <b>{roe:+.2f}%</b>

"""

            # Підсумок
            summary_emoji = "🟢" if total_unrealized_pnl >= 0 else "🔴"
            text += f"""
━━━━━━━━━━━━━━━━━━━━
{summary_emoji} <b>Загальний нереалізований PnL: ${total_unrealized_pnl:+.2f}</b>
Всього позицій: {len(active_positions)}
"""

            await self._reply(update, text, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Error fetching positions: {e}", exc_info=True)
            await self._reply(update, f"❌ Помилка отримання позицій: {str(e)}")

    async def _cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = [
            [InlineKeyboardButton("🔄 Перемкнути торгівлю", callback_data="toggle_trading")],
            [InlineKeyboardButton("📋 Білий список", callback_data="whitelist")],
            [InlineKeyboardButton("🛡️ Налаштування ризику", callback_data="risk_settings")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back")]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await self._reply(update, "⚙️ Налаштування", reply_markup=reply_markup)

    async def _cmd_emergency(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        keyboard = [
            [InlineKeyboardButton("🚨 Тільки зупинити торгівлю", callback_data="emergency_stop_only")],
            [InlineKeyboardButton("🚨 Зупинити і закрити позиції", callback_data="emergency_stop_close")],
            [InlineKeyboardButton("❌ Скасувати", callback_data="cancel")]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)
        await self._reply(update, "⚠️ Аварійна зупинка - Виберіть дію:", reply_markup=reply_markup)

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()

        if query.data == "status":
            await self._cmd_status(update, context)
        elif query.data == "balance":
            await self._cmd_balance(update, context)
        elif query.data == "positions":
            await self._cmd_positions(update, context)
        elif query.data == "settings":
            await self._cmd_settings(update, context)
        elif query.data == "toggle_trading":
            enabled = self.settings_manager.get_trading_enabled()
            await self.settings_manager.set_trading_enabled(not enabled)
            await query.edit_message_text(f"Торгівля {'Вимкнено' if enabled else 'Увімкнено'}")
        elif query.data == "emergency_stop_only":
            await self.settings_manager.activate_emergency_stop(close_positions=False)
            await query.edit_message_text("🚨 Аварійна зупинка активована - Торгівля вимкнена")
        elif query.data == "emergency_stop_close":
            await self.settings_manager.activate_emergency_stop(close_positions=True)
            await query.edit_message_text("🚨 Аварійна зупинка активована - Закриваємо всі позиції")
        elif query.data == "export_db":
            await self._cmd_export_db(update, context)

    async def _on_position_opened(self, event: Event) -> None:
        data = event.data

        text = f"""
✅ <b>Позицію відкрито</b>

Символ: {data['symbol']}
Напрямок: {data['side']}
Вхід: ${data['entry_price']:.4f}
Кількість: {data['quantity']}
Плече: {data['leverage']}x
"""
        if data.get('stop_loss_price'):
            text += f"Stop Loss: ${data['stop_loss_price']:.4f}\n"

        await self.send_message(text)

    async def _on_position_closed(self, event: Event) -> None:
        data = event.data
        pnl = data.get('realized_pnl', 0)
        emoji = "🟢" if pnl > 0 else "🔴"

        text = f"""
{emoji} <b>Позицію закрито</b>

Символ: {data['symbol']}
Напрямок: {data['side']}
Ціна закриття: ${data.get('close_price', 0):.4f}
Реалізований PnL: ${pnl:.2f}
"""
        await self.send_message(text)

    async def _on_stop_loss_triggered(self, event: Event) -> None:
        text = f"""
🛑 <b>Спрацював стоп-лосс</b>

Символ: {event.data.get('symbol')}
Ціна: ${event.data.get('price', 0):.4f}
"""
        await self.send_message(text)

    async def _on_take_profit_triggered(self, event: Event) -> None:
        text = f"""
🎯 <b>Досягнуто тейк-профіт</b>

Символ: {event.data.get('symbol')}
Рівень: {event.data.get('level', 1)}
Ціна: ${event.data.get('price', 0):.4f}
"""
        await self.send_message(text)

    async def _on_error(self, event: Event) -> None:
        text = f"""
⚠️ <b>Помилка</b>

{event.data.get('error', 'Невідома помилка')}
Контекст: {event.data.get('context', 'N/A')}
"""
        await self.send_message(text)

    async def _on_critical_error(self, event: Event) -> None:
        text = f"""
🚨 <b>КРИТИЧНА ПОМИЛКА</b>

{event.data.get('error', 'Невідома критична помилка')}
"""
        await self.send_message(text)

    

    async def _cmd_export_db(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        DB_PATH = "data/trading_bot.db"

        try:
            if not os.path.exists(DB_PATH):
                await self._reply(update, "❌ Файл бази даних не знайдено")
                return

            file_size = os.path.getsize(DB_PATH)
            max_size = 50 * 1024 * 1024  # ліміт Telegram Bot API — 50 МБ

            if file_size > max_size:
                await self._reply(
                    update,
                    f"❌ Файл завеликий для відправки через Telegram ({file_size / 1024 / 1024:.1f} МБ, ліміт 50 МБ)"
                )
                return

            with open(DB_PATH, 'rb') as db_file:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=db_file,
                    filename='trading_bot.db',
                    caption=f'📦 Резервна копія бази даних ({file_size / 1024:.1f} КБ)'
                )

        except Exception as e:
            logger.error(f"Error exporting database: {e}", exc_info=True)
            await self._reply(update, f"❌ Помилка експорту бази: {str(e)}")