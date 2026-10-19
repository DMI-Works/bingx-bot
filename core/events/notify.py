import logging
from typing import Optional

from .event_bus import Event, EventBus
from .event_types import EventType

logger = logging.getLogger(__name__)


async def publish_error(
    event_bus: EventBus,
    context: str,
    error: str = '',
    critical: bool = False,
    source: Optional[str] = None,
) -> None:
    """Публікує ERROR/CRITICAL_ERROR — TelegramBot підписаний на обидва й шле
    коротке повідомлення в чат. Збій публікації лише логується: сповіщення
    не повинно ламати торгову логіку, з якої його надсилають."""
    try:
        await event_bus.publish(Event(
            type=EventType.CRITICAL_ERROR if critical else EventType.ERROR,
            data={'error': error, 'context': context},
            source=source,
        ))
    except Exception as e:
        logger.error(f"Failed to publish error notification ({context}): {e}")
