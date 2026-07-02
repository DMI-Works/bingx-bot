import asyncio
import logging
from typing import Dict, Any, Optional, List

from .websocket_client import WebSocketClient
from .rest_client import RestClient
from ..events import EventBus, Event, EventType


logger = logging.getLogger(__name__)


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

        logger.info(f"BingXClient initialized (testnet={testnet})")

    async def _handle_ws_message(self, data: Dict[str, Any]) -> None:
        try:
            if 'dataType' in data:
                data_type = data['dataType']

                if data_type == 'ACCOUNT_UPDATE':
                    await self._handle_account_update(data)
                elif data_type == 'ORDER_TRADE_UPDATE':
                    await self._handle_order_update(data)
                elif data_type.endswith('@trade'):
                    await self._handle_price_update(data)

        except Exception as e:
            logger.error(f"Error handling WebSocket message: {e}", exc_info=True)

    async def _handle_account_update(self, data: Dict[str, Any]) -> None:
        if self.event_bus:
            await self.event_bus.publish(Event(
                type=EventType.BALANCE_UPDATED,
                data=data,
                source="BingXClient"
            ))

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

    async def subscribe_account(self) -> None:
        if self.ws_client:
            await self.ws_client.subscribe("ACCOUNT_UPDATE")

    async def subscribe_orders(self) -> None:
        if self.ws_client:
            await self.ws_client.subscribe("ORDER_TRADE_UPDATE")

    # REST API Methods

    async def get_account_balance(self) -> Dict[str, Any]:
        try:
            response = await self.rest_client.get('/openApi/swap/v2/user/balance', signed=True)
            return response
        except Exception as e:
            logger.error(f"Failed to get account balance: {e}")
            raise

    async def get_positions(self) -> List[Dict[str, Any]]:
        try:
            response = await self.rest_client.get('/openApi/swap/v2/user/positions', signed=True)
            return response.get('data', {}).get('positions', [])
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            raise

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            params = {}
            if symbol:
                params['symbol'] = symbol

            response = await self.rest_client.get('/openApi/swap/v2/trade/openOrders', params, signed=True)
            return response.get('data', {}).get('orders', [])
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            raise

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        reduce_only: bool = False
    ) -> Dict[str, Any]:

        try:
            params = {
                'symbol': symbol,
                'side': side,
                'type': order_type,
                'quantity': quantity,
                'reduceOnly': reduce_only
            }

            if price:
                params['price'] = price
            if stop_price:
                params['stopPrice'] = stop_price

            response = await self.rest_client.post('/openApi/swap/v2/trade/order', params)
            logger.info(f"Order created: {symbol} {side} {quantity}")
            return response

        except Exception as e:
            logger.error(f"Failed to create order: {e}")
            raise

    async def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        try:
            params = {
                'symbol': symbol,
                'orderId': order_id
            }

            response = await self.rest_client.delete('/openApi/swap/v2/trade/order', params)
            logger.info(f"Order cancelled: {order_id}")
            return response

        except Exception as e:
            logger.error(f"Failed to cancel order: {e}")
            raise

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        try:
            params = {}
            if symbol:
                params['symbol'] = symbol

            response = await self.rest_client.delete('/openApi/swap/v2/trade/allOpenOrders', params)
            logger.info(f"All orders cancelled" + (f" for {symbol}" if symbol else ""))
            return response

        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            raise

    async def set_leverage(self, symbol: str, leverage: int, side: str = "BOTH") -> Dict[str, Any]:
        try:
            params = {
                'symbol': symbol,
                'leverage': leverage,
                'side': side
            }

            response = await self.rest_client.post('/openApi/swap/v2/trade/leverage', params)
            logger.info(f"Leverage set: {symbol} {leverage}x")
            return response

        except Exception as e:
            logger.error(f"Failed to set leverage: {e}")
            raise

    async def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        try:
            response = await self.rest_client.get('/openApi/swap/v2/quote/contracts')
            contracts = response.get('data', [])

            for contract in contracts:
                if contract.get('symbol') == symbol:
                    return contract

            raise ValueError(f"Symbol {symbol} not found")

        except Exception as e:
            logger.error(f"Failed to get symbol info: {e}")
            raise

    async def get_ticker_price(self, symbol: str) -> float:
        try:
            response = await self.rest_client.get('/openApi/swap/v2/quote/price', {'symbol': symbol})
            return float(response.get('data', {}).get('price', 0))

        except Exception as e:
            logger.error(f"Failed to get ticker price: {e}")
            raise

    async def close(self) -> None:
        await self.stop_websocket()
        await self.rest_client.close()
        logger.info("BingXClient closed")
