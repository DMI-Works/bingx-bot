import asyncio
import logging
import hmac
import hashlib
import time
from typing import Dict, Any, Optional, List

from .websocket_client import WebSocketClient
from .rest_client import RestClient
from ..events import EventBus, Event, EventType


logger = logging.getLogger(__name__)


class BingXAPIError(Exception):
    def __init__(self, code: int, msg: str, endpoint: str = "", response: Optional[Dict[str, Any]] = None):
        self.code = code
        self.msg = msg
        self.endpoint = endpoint
        self.response = response or {}
        super().__init__(f"BingX API error {code} at {endpoint}: {msg}")


class BingXClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        event_bus: Optional[EventBus] = None
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.event_bus = event_bus

        # URLs
        if testnet:
            self.rest_base_url = "https://open-api-vst.bingx.com"
            self.ws_url = "wss://vst-open-api-ws.bingx.com/swap-market"
        else:
            self.rest_base_url = "https://open-api.bingx.com"
            self.ws_url = "wss://open-api-swap.bingx.com/swap-market"

        self.rest_client = RestClient(
            base_url=self.rest_base_url,
            api_key=api_key,
            api_secret=api_secret
        )

        self.ws_client: Optional[WebSocketClient] = None
        self.subscribed_symbols: set = set()
        self.depth_subscribed_symbols: set = set()

        logger.info(f"BingXClient initialized (testnet={testnet})")

    @staticmethod
    def _raise_if_error(response: Dict[str, Any], endpoint: str) -> Dict[str, Any]:
        """
        BingX повертає HTTP 200 навіть при бізнес-помилках — код лежить у тілі.
        rest_client лише логує такі помилки, але не кидає виключення
        (це усвідомлено, щоб дозволити retry-логіці на 100001 відпрацювати).
        Тому на рівні BingXClient ми зобов'язані самі перевірити code перед
        тим, як вважати операцію успішною.
        """
        if isinstance(response, dict) and response.get('code') not in (0, None):
            raise BingXAPIError(
                code=response.get('code'),
                msg=response.get('msg', ''),
                endpoint=endpoint,
                response=response
            )
        return response

    async def _handle_ws_message(self, data: Dict[str, Any]) -> None:
        try:
            event_type = data.get("e")

            # Обрабатываем только события, которые нам нужны
            if event_type == "ACCOUNT_UPDATE":
                await self._handle_account_update(data)
                return

            if event_type == "ORDER_TRADE_UPDATE":
                await self._handle_order_update(data)
                return

            # Рыночні дані. Ізольована маршрутизація за суфіксом dataType —
            # існуючі обробники (@trade) не зачіпаються.
            data_type = data.get("dataType")
            if not data_type:
                return
            if data_type.endswith("@trade"):
                await self._handle_price_update(data)
            elif "@depth" in data_type:
                await self._handle_depth_update(data)

        except Exception as e:
            logger.error(f"Error handling WebSocket message: {e}", exc_info=True)

    async def _handle_account_update(self, data: Dict[str, Any]) -> None:
        if self.event_bus:
            await self.event_bus.publish(Event(
                type=EventType.HANDLE_ACCOUNT_INFO_UPDATE,
                data=data,
                source="BingXClient"
            ))
            logger.info("[WS] HANDLE_ACCOUNT_INFO_UPDATE event published")

    async def _handle_order_update(self, data: Dict[str, Any]) -> None:
        if self.event_bus:
            await self.event_bus.publish(Event(
                type=EventType.ORDER_FILLED,
                data=data,
                source="BingXClient"
            ))

    async def _handle_price_update(self, data: Dict[str, Any]) -> None:
        if self.event_bus and 'data' in data:
            price_data = data['data']
            await self.event_bus.publish(Event(
                type=EventType.PRICE_UPDATED,
                data=price_data,
                source="BingXClient"
            ))

    async def _handle_depth_update(self, data: Dict[str, Any]) -> None:
        """dataType='<symbol>@depth20' (снепшот топ-N рівнів, не інкремент —
        BingX пушить повний bids/asks-зріз щосекунди, тому окремої логіки
        синхронізації порядкового номера тут НЕ потрібно)."""
        if not self.event_bus or 'data' not in data:
            return
        depth_data = data['data']
        data_type = data.get('dataType', '')
        # символ не завжди присутній у самому payload depth — витягуємо з dataType
        symbol = data_type.split('@', 1)[0] if '@' in data_type else depth_data.get('s')
        await self.event_bus.publish(Event(
            type=EventType.ORDERBOOK_UPDATED,
            data={
                'symbol': symbol,
                'bids': depth_data.get('bids', []),
                'asks': depth_data.get('asks', []),
            },
            source="BingXClient"
        ))

    async def start_websocket(self) -> None:
        self.ws_client = WebSocketClient(
            url=self.ws_url,
            on_message=self._handle_ws_message,
            ping_interval=20,
            reconnect_interval=5,
            max_reconnect_attempts=10
        )

        await self.ws_client.start()

        if self.event_bus:
            await self.event_bus.publish(Event(
                type=EventType.WEBSOCKET_CONNECTED,
                data={'url': self.ws_url},
                source="BingXClient"
            ))

        logger.info("WebSocket started")

    async def stop_websocket(self) -> None:
        if self.ws_client:
            await self.ws_client.stop()
            logger.info("WebSocket stopped")

    async def subscribe_trades(self, symbol: str) -> None:
        if self.ws_client:
            await self.ws_client.subscribe(f"{symbol}@trade", symbol)
            self.subscribed_symbols.add(symbol)

    async def unsubscribe_trades(self, symbol: str) -> None:
        if self.ws_client:
            await self.ws_client.unsubscribe(f"{symbol}@trade", symbol)
            self.subscribed_symbols.discard(symbol)

    async def subscribe_depth(self, symbol: str, level: int = 20) -> None:
        """Підписка на partial book depth (знепшот топ-N рівнів, оновлюється
        ~раз/сек). level: 5, 20 або 100 (підтримувані BingX варіанти)."""
        if self.ws_client:
            await self.ws_client.subscribe(f"{symbol}@depth{level}", symbol)
            self.depth_subscribed_symbols.add(symbol)

    async def unsubscribe_depth(self, symbol: str, level: int = 20) -> None:
        if self.ws_client:
            await self.ws_client.unsubscribe(f"{symbol}@depth{level}", symbol)
            self.depth_subscribed_symbols.discard(symbol)

    async def start_user_data_stream(self) -> None:
        self.listen_key = await self.get_listen_key()
        private_ws_url = f"{self.ws_url}?listenKey={self.listen_key}"

        self.private_ws_client = WebSocketClient(
            url=private_ws_url,
            on_message=self._handle_ws_message,
            ping_interval=20,
            reconnect_interval=5,
            max_reconnect_attempts=10
        )
        await self.private_ws_client.start()

        self._keepalive_task = asyncio.create_task(self._listen_key_keepalive_loop())

    async def _listen_key_keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(30 * 60)
            try:
                await self.keep_alive_listen_key(self.listen_key)
                logger.info("Listen key extended")
            except Exception as e:
                logger.error(f"Failed to extend listen key: {e}")

    # REST API Methods

    async def get_account_balance(self) -> Dict[str, Any]:
        try:
            response = await self.rest_client.get('/openApi/swap/v2/user/balance', signed=True)
            return self._raise_if_error(response, '/openApi/swap/v2/user/balance')
        except Exception as e:
            logger.error(f"Failed to get account balance: {e}")
            raise

    async def get_positions(self) -> List[Dict[str, Any]]:
        try:
            response = await self.rest_client.get(
                "/openApi/swap/v2/user/positions",
                signed=True
            )
            self._raise_if_error(response, '/openApi/swap/v2/user/positions')

            return response.get("data", [])

        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            raise

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            params = {}
            if symbol:
                params['symbol'] = symbol

            response = await self.rest_client.get('/openApi/swap/v2/trade/openOrders', params, signed=True)
            self._raise_if_error(response, '/openApi/swap/v2/trade/openOrders')
            return response.get('data', {}).get('orders', [])
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            raise

    async def get_order(self, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        try:
            params = {
                'symbol': symbol,
                'orderId': order_id
            }

            response = await self.rest_client.get('/openApi/swap/v2/trade/order', params, signed=True)
            self._raise_if_error(response, '/openApi/swap/v2/trade/order')
            return response.get('data', {}).get('order')
        except Exception as e:
            logger.error(f"Failed to get order {order_id}: {e}")
            return None

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        reduce_only: bool = False,
        position_side: Optional[str] = None,
        client_order_id: Optional[str] = None,
        close_position: bool = False,
    ) -> Dict[str, Any]:
        """
        close_position: якщо True — позиція закривається ПОВНІСТЮ по факту
        спрацювання (лише STOP_MARKET / TAKE_PROFIT_MARKET).

        ВАЖЛИВО (виявлено емпірично, розходиться з текстом офіційної доки):
        реальний BingX API вимагає quantity ЗАВЖДИ для STOP_MARKET/
        TAKE_PROFIT_MARKET, навіть з closePosition=true — інакше повертає
        109400 "parameter quantity or stopPrice is must". Тому quantity
        відправляється в обох випадках; при closePosition=true його значення
        сервер, судячи з усього, ігнорує і все одно закриває позицію повністю.
        """
        if not quantity and not close_position:
            raise ValueError("create_order: quantity is required")
        if close_position and quantity is None:
            raise ValueError(
                "create_order: quantity is required even with close_position=True "
                "(BingX API rejects the request without it despite docs saying otherwise)"
            )

        try:
            params = {
                'symbol': symbol,
                'side': side,
                'type': order_type,
                'quantity': quantity,
            }

            if close_position:
                params['closePosition'] = 'true'

            if position_side:
                params['positionSide'] = position_side
            else:
                params['positionSide'] = 'LONG' if side == 'BUY' else 'SHORT'

            if price:
                params['price'] = price
            if stop_price:
                params['stopPrice'] = stop_price
            if client_order_id:
                params['clientOrderID'] = client_order_id

            response = await self.rest_client.post('/openApi/swap/v2/trade/order', params)
            self._raise_if_error(response, '/openApi/swap/v2/trade/order')
            logger.info(
                f"Order created: {symbol} {side} quantity={quantity}"
                + (f" closePosition=true" if close_position else "")
                + (f" clientOrderID={client_order_id}" if client_order_id else "")
            )
            return response

        except Exception as e:
            logger.error(f"Failed to create order: {e}")
            raise

    async def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        try:
            params = {
                'symbol': symbol,
                'orderId': order_id,
            }
            response = await self.rest_client.delete('/openApi/swap/v2/trade/order', params)
            self._raise_if_error(response, '/openApi/swap/v2/trade/order')
            logger.info(f"Order cancelled: {symbol} orderId={order_id}")
            return response
        except BingXAPIError:
            # прокидаємо як є — виклик, що чекає на BingXAPIError (напр.
            # TrailingStopManager), сам вирішує, чи це "вже виконаний ордер"
            raise
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id} for {symbol}: {e}")
            raise

    async def set_leverage(self, symbol: str, leverage: int, side: str = "BOTH") -> Dict[str, Any]:
        try:
            params = {
                'symbol': symbol,
                'leverage': leverage,
                'side': side
            }

            response = await self.rest_client.post('/openApi/swap/v2/trade/leverage', params)
            self._raise_if_error(response, '/openApi/swap/v2/trade/leverage')
            logger.info(f"Leverage set: {symbol} {leverage}x")
            return response

        except BingXAPIError as e:
            if e.code == 109400 and 'Hedge mode' in e.msg:
                logger.error(
                    f"set_leverage failed: account is in Hedge mode, "
                    f"side='{side}' is invalid there — use 'LONG', 'SHORT' or 'ALL' instead of 'BOTH'"
                )
            else:
                logger.error(f"Failed to set leverage: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to set leverage: {e}")
            raise

    async def get_ticker_price(self, symbol: str) -> float:
        try:
            response = await self.rest_client.get('/openApi/swap/v2/quote/price', {'symbol': symbol})
            self._raise_if_error(response, '/openApi/swap/v2/quote/price')
            return float(response.get('data', {}).get('price', 0))

        except Exception as e:
            logger.error(f"Failed to get ticker price: {e}")
            raise

    async def get_mark_price(self, symbol: str) -> float:
        """
        Mark price (індексна/funding-скоригована ціна) — саме та ціна, за
        якою BingX тригерить STOP_MARKET/TAKE_PROFIT_MARKET ордери за
        замовчуванням (workingType=MARK_PRICE, підтверджено з WS
        ORDER_TRADE_UPDATE echo). Використовуй ЦЮ функцію, а не
        get_ticker_price(), коли рахуєш/переанкориш stopPrice відносно
        "поточної ціни" — інакше на низьколіквідних парах (мало угод —
        last-trade price "залипає") можна знову і знову отримувати 110412
        "Stop Loss price should be greater/less than the current price",
        навіть коли переанкоринг вже спрацював, бо звірявся не з тією ціною.
        """
        try:
            response = await self.rest_client.get('/openApi/swap/v2/quote/premiumIndex', {'symbol': symbol})
            self._raise_if_error(response, '/openApi/swap/v2/quote/premiumIndex')
            data = response.get('data', {})
            if isinstance(data, list):
                data = data[0] if data else {}
            return float(data.get('markPrice', 0))

        except Exception as e:
            logger.error(f"Failed to get mark price: {e}")
            raise

    async def close(self) -> None:
        await self.stop_websocket()
        await self.rest_client.close()
        logger.info("BingXClient closed")

    async def get_listen_key(self) -> str:
        response = await self.rest_client.post('/openApi/user/auth/userDataStream', {})
        self._raise_if_error(response, '/openApi/user/auth/userDataStream')
        return response.get('listenKey')

    async def keep_alive_listen_key(self, listen_key: str) -> None:
        response = await self.rest_client.put('/openApi/user/auth/userDataStream', {'listenKey': listen_key})
        self._raise_if_error(response, '/openApi/user/auth/userDataStream')

    async def get_all_tickers(self) -> List[Dict[str, Any]]:
        """24h статистика по всім swap-контрактам (об'єм, ціна, спред)."""
        try:
            response = await self.rest_client.get('/openApi/swap/v2/quote/ticker')
            self._raise_if_error(response, '/openApi/swap/v2/quote/ticker')
            return response.get('data', [])
        except Exception as e:
            logger.error(f"Failed to get all tickers: {e}")
            raise


    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Історичні свічки (K-line). Endpoint: /openApi/swap/v3/quote/klines
        interval: '1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d' і т.д.
        Повертає список свічок {open, high, low, close, volume, time(ms)}
        у ХРОНОЛОГІЧНОМУ порядку (старі -> нові).
        """
        try:
            params = {
                'symbol': symbol,
                'interval': interval,
                'limit': min(limit, 1000),
            }
            if start_time is not None:
                params['startTime'] = start_time
            if end_time is not None:
                params['endTime'] = end_time

            response = await self.rest_client.get('/openApi/swap/v3/quote/klines', params)
            self._raise_if_error(response, '/openApi/swap/v3/quote/klines')

            data = response.get('data', [])

            # BingX іноді повертає свічки від нових до старих — нормалізуємо
            if len(data) >= 2 and data[0].get('time', 0) > data[-1].get('time', 0):
                data = list(reversed(data))

            logger.info(f"[{symbol}] get_klines: отримано {len(data)} свічок, interval={interval}")

            if data:
                first, last = data[0], data[-1]
                logger.info(
                    f"[{symbol}] get_klines: перша={first.get('time')} "
                    f"(o={first.get('open')} c={first.get('close')}) | "
                    f"остання={last.get('time')} (o={last.get('open')} c={last.get('close')})"
                )
                invalid = [
                    k for k in data
                    if float(k.get('high', 0)) < float(k.get('low', 0))
                    or not (float(k.get('low', 0)) <= float(k.get('open', 0)) <= float(k.get('high', 0)))
                    or not (float(k.get('low', 0)) <= float(k.get('close', 0)) <= float(k.get('high', 0)))
                ]
                if invalid:
                    logger.warning(
                        f"[{symbol}] get_klines: {len(invalid)} підозрілих свічок "
                        f"(high/low/open/close не узгоджені) — перевір дані вручну"
                    )
            else:
                logger.warning(f"[{symbol}] get_klines: біржа повернула ПОРОЖНІЙ список свічок")

            return data

        except Exception as e:
            logger.error(f"Failed to get klines for {symbol}: {e}")
            raise