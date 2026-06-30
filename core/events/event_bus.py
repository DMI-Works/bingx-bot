import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from .event_types import EventType


logger = logging.getLogger(__name__)


@dataclass
class Event:
    type: EventType
    data: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)
    source: Optional[str] = None

    def __str__(self) -> str:
        return f"Event({self.type.value}, source={self.source}, time={self.timestamp})"


class EventBus:
    def __init__(self):
        self._subscribers: Dict[EventType, List[Callable]] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        logger.info("EventBus initialized")

    def subscribe(self, event_type: EventType, callback: Callable) -> None:
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []

        self._subscribers[event_type].append(callback)
        logger.info(f"Subscribed {callback.__name__} to {event_type.value}")

    def unsubscribe(self, event_type: EventType, callback: Callable) -> None:
        if event_type in self._subscribers:
            self._subscribers[event_type].remove(callback)
            logger.info(f"Unsubscribed {callback.__name__} from {event_type.value}")

    async def publish(self, event: Event) -> None:
        await self._queue.put(event)
        logger.debug(f"Published: {event}")

    async def _process_events(self) -> None:
        logger.info("Event processing loop started")

        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._dispatch_event(event)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Error processing event: {e}", exc_info=True)

    async def _dispatch_event(self, event: Event) -> None:
        if event.type not in self._subscribers:
            logger.debug(f"No subscribers for {event.type.value}")
            return

        callbacks = self._subscribers[event.type]
        logger.debug(f"Dispatching {event.type.value} to {len(callbacks)} subscribers")

        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)
            except Exception as e:
                logger.error(f"Error in callback {callback.__name__} for {event.type.value}: {e}", exc_info=True)

    async def start(self) -> None:
        if self._running:
            logger.warning("EventBus already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._process_events())
        logger.info("EventBus started")

    async def stop(self) -> None:
        if not self._running:
            return

        logger.info("Stopping EventBus")
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("EventBus stopped")

    def get_subscriber_count(self, event_type: EventType) -> int:
        return len(self._subscribers.get(event_type, []))

    def get_queue_size(self) -> int:
        return self._queue.qsize()
