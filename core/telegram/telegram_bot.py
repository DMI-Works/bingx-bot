import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from typing import Optional

from ..events import EventBus, Event, EventType
from ..state import SettingsManager
from ..diagnostics import generate_pnl_card


LOCAL_TZ = ZoneInfo("Europe/Kyiv")

logger = logging.getLogger(__name__)


class TelegramBot:
    """
    Панель бота звужена навмисно: єдина точка керування угодами тепер —
    мініапп (кнопка "Профіль"). Тут лишається тільки те, що мусить бути
    доступне миттєво з чату навіть якщо мініапп з якоїсь причини не
    відкривається — аварійна зупинка — та проактивні алерти (відкриття/
    закриття позицій, SL/TP, помилки), які продовжують прилітати самі,
    без запиту користувача.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        event_bus: EventBus,
        settings_manager: SettingsManager,
        exchange_client=None,
    ):
        self.webapp_url = os.getenv('WEBAPP_URL', '')
        self.token = token
        self.chat_id = chat_id
        self.event_bus = event_bus
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
        self.event_bus.subscribe(EventType.STOP_LOSS_MOVED, self._on_stop_loss_moved)
        self.event_bus.subscribe(EventType.TAKE_PROFIT_TRIGGERED, self._on_take_profit_triggered)
        self.event_bus.subscribe(EventType.ERROR, self._on_error)
        self.event_bus.subscribe(EventType.CRITICAL_ERROR, self._on_critical_error)
        self.event_bus.subscribe(EventType.SYMBOLS_ROTATED, self._on_symbols_rotated)

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

        keyboard.append([InlineKeyboardButton("🚨 Аварійна зупинка", callback_data="emergency")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await self._reply(update, "Панель керування торговим ботом", reply_markup=reply_markup)

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

        if query.data == "emergency":
            await self._cmd_emergency(update, context)
        elif query.data == "cancel":
            await query.edit_message_text("❌ Скасовано")
        elif query.data == "emergency_stop_only":
            await self.settings_manager.activate_emergency_stop(close_positions=False)
            await query.edit_message_text("🚨 Аварійна зупинка активована - Торгівля вимкнена")
        elif query.data == "emergency_stop_close":
            await self.settings_manager.activate_emergency_stop(close_positions=True)
            await query.edit_message_text("🚨 Аварійна зупинка активована - Закриваємо всі позиції")

    # ------------------------------------------------------------------
    # Проактивні алерти — не частина панелі, працюють самі по собі
    # ------------------------------------------------------------------

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

    async def _on_symbols_rotated(self, event: Event) -> None:
        added = event.data.get('added') or []
        removed = event.data.get('removed') or []
        total = event.data.get('total_active')

        lines = ["🔄 <b>Ротація монет</b>"]
        if added:
            lines.append("├ Додано: " + ", ".join(f"<code>{s}</code>" for s in added))
        if removed:
            lines.append("├ Прибрано: " + ", ".join(f"<code>{s}</code>" for s in removed))
        if total is not None:
            lines.append(f"└ Активних символів: {total}")

        await self.send_message("\n".join(lines))

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