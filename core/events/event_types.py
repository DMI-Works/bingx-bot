from enum import Enum


class EventType(Enum):
    # Price Events
    PRICE_UPDATED = "price_updated"

    # Signal Events
    SIGNAL_GENERATED = "signal_generated"

    # Order Events
    ORDER_CREATED = "order_created"
    ORDER_SENT = "order_sent"
    ORDER_ACCEPTED = "order_accepted"
    ORDER_PARTIALLY_FILLED = "order_partially_filled"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REJECTED = "order_rejected"
    ORDER_EXPIRED = "order_expired"
    ORDER_FAILED = "order_failed"

    # Position Events
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    POSITION_UPDATED = "position_updated"

    # Stop Loss Events
    STOP_LOSS_CREATED = "stop_loss_created"
    STOP_LOSS_TRIGGERED = "stop_loss_triggered"
    STOP_LOSS_FAILED = "stop_loss_failed"

    # Take Profit Events
    TAKE_PROFIT_CREATED = "take_profit_created"
    TAKE_PROFIT_TRIGGERED = "take_profit_triggered"
    TAKE_PROFIT_FAILED = "take_profit_failed"

    # Balance Events
    BALANCE_UPDATED = "balance_updated"

    # WebSocket Events
    WEBSOCKET_CONNECTED = "websocket_connected"
    WEBSOCKET_DISCONNECTED = "websocket_disconnected"
    WEBSOCKET_RECONNECTING = "websocket_reconnecting"
    WEBSOCKET_ERROR = "websocket_error"

    # Recovery Events
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"
    RECOVERY_FAILED = "recovery_failed"

    # Risk Events
    RISK_LIMIT_EXCEEDED = "risk_limit_exceeded"
    COOLDOWN_ACTIVE = "cooldown_active"

    # Settings Events
    SETTINGS_CHANGED = "settings_changed"

    # Emergency Events
    EMERGENCY_STOP_ACTIVATED = "emergency_stop_activated"
    EMERGENCY_STOP_DEACTIVATED = "emergency_stop_deactivated"

    # Error Events
    ERROR = "error"
    CRITICAL_ERROR = "critical_error"
