import asyncio
import websockets
import json
import gzip
import logging
from typing import Callable, Optional
from datetime import datetime


logger = logging.getLogger(__name__)


class WebSocketClient:
    def __init__(
        self,
        url: str,
        on_message: Callable,
        ping_interval: int = 20,
        reconnect_interval: int = 5,
        max_reconnect_attempts: int = 10
    ):
        self.url = url
        self.on_message = on_message
        self.ping_interval = ping_interval
        self.reconnect_interval = reconnect_interval
        self.max_reconnect_attempts = max_reconnect_attempts

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.is_connected = False
        self.is_running = False
        self.reconnect_count = 0
        self._tasks = []

        self._reconnect_lock = asyncio.Lock()
        self._reconnecting = False

    async def connect(self) -> None:
        logger.info(f"Connecting to WebSocket: {self.url}")

        try:
            self.ws = await websockets.connect(self.url)
            self.is_connected = True
            self.reconnect_count = 0
            logger.info("WebSocket connected")

        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            raise

    async def disconnect(self) -> None:
        logger.info("Disconnecting WebSocket")
        self.is_running = False
        self.is_connected = False

        if self.ws:
            await self.ws.close()
            self.ws = None

        await self._cancel_tasks(exclude_current=True)
        logger.info("WebSocket disconnected")

    async def send(self, data: dict) -> None:
        if not self.is_connected or not self.ws:
            raise ConnectionError("WebSocket not connected")

        message = json.dumps(data)
        await self.ws.send(message)
        logger.debug(f"Sent: {message}")

    async def subscribe(self, channel: str, symbol: Optional[str] = None) -> None:
        subscribe_message = {
            "id": f"subscribe_{channel}_{int(datetime.utcnow().timestamp())}",
            "reqType": "sub",
            "dataType": channel
        }

        if symbol:
            subscribe_message["symbol"] = symbol

        await self.send(subscribe_message)
        logger.info(f"Subscribed to {channel}" + (f" for {symbol}" if symbol else ""))

    async def unsubscribe(self, channel: str, symbol: Optional[str] = None) -> None:
        unsubscribe_message = {
            "id": f"unsubscribe_{channel}_{int(datetime.utcnow().timestamp())}",
            "reqType": "unsub",
            "dataType": channel
        }

        if symbol:
            unsubscribe_message["symbol"] = symbol

        await self.send(unsubscribe_message)
        logger.info(f"Unsubscribed from {channel}" + (f" for {symbol}" if symbol else ""))

    async def _receive_loop(self) -> None:
        while self.is_running and self.is_connected:
            try:
                ws = self.ws
                if ws is None:
                    break

                message = await asyncio.wait_for(ws.recv(), timeout=self.ping_interval * 2)

                # Handle binary gzip-compressed data
                if isinstance(message, bytes):
                    try:
                        message = gzip.decompress(message).decode('utf-8')
                    except Exception:
                        # Not gzipped, try direct decode
                        message = message.decode('utf-8')

                if isinstance(message, str) and message.lower() == "ping":
                    await ws.send("Pong")
                    continue

                data = json.loads(message)
                logger.debug(f"Received: {data}")

                await self.on_message(data)

            except asyncio.TimeoutError:
                logger.warning("WebSocket receive timeout")
                await self._reconnect()
                return

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket connection closed")
                await self._reconnect()
                return

            except asyncio.CancelledError:
                raise

            except Exception as e:
                logger.error(f"Error receiving message: {e}", exc_info=True)

    async def _ping_loop(self) -> None:
        while self.is_running and self.is_connected:
            try:
                await asyncio.sleep(self.ping_interval)
                ws = self.ws
                if self.is_connected and ws:
                    await ws.ping()
                    logger.debug("Ping sent")

            except asyncio.CancelledError:
                raise

            except Exception as e:
                logger.error(f"Error sending ping: {e}")
                await self._reconnect()
                return

    async def _cancel_tasks(self, exclude_current: bool = False) -> None:
        """Cancel every tracked task (optionally skipping the currently
        running one, so a loop that calls this on itself doesn't deadlock)."""
        current = asyncio.current_task() if exclude_current else None
        tasks_to_cancel = [t for t in self._tasks if t is not current and not t.done()]

        for task in tasks_to_cancel:
            task.cancel()

        for task in tasks_to_cancel:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"Task raised during cancellation: {e}")

        self._tasks = [t for t in self._tasks if t is current and not t.done()]

    async def _reconnect(self) -> None:
        if not self.is_running:
            return

        if self._reconnecting:
            async with self._reconnect_lock:
                pass
            return

        async with self._reconnect_lock:
            self._reconnecting = True
            try:
                self.is_connected = False
                self.reconnect_count += 1

                if self.reconnect_count > self.max_reconnect_attempts:
                    logger.error(
                        f"Max reconnect attempts ({self.max_reconnect_attempts}) reached"
                    )
                    self.is_running = False
                    return

                logger.info(
                    f"Reconnecting... Attempt {self.reconnect_count}/{self.max_reconnect_attempts}"
                )

                await asyncio.sleep(self.reconnect_interval)

                
                await self._cancel_tasks(exclude_current=True)

                if self.ws:
                    try:
                        await self.ws.close()
                    except Exception:
                        pass
                    self.ws = None

                try:
                    await self.connect()
                except Exception as e:
                    logger.error(f"Reconnection failed: {e}")
                    self._reconnecting = False
                    await self._reconnect()
                    return

                if not self.is_running:
                    return

                self._tasks = [
                    asyncio.create_task(self._receive_loop()),
                    asyncio.create_task(self._ping_loop())
                ]

            finally:
                self._reconnecting = False

    async def start(self) -> None:
        if self.is_running:
            logger.warning("WebSocket already running")
            return

        self.is_running = True
        await self.connect()

        self._tasks = [
            asyncio.create_task(self._receive_loop()),
            asyncio.create_task(self._ping_loop())
        ]

        logger.info("WebSocket client started")

    async def stop(self) -> None:
        await self.disconnect()