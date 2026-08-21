import os
import json
from pathlib import Path
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from typing import Optional

from ..events import EventBus, Event, EventType
from ..state import SettingsManager
from ..database import StrategySettingsStore
from ..diagnostics import generate_pnl_card, generate_stats_card

from .settings_menu import SettingsMenu, StrategySchema, ParamSpec


LOCAL_TZ = ZoneInfo("Europe/Kyiv")
PAGE_SIZE = 5

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(
        self,
        token: str,
        chat_id: str,
        event_bus: EventBus,
        db,
        settings_manager: SettingsManager,
        exchange_client=None,
        symbol_selector=None,
        strategy_settings: StrategySettingsStore = None,
        strategy_manager=None,
    ):
        self.token = token
        self.chat_id = chat_id
        self.event_bus = event_bus
        self.db = db
        self.settings_manager = settings_manager
        self.exchange_client = exchange_client
        self.symbol_selector = symbol_selector
        self.strategy_manager = strategy_manager

        self.application: Optional[Application] = None
        self.notifications_enabled = True

        self._subscribe_to_events()
        logger.info("TelegramBot initialized")

        if strategy_settings is None:
            raise ValueError("TelegramBot requires strategy_settings (StrategySettingsStore) to be passed in")

        self.strategy_settings_store = strategy_settings
        self.strategy_schemas: dict = {}  
        self.settings_menu = SettingsMenu(
            self.strategy_settings_store,
            self.strategy_schemas,
            strategy_manager=self.strategy_manager,
        )

    def _subscribe_to_events(self) -> None:
        self.event_bus.subscribe(EventType.POSITION_OPENED, self._on_position_opened)
        self.event_bus.subscribe(EventType.POSITION_CLOSED, self._on_position_closed)
        self.event_bus.subscribe(EventType.STOP_LOSS_TRIGGERED, self._on_stop_loss_triggered)
        self.event_bus.subscribe(EventType.STOP_LOSS_MOVED, self._on_stop_loss_moved)
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
        self.application.add_handler(CommandHandler("symbols", self._cmd_symbols))
        self.application.add_handler(CommandHandler("history", self._cmd_history))

        # ВАЖЛИВО: реєструємо меню налаштувань РАНІШЕ загального CallbackQueryHandler.
        # PTB виконує лише перший хендлер у групі, чий check_update спрацював -
        # якщо спочатку зареєструвати _handle_callback (він без pattern і ловить
        # усе підряд), callback'и "sm:..." від меню налаштувань до нього просто
        # не дійдуть.
        self.settings_menu.register(self.application)
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
            [InlineKeyboardButton("📜 Історія угод", callback_data="history_page_0")],
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
├ Вхід: <code>${entry_price:.6f}</code>
├ Поточна: <code>${mark_price:.6f}</code>
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
            [InlineKeyboardButton("📋 Підписані символи", callback_data="symbols")],
            [InlineKeyboardButton("🛡️ Налаштування ризику", callback_data="risk_settings")],
            [InlineKeyboardButton("🧪 Параметри стратегій", callback_data=self.settings_menu.root_callback())],
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

        try:
            await query.answer()
        except Exception as e:
            logger.warning(f"Failed to answer callback query (probably expired): {e}")

        if query.data == "status":
            await self._cmd_status(update, context)
        elif query.data == "balance":
            await self._cmd_balance(update, context)
        elif query.data == "positions":
            await self._cmd_positions(update, context)
        elif query.data == "settings":
            await self._cmd_settings(update, context)
        elif query.data == "export_db":
            await self._cmd_export_db(update, context)
        elif query.data == "symbols":
            await self._cmd_symbols(update, context)
        elif query.data == "risk_settings":
            await self._cmd_risk_settings(update, context)
        elif query.data == "back":
            await self._cmd_start(update, context)
        elif query.data == "cancel":
            await query.edit_message_text("❌ Скасовано")
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
        elif query.data.startswith("history_page_"):
            page = int(query.data.replace("history_page_", ""))
            await self._show_history_page(update, context, page)

    async def _on_position_opened(self, event: Event) -> None:
        data = event.data

        tp_levels = data.get('take_profit_levels') or []

        lines = ["✅ <b>Позицію відкрито</b>", ""]

        if tp_levels:
            lines.append("Тейк:")
            for i, tp in enumerate(tp_levels, start=1):
                prefix = "└" if i == len(tp_levels) else "  ├"
                lines.append(f"{prefix} {i}: 💰{tp['price']:.6f} ({tp.get('close_percent', 0)}%)")

        if data.get('stop_loss_price'):
            lines.append(f"Стоп: ${data['stop_loss_price']:.6f}")

        lines.append("")
        lines.append(f"[INFO]: {data.get('positions_info_message', 'N/A')}")

        text = "\n".join(lines)

        tp_summary = f"{len(tp_levels)} рівні" if len(tp_levels) > 1 else (
            f"{tp_levels[0]['price']:.6f}" if tp_levels else None
        )

        photo_buf = generate_pnl_card(
            symbol=data.get('symbol', 'N/A'),
            side=data.get('side', 'N/A'),
            leverage=data.get('leverage') or 1,
            card_type="opened",
            entry_price=data.get('entry_price') or 0.0,
            margin_usdt=data.get('margin_usdt'),
            stop_loss_price=data.get('stop_loss_price'),
            take_profit_summary=tp_summary,
            account_label="User Account",
            closed_at=datetime.now(LOCAL_TZ),
            logo_crop_center=(0.5, 0.28),
        )

        await self.application.bot.send_photo(
            chat_id=self.chat_id,
            photo=photo_buf,
            caption=text,
            parse_mode='HTML'
        )

    async def _on_position_closed(self, event: Event) -> None:
        data = event.data

        symbol = data.get('symbol', 'N/A')
        side = data.get('side', 'N/A')
        leverage = data.get('leverage') or 1
        roe = data.get('roe_percent') or 0.0
        entry_price = data.get('entry_price') or 0.0
        close_price = data.get('close_price') or 0.0
        net_pnl = data.get('net_pnl')

        caption = f"[INFO]: {data.get('positions_info_message', 'N/A')}"

        photo_buf = generate_pnl_card(
            symbol=symbol,
            side=side,
            leverage=leverage,
            card_type="closed",
            roe_percent=roe,
            net_pnl=net_pnl,
            entry_price=entry_price,
            close_price=close_price,
            account_label="N/A",
            closed_at=datetime.now(LOCAL_TZ),
            logo_crop_center=(0.5, 0.28),
        )

        await self.application.bot.send_photo(
            chat_id=self.chat_id,
            photo=photo_buf,
            caption=caption,
            parse_mode='HTML'
        )

    async def _on_stop_loss_triggered(self, event: Event) -> None:
        text = f"""
🛑 <b>Спрацював стоп-лосс</b>

Символ: {event.data.get('symbol')}
Ціна: ${event.data.get('price', 0):.6f}
[INFO]: {event.data.get('positions_info_message')}
"""
        await self.send_message(text)

    async def _on_stop_loss_moved(self, event: Event) -> None:
        data = event.data

        symbol = data.get('symbol', 'N/A')
        side = data.get('side', 'N/A')
        stage = data.get('stage')
        old_price = data.get('old_stop_price')
        new_price = data.get('new_stop_price')
        entry_price = data.get('entry_price')
        leverage = data.get('leverage') or 1
        strategy = data.get('strategy')

        stage_label = {
            'breakeven': '🟡 Перенесено в беззбиток',
            'trailing': '🟢 Підтягнуто трейлінгом',
        }.get(stage, '🔄 Стоп перенесено')

        side_emoji = "🟢" if side == "LONG" else "🔴"

        if old_price is None or new_price is None or entry_price is None:
            logger.warning(f"STOP_LOSS_MOVED event missing price data: {data}")
            await self.send_message(f"{stage_label}\n\n{side_emoji} <b>{symbol}</b> {side}")
            return

        try:
            leverage = float(leverage)
        except (TypeError, ValueError):
            leverage = 1.0
        if leverage <= 0:
            leverage = 1.0

        ref = abs(entry_price or 0)
        decimals = 4 if ref >= 1 else (6 if ref >= 0.01 else 8)

        def _roi_percent(stop_price: float) -> float:
            fraction = (
                (stop_price - entry_price) / entry_price if side == 'LONG'
                else (entry_price - stop_price) / entry_price
            )
            return fraction * leverage * 100.0

        old_roi = _roi_percent(old_price)
        new_roi = _roi_percent(new_price)

        text = f"""
    {stage_label}

    {side_emoji} <b>{symbol}</b> {side}
    ├ Вхід: <code>${entry_price:.{decimals}f}</code>
    ├ Старий стоп: <code>{old_roi:+.2f}% ROI</code>
    └ Новий стоп: <code>{new_roi:+.2f}% ROI</code>
    """
        if strategy:
            text += f"\n[INFO]: Стратегія: {strategy}"

        await self.send_message(text)

    async def _on_take_profit_triggered(self, event: Event) -> None:
        text = f"""
🎯 <b>Досягнуто тейк-профіт</b>

Символ: {event.data.get('symbol')}
Рівень: {event.data.get('level', 1)}
Ціна: ${event.data.get('price', 0):.6f}
[INFO]: {event.data.get('positions_info_message')}
"""
        await self.send_message(text)

    async def _on_error(self, event: Event) -> None:
        context = event.data.get('context')
        error = event.data.get('error', 'Невідома помилка')
        text = f"⚠️ <b>Помилка</b>: {context or error}"
        if context and error:
            text += f"\n<code>{error}</code>"
        await self.send_message(text)

    async def _on_critical_error(self, event: Event) -> None:
        context = event.data.get('context')
        error = event.data.get('error', 'Невідома критична помилка')
        text = f"🚨 <b>КРИТИЧНА ПОМИЛКА</b>: {context or error}"
        if context and error:
            text += f"\n<code>{error}</code>"
        await self.send_message(text)

    async def notify_startup(
        self,
        *,
        testnet: bool,
        strategies: list,
    ) -> None:
        """
        Відправляє одноразове вітальне повідомлення одразу після повного
        старту бота (біржа, стратегії, символи вже готові). Викликається
        явно з main.py в самому кінці ініціалізації — жодних побічних
        ефектів на існуючу логіку не має, це чиста нотифікація.
        """

        mode_label = "🧪 Тестовий" if testnet else "🔴 LIVE (реальні кошти)"

        enabled_names = [s.name for s in strategies if s.is_enabled()]
        total_count = len(strategies)
        enabled_count = len(enabled_names)

        lines = [
            "🚀 <b>Бот запущено та готовий до роботи</b>",
            "",
            f"Режим: {mode_label}",
            f"Стратегій активно: {enabled_count}/{total_count}",
        ]

        if enabled_names:
            lines.append("")
            lines.append("Активні стратегії:")
            for name in enabled_names:
                lines.append(f"  • {name}")

        if self.exchange_client:
            try:
                balance_data = await self.exchange_client.get_account_balance()
                if balance_data.get('code') == 0 and 'data' in balance_data:
                    balance = balance_data['data'].get('balance', {})
                    lines.append("")
                    lines.append(f"💰 Баланс: ${float(balance.get('balance', 0)):.2f}")
            except Exception as e:
                logger.warning(f"notify_startup: не вдалось отримати баланс: {e}")

        lines.append("")
        lines.append(f"🕐 {datetime.now(LOCAL_TZ).strftime('%d.%m.%Y %H:%M:%S')}")

        await self.send_message("\n".join(lines))

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
    
    async def _cmd_symbols(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.symbol_selector:
            await self._reply(update, "❌ Символ-селектор недоступний")
            return

        symbols = sorted(self.symbol_selector.current_symbols)

        if not symbols:
            await self._reply(update, "📭 Немає підписаних символів")
            return

        text = f"<b>📋 Підписані символи ({len(symbols)})</b>\n\n"
        text += "\n".join(f"• {s}" for s in symbols)

        await self._reply(update, text, parse_mode='HTML')

    async def _cmd_risk_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = "🛡️ <b>Налаштування ризику</b>\n\nРедагування поки доступне тільки через конфіг-файл."
        await self._reply(update, text, parse_mode='HTML')

    async def _cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show_history_page(update, context, page=0)

    def _build_stats_card(self):
        """Агрегує всі закриті угоди в картку статистики (PnL по монетах, +/-, разом,
        та Win/Loss по стратегіях, витягнутих з metadata['strategy'])."""
        try:
            all_closed = self.db.get_all_closed_positions()
        except Exception as e:
            logger.error(f"Failed to fetch closed positions for stats card: {e}", exc_info=True)
            return None

        total_trades = 0
        profitable_count = 0
        losing_count = 0
        total_pnl = 0.0
        symbol_pnl: dict = {}
        strategy_counts: dict = {}  # strategy_name -> [win, loss]

        for row in all_closed:
            net = row['net_pnl'] if 'net_pnl' in row.keys() and row['net_pnl'] is not None else float(row['realized_pnl'] or 0.0)
            pnl = net  # net_pnl вже враховує комісію

            symbol = row['symbol']
            total_trades += 1
            total_pnl += pnl
            symbol_pnl[symbol] = symbol_pnl.get(symbol, 0.0) + pnl

            is_win = pnl >= 0
            if is_win:
                profitable_count += 1
            else:
                losing_count += 1

            # --- ім'я стратегії з metadata['strategy'] (записується при відкритті позиції) ---
            try:
                meta = json.loads(row['metadata']) if row['metadata'] else {}
            except (TypeError, ValueError):
                meta = {}
            strategy_name = meta.get('strategy') or "Невідомо"

            counts = strategy_counts.setdefault(strategy_name, [0, 0])
            counts[0 if is_win else 1] += 1

        if total_trades == 0:
            return None

        strategy_stats = [(name, win, loss) for name, (win, loss) in strategy_counts.items()]

        try:
            return generate_stats_card(
                total_trades=total_trades,
                profitable_count=profitable_count,
                losing_count=losing_count,
                total_pnl=total_pnl,
                symbol_pnl=list(symbol_pnl.items()),
                strategy_stats=strategy_stats,
                generated_at=datetime.now(LOCAL_TZ),
            )
        except Exception as e:
            logger.error(f"Failed to generate stats card: {e}", exc_info=True)
            return None

    async def _show_history_page(self, update: Update, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
        offset = page * PAGE_SIZE

        try:
            rows = self.db.get_closed_positions(limit=PAGE_SIZE, offset=offset)
            total_count = self.db.get_closed_positions_count()
        except Exception as e:
            logger.error(f"Failed to fetch position history: {e}", exc_info=True)
            await self._reply(update, f"❌ Помилка отримання історії: {str(e)}")
            return

        total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)

        if page == 0:
            stats_buf = self._build_stats_card()
            if stats_buf:
                try:
                    await self.application.bot.send_photo(chat_id=self.chat_id, photo=stats_buf)
                except Exception as e:
                    logger.error(f"Failed to send stats card: {e}", exc_info=True)

        def parse_metadata(row) -> dict:
            try:
                return json.loads(row['metadata']) if row['metadata'] else {}
            except (TypeError, ValueError):
                return {}

        def format_sl_tp_lines(metadata: dict, entry_price: float, side: str) -> list:
            """Формує список рядків SL/TP для картки угоди (кожен рівень — окремий рядок)"""
            lines = []

            sl_price = metadata.get('stop_loss_price')
            if sl_price:
                sl_price = float(sl_price)
                if entry_price:
                    sl_percent = (
                        (entry_price - sl_price) / entry_price * 100 if side == 'LONG'
                        else (sl_price - entry_price) / entry_price * 100
                    )
                    lines.append(f"🔴 SL: <code>${sl_price:.6f}</code> (-{sl_percent:.1f}%)")
                else:
                    lines.append(f"🔴 SL: <code>${sl_price:.6f}</code>")

            tp_levels = metadata.get('take_profit_levels') or []
            if tp_levels:
                lines.append("🟢 TP:")
                for i, lvl in enumerate(tp_levels):
                    tp_price = float(lvl.get('price', 0))
                    close_pct = lvl.get('close_percent', 0)
                    prefix = "└" if i == len(tp_levels) - 1 else "  ├"
                    
                    lines.append(f"{prefix} <code>${tp_price:.6f}</code> ({close_pct}%)")

            return lines

        text = "<b>📜 Історія угод</b>\n━━━━━━━━━━━━━━━━━━━━\n"

        if not rows:
            text += "\n📭 Немає закритих позицій на цій сторінці"
        else:
            for row in rows:
                metadata = parse_metadata(row)
                entry_price = metadata.get('entry_price') or 0.0
                quantity = metadata.get('quantity')
                leverage = metadata.get('leverage')
                order_id = row['order_id']
                side = row['side']

                pnl = row['realized_pnl'] if row['realized_pnl'] is not None else 0.0
                roe = row['roe_percent']
                close_price = row['close_price'] if row['close_price'] is not None else 0.0
                margin_usdt = row['margin_usdt'] if 'margin_usdt' in row.keys() else None
                commission_usdt = row['commission_usdt'] if 'commission_usdt' in row.keys() else None
                net_pnl = row['net_pnl'] if 'net_pnl' in row.keys() else None

                emoji = "🟢" if pnl >= 0 else "🔴"

                closed_at_raw = row['closed_at']
                try:
                    closed_at_utc = datetime.fromisoformat(str(closed_at_raw)).replace(tzinfo=ZoneInfo("UTC"))
                    closed_at_local = closed_at_utc.astimezone(LOCAL_TZ)
                    closed_at = closed_at_local.strftime('%d.%m %H:%M')
                except (ValueError, TypeError):
                    closed_at = str(closed_at_raw)

                closed_by = metadata.get('closed_by', '?')

                text += f"""
            {emoji} <b>{row['symbol']}</b> {side}{f" {leverage}x" if leverage else ""}
            ├ Вхід: <code>${entry_price:.6f}</code>
            ├ Закрито: <code>${close_price:.6f}</code>
            """
                if quantity:
                    text += f"├ Кількість: <code>{quantity}</code>\n"
                if margin_usdt is not None:
                    text += f"├ Маржа: <code>${float(margin_usdt):.2f}</code>\n"
                text += f"├ Час: <code>{closed_at}</code> ({closed_by})\n"

                sl_tp_lines = format_sl_tp_lines(metadata, entry_price, side)
                for line in sl_tp_lines:
                    text += f"├ {line}\n"

                if roe is not None:
                    text += f"├ ROE: <b>{roe:+.2f}%</b>\n"
                if commission_usdt is not None:
                    text += f"├ Комісія: <code>${float(commission_usdt):.6f}</code>\n"
                text += f"├ № ордера: <code>{order_id}</code>\n"
                text += f"└ PnL: <b>${pnl:+.2f}</b>"
                if net_pnl is not None:
                    text += f" (чистий: ${float(net_pnl):+.2f})"
                text += "\n"

        text += f"\n━━━━━━━━━━━━━━━━━━━━\nСторінка {page + 1} з {total_pages}"

        keyboard_row = []
        if page > 0:
            keyboard_row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"history_page_{page - 1}"))
        if page < total_pages - 1:
            keyboard_row.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"history_page_{page + 1}"))

        keyboard = [keyboard_row] if keyboard_row else []
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)
        else:
            await self._reply(update, text, parse_mode='HTML', reply_markup=reply_markup)