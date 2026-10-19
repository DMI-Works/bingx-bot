import asyncio
import logging
from typing import Dict, Any, Optional, List

from .errors import BingXAPIError
from .websocket_client import WebSocketClient
from .rest_client import RestClient
from ..events import EventBus, Event, EventType


logger = logging.getLogger(__name__)

__all__ = ['BingXClient', 'BingXAPIError']


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
        self.private_ws_client: Optional[WebSocketClient] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self.listen_key: Optional[str] = None
        self.subscribed_symbols: set = set()
        self.depth_subscribed_symbols: set = set()

        logger.info(f"BingXClient initialized (testnet={testnet})")

    # ------------------------------------------------------------------
    # REST: транспорт
    # ------------------------------------------------------------------

    @staticmethod
    def _raise_if_error(response: Dict[str, Any], endpoint: str) -> Dict[str, Any]:
        """
        BingX повертає HTTP 200 навіть при бізнес-помилках — код лежить у тілі.
        RestClient лише логує такі помилки (щоб відпрацював його retry на
        100001), тому успішність операції перевіряємо тут.
        """
        if isinstance(response, dict) and response.get('code') not in (0, None):
            raise BingXAPIError(
                code=response.get('code'),
                msg=response.get('msg', ''),
                endpoint=endpoint,
                response=response
            )
        return response

    async def _request(
        self, method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, **kwargs
    ) -> Dict[str, Any]:
        """Запит + перевірка бізнес-коду. Помилки (мережеві й BingXAPIError)
        прокидаються викликачу; RestClient уже залогував їх."""
        send = {
            'GET': self.rest_client.get,
            'POST': self.rest_client.post,
            'PUT': self.rest_client.put,
            'DELETE': self.rest_client.delete,
        }[method]
        response = await send(endpoint, params, **kwargs)
        return self._raise_if_error(response, endpoint)

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------

    async def _publish(self, event_type: EventType, data: Any) -> None:
        if self.event_bus:
            await self.event_bus.publish(Event(type=event_type, data=data, source="BingXClient"))

    async def _handle_ws_message(self, data: Dict[str, Any]) -> None:
        try:
            event_type = data.get("e")

            if event_type == "ACCOUNT_UPDATE":
                await self._publish(EventType.HANDLE_ACCOUNT_INFO_UPDATE, data)
                logger.info("[WS] HANDLE_ACCOUNT_INFO_UPDATE event published")
                return

            if event_type == "ORDER_TRADE_UPDATE":
                await self._publish(EventType.ORDER_FILLED, data)
                return

            # Ринкові дані — маршрутизація за суфіксом dataType
            data_type = data.get("dataType")
            if not data_type or 'data' not in data:
                return
            if data_type.endswith("@trade"):
                await self._publish(EventType.PRICE_UPDATED, data['data'])
            elif "@depth" in data_type:
                await self._handle_depth_update(data_type, data['data'])

        except Exception as e:
            logger.error(f"Error handling WebSocket message: {e}", exc_info=True)

    async def _handle_depth_update(self, data_type: str, depth_data: Dict[str, Any]) -> None:
        """dataType='<symbol>@depth20' — снепшот топ-N рівнів (не інкремент:
        BingX пушить повний bids/asks-зріз щосекунди, синхронізація
        порядкового номера не потрібна)."""
        # символ не завжди присутній у самому payload depth — витягуємо з dataType
        symbol = data_type.split('@', 1)[0] if '@' in data_type else depth_data.get('s')
        await self._publish(EventType.ORDERBOOK_UPDATED, {
            'symbol': symbol,
            'bids': depth_data.get('bids', []),
            'asks': depth_data.get('asks', []),
        })

    def _new_ws_client(self, url: str, on_reconnect=None) -> WebSocketClient:
        return WebSocketClient(
            url=url,
            on_message=self._handle_ws_message,
            ping_interval=20,
            reconnect_interval=5,
            max_reconnect_attempts=10,
            on_reconnect=on_reconnect,
        )

    async def start_websocket(self) -> None:
        self.ws_client = self._new_ws_client(self.ws_url, on_reconnect=self._resubscribe_all)
        await self.ws_client.start()
        await self._publish(EventType.WEBSOCKET_CONNECTED, {'url': self.ws_url})
        logger.info("WebSocket started")

    async def _resubscribe_all(self) -> None:
        """Викликається WebSocketClient ПІСЛЯ кожного reconnect (новий сокет,
        біржа не пам'ятає попередніх підписок). subscribed_symbols /
        depth_subscribed_symbols — те, що бот ВВАЖАЄ підписаним; sub шлемо
        напряму через ws_client, НЕ через subscribe_trades/subscribe_depth,
        бо ті мутують ці самі set'и."""
        if not self.ws_client:
            return

        channels = (
            [(f"{s}@trade", s) for s in sorted(self.subscribed_symbols)]
            + [(f"{s}@depth20", s) for s in sorted(self.depth_subscribed_symbols)]
        )
        if not channels:
            return

        logger.warning(
            f"[WS] Reconnected — resubscribing {len(self.subscribed_symbols)} @trade "
            f"and {len(self.depth_subscribed_symbols)} @depth channels (exchange forgot them "
            f"on the new connection)"
        )

        failed = set()
        for channel, symbol in channels:
            try:
                await self.ws_client.subscribe(channel, symbol)
            except Exception as e:
                failed.add(symbol)
                logger.error(f"[WS] Failed to resubscribe {channel}: {e}")

        if failed:
            logger.error(f"[WS] Resubscribe finished with failures for: {sorted(failed)}")
        else:
            logger.info("[WS] Resubscribe after reconnect completed successfully")

    async def stop_websocket(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        for client in (self.ws_client, self.private_ws_client):
            if client:
                await client.stop()
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
        """Підписка на partial book depth (снепшот топ-N рівнів, оновлюється
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
        self.private_ws_client = self._new_ws_client(f"{self.ws_url}?listenKey={self.listen_key}")
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

    async def get_listen_key(self) -> str:
        response = await self._request('POST', '/openApi/user/auth/userDataStream', {})
        return response.get('listenKey')

    async def keep_alive_listen_key(self, listen_key: str) -> None:
        await self._request('PUT', '/openApi/user/auth/userDataStream', {'listenKey': listen_key})

    # ------------------------------------------------------------------
    # REST: акаунт
    # ------------------------------------------------------------------

    async def get_account_balance(self) -> Dict[str, Any]:
        return await self._request('GET', '/openApi/swap/v2/user/balance', signed=True)

    async def get_positions(self) -> List[Dict[str, Any]]:
        response = await self._request('GET', '/openApi/swap/v2/user/positions', signed=True)
        return response.get("data", [])

    async def get_income_history(
        self,
        symbol: Optional[str] = None,
        income_type: Optional[str] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        Історія нарахувань/списань по рахунку: GET /openApi/swap/v2/user/income.
        income_type: 'REALIZED_PNL' | 'FUNDING_FEE' | 'COMMISSION' | ...

        На відміну від ORDER_TRADE_UPDATE (WS), сюди потрапляє і FUNDING_FEE —
        списання/нарахування за УТРИМАННЯ позиції, не прив'язане до ордера,
        тому net_pnl закритих угод його не містить. Див. /api/stats.
        """
        params: Dict[str, Any] = {'limit': limit}
        if symbol:
            params['symbol'] = symbol
        if income_type:
            params['incomeType'] = income_type
        if start_time is not None:
            params['startTime'] = start_time
        if end_time is not None:
            params['endTime'] = end_time

        response = await self._request('GET', '/openApi/swap/v2/user/income', params, signed=True)
        return response.get('data', [])

    # ------------------------------------------------------------------
    # REST: ордери
    # ------------------------------------------------------------------

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {'symbol': symbol} if symbol else {}
        response = await self._request('GET', '/openApi/swap/v2/trade/openOrders', params, signed=True)
        return response.get('data', {}).get('orders', [])

    async def get_order(self, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        """None при будь-якій помилці — викликач (опитування виконання) просто
        спробує ще раз."""
        try:
            response = await self._request(
                'GET', '/openApi/swap/v2/trade/order', {'symbol': symbol, 'orderId': order_id}, signed=True
            )
        except Exception as e:
            logger.error(f"Failed to get order {order_id}: {e}")
            return None
        return response.get('data', {}).get('order')

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
        working_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        working_type: 'MARK_PRICE' | 'CONTRACT_PRICE' | 'INDEX_PRICE' — за якою
        ціною тригериться STOP_MARKET/TAKE_PROFIT_MARKET. Не задано — дефолт
        біржі (MARK_PRICE). Явний 'MARK_PRICE' потрібен там, де стоп не повинно
        вибивати тіньовим сквізом last-price (TrailingStopManager, atr_3step).

        close_position: True — позиція закривається ПОВНІСТЮ по факту
        спрацювання (лише STOP_MARKET / TAKE_PROFIT_MARKET).

        ВАЖЛИВО (виявлено емпірично, розходиться з офіційною докою): BingX
        вимагає quantity ЗАВЖДИ для STOP_MARKET/TAKE_PROFIT_MARKET, навіть з
        closePosition=true — інакше 109400 "parameter quantity or stopPrice is
        must". При closePosition=true значення quantity сервер ігнорує.
        """
        if not quantity and not close_position:
            raise ValueError("create_order: quantity is required")
        if close_position and quantity is None:
            raise ValueError(
                "create_order: quantity is required even with close_position=True "
                "(BingX API rejects the request without it despite docs saying otherwise)"
            )

        params = {
            'symbol': symbol,
            'side': side,
            'type': order_type,
            'quantity': quantity,
            'positionSide': position_side or ('LONG' if side == 'BUY' else 'SHORT'),
        }
        if close_position:
            params['closePosition'] = 'true'
        if price:
            params['price'] = price
        if stop_price:
            params['stopPrice'] = stop_price
        if client_order_id:
            params['clientOrderID'] = client_order_id
        if working_type:
            params['workingType'] = working_type

        response = await self._request('POST', '/openApi/swap/v2/trade/order', params)
        logger.info(
            f"Order created: {symbol} {side} quantity={quantity}"
            + (" closePosition=true" if close_position else "")
            + (f" clientOrderID={client_order_id}" if client_order_id else "")
        )
        return response

    async def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """BingXAPIError прокидається як є — викликач (напр. TrailingStopManager)
        сам вирішує, чи це "вже виконаний ордер"."""
        response = await self._request(
            'DELETE', '/openApi/swap/v2/trade/order', {'symbol': symbol, 'orderId': order_id}
        )
        logger.info(f"Order cancelled: {symbol} orderId={order_id}")
        return response

    async def set_leverage(self, symbol: str, leverage: int, side: str = "BOTH") -> Dict[str, Any]:
        try:
            response = await self._request(
                'POST', '/openApi/swap/v2/trade/leverage', {'symbol': symbol, 'leverage': leverage, 'side': side}
            )
        except BingXAPIError as e:
            if e.code == 109400 and 'Hedge mode' in e.msg:
                logger.error(
                    f"set_leverage failed: account is in Hedge mode, "
                    f"side='{side}' is invalid there — use 'LONG', 'SHORT' or 'ALL' instead of 'BOTH'"
                )
            raise
        logger.info(f"Leverage set: {symbol} {leverage}x")
        return response

    # ------------------------------------------------------------------
    # REST: ринкові дані
    # ------------------------------------------------------------------

    async def get_ticker_price(self, symbol: str) -> float:
        response = await self._request('GET', '/openApi/swap/v2/quote/price', {'symbol': symbol})
        return float(response.get('data', {}).get('price', 0))

    async def get_mark_price(self, symbol: str) -> float:
        """
        Mark price — саме за нею BingX тригерить STOP_MARKET/TAKE_PROFIT_MARKET
        за замовчуванням (workingType=MARK_PRICE). Використовуй ЦЮ функцію, а не
        get_ticker_price(), коли рахуєш/переанкориш stopPrice відносно "поточної
        ціни" — інакше на низьколіквідних парах (last-trade price "залипає")
        можна раз за разом отримувати 110412 "Stop Loss price should be
        greater/less than the current price".
        """
        response = await self._request('GET', '/openApi/swap/v2/quote/premiumIndex', {'symbol': symbol})
        data = response.get('data', {})
        if isinstance(data, list):
            data = data[0] if data else {}
        return float(data.get('markPrice', 0))

    async def get_all_tickers(self) -> List[Dict[str, Any]]:
        """24h статистика по всім swap-контрактам (об'єм, ціна, спред)."""
        response = await self._request('GET', '/openApi/swap/v2/quote/ticker')
        return response.get('data', [])

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Історичні свічки: /openApi/swap/v3/quote/klines.
        interval: '1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d' і т.д.
        Повертає [{open, high, low, close, volume, time(ms)}] у ХРОНОЛОГІЧНОМУ
        порядку (старі -> нові).
        """
        params = {'symbol': symbol, 'interval': interval, 'limit': min(limit, 1000)}
        if start_time is not None:
            params['startTime'] = start_time
        if end_time is not None:
            params['endTime'] = end_time

        response = await self._request('GET', '/openApi/swap/v3/quote/klines', params)
        data = response.get('data', [])

        # BingX іноді повертає свічки від нових до старих — нормалізуємо
        if len(data) >= 2 and data[0].get('time', 0) > data[-1].get('time', 0):
            data = list(reversed(data))

        self._log_klines(symbol, interval, data)
        return data

    @staticmethod
    def _log_klines(symbol: str, interval: str, data: List[Dict[str, Any]]) -> None:
        logger.info(f"[{symbol}] get_klines: отримано {len(data)} свічок, interval={interval}")
        if not data:
            logger.warning(f"[{symbol}] get_klines: біржа повернула ПОРОЖНІЙ список свічок")
            return

        first, last = data[0], data[-1]
        logger.info(
            f"[{symbol}] get_klines: перша={first.get('time')} "
            f"(o={first.get('open')} c={first.get('close')}) | "
            f"остання={last.get('time')} (o={last.get('open')} c={last.get('close')})"
        )

        def is_invalid(k) -> bool:
            low, high = float(k.get('low', 0)), float(k.get('high', 0))
            return (
                high < low
                or not (low <= float(k.get('open', 0)) <= high)
                or not (low <= float(k.get('close', 0)) <= high)
            )

        invalid = sum(1 for k in data if is_invalid(k))
        if invalid:
            logger.warning(
                f"[{symbol}] get_klines: {invalid} підозрілих свічок "
                f"(high/low/open/close не узгоджені) — перевір дані вручну"
            )

    async def close(self) -> None:
        await self.stop_websocket()
        await self.rest_client.close()
        logger.info("BingXClient closed")
