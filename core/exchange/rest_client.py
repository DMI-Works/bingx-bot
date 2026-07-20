import aiohttp
import asyncio
import hmac
import hashlib
import time
import logging
from typing import Dict, Any, Optional


logger = logging.getLogger(__name__)


class RestClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        timeout: int = 30,
        max_retries: int = 3,
        retry_delay: int = 1
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.session: Optional[aiohttp.ClientSession] = None

        # server_time = local_time + self._time_offset_ms
        self._time_offset_ms: int = 0
        self._time_offset_synced_at: float = 0.0
        self._time_offset_ttl_seconds: int = 300  # пересинхронизация раз в 5 минут
        self._time_synced_once: bool = False  # флаг: была ли хоть одна успешная синхронизация
        self._sync_lock = asyncio.Lock()  # чтобы несколько параллельных запросов не синкались одновременно

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self.session

    def _generate_signature(self, params_str: str) -> str:
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            params_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _parse_param(self, params: Dict[str, Any]) -> str:
        """Generate parseParam string according to BingX requirements"""
        sorted_keys = sorted(params.keys())
        params_list = [f"{key}={params[key]}" for key in sorted_keys]
        return "&".join(params_list)

    async def _sync_server_time(self, force: bool = False) -> None:
        """
        Синхронизирует смещение локальных часов с сервером BingX.
        Использует публичный (неподписанный) endpoint серверного времени.

        force=True используется, когда нужна гарантированная синхронизация
        (например, при первом подписанном запросе или после signature mismatch) —
        в этом случае исключение НЕ проглатывается, а пробрасывается наружу,
        чтобы вызывающий код не подписал запрос с заведомо неверным офсетом.
        """
        async with self._sync_lock:
            try:
                session = await self._get_session()
                request_sent_at = time.time() * 1000
                async with session.get(f"{self.base_url}/openApi/swap/v2/server/time") as response:
                    result = await response.json()
                    request_received_at = time.time() * 1000

                server_time = result.get('data', {}).get('serverTime') or result.get('serverTime')
                if server_time is None:
                    logger.warning(f"Unexpected server time response format: {result}")
                    if force:
                        raise RuntimeError(f"Cannot parse server time from response: {result}")
                    return

                # приблизительно компенсируем сетевую задержку round-trip
                local_time_at_response = (request_sent_at + request_received_at) / 2
                new_offset = int(server_time - local_time_at_response)

                # если офсет подозрительно большой — это признак кривых системных
                # часов на этой машине (а не сетевой задержки), логируем явно
                if abs(new_offset) > 30_000:
                    logger.error(
                        f"Suspiciously large time offset computed: {new_offset}ms. "
                        f"This usually means the system clock on this machine is wrong. "
                        f"Check your OS time sync (NTP)."
                    )

                self._time_offset_ms = new_offset
                self._time_offset_synced_at = time.time()
                self._time_synced_once = True

                logger.info(f"Time offset synced with BingX server: {self._time_offset_ms}ms")

            except Exception as e:
                logger.warning(f"Failed to sync server time, falling back to local clock: {e}")
                if force:
                    raise

    async def _get_timestamp_ms(self) -> int:
        now = time.time()
        if not self._time_synced_once:
            await self._sync_server_time(force=True)
        elif now - self._time_offset_synced_at > self._time_offset_ttl_seconds:
            await self._sync_server_time()
        return int(time.time() * 1000) + self._time_offset_ms

    async def _request(
            self,
            method: str,
            endpoint: str,
            params: Optional[Dict[str, Any]] = None,
            signed: bool = False
        ) -> Dict[str, Any]:

            if params is None:
                params = {}

            headers = {
                'X-BX-APIKEY': self.api_key
            }

            session = await self._get_session()
            last_exception: Optional[Exception] = None

            for attempt in range(self.max_retries):
                # для подписанных запросов timestamp и signature пересчитываются
                # на КАЖДОЙ попытке — иначе retry уходит со старым timestamp
                request_params = dict(params)
                if signed:
                    request_params.setdefault('recvWindow', 60000)
                    request_params['timestamp'] = await self._get_timestamp_ms()
                    params_str = self._parse_param(request_params)
                    logger.info(f"[SIGN DEBUG] endpoint={endpoint}, params_str={params_str}")
                    request_params['signature'] = self._generate_signature(params_str)

                query_string = self._parse_param(request_params) if request_params else ""
                url = f"{self.base_url}{endpoint}"
                full_url = f"{url}?{query_string}" if query_string else url

                try:
                    if method == 'GET':
                        async with session.get(full_url, headers=headers) as response:
                            result = await response.json()
                            response.raise_for_status()
                    elif method == 'POST':
                        async with session.post(full_url, headers=headers) as response:
                            result = await response.json()
                            response.raise_for_status()
                    elif method == 'DELETE':
                        async with session.delete(full_url, headers=headers) as response:
                            result = await response.json()
                            response.raise_for_status()
                    else:
                        raise ValueError(f"Unsupported HTTP method: {method}")

                    # BingX возвращает HTTP 200 даже при бизнес-ошибках (например,
                    # неверная подпись или expired timestamp) — код ошибки лежит в теле.
                    # Ловим это явно, чтобы иметь возможность пересинхронизировать время и повторить.
                    if isinstance(result, dict) and result.get('code') not in (0, None):
                        error_code = result.get('code')
                        error_msg = result.get('msg', '')

                        # 100001 = signature mismatch, может быть из-за рассинхрона времени —
                        # принудительно пересинхронизируем перед следующей попыткой
                        if signed and error_code == 100001 and attempt < self.max_retries - 1:
                            logger.warning(
                                f"Signature mismatch (attempt {attempt + 1}/{self.max_retries}), "
                                f"forcing time resync: {error_msg}"
                            )
                            await self._sync_server_time(force=True)
                            await asyncio.sleep(self.retry_delay)
                            continue

                        logger.error(f"BingX API error {error_code}: {error_msg}")

                    return result

                except aiohttp.ClientError as e:
                    last_exception = e
                    logger.warning(f"Request failed (attempt {attempt + 1}/{self.max_retries}): {e}")

                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(self.retry_delay)
                    else:
                        logger.error(f"Request failed after {self.max_retries} attempts")
                        raise

            if last_exception:
                raise last_exception
            raise Exception("Request failed")

    async def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None, signed: bool = False) -> Dict[str, Any]:
        return await self._request('GET', endpoint, params, signed)

    async def post(self, endpoint: str, params: Optional[Dict[str, Any]] = None, signed: bool = True) -> Dict[str, Any]:
        return await self._request('POST', endpoint, params, signed)

    async def delete(self, endpoint: str, params: Optional[Dict[str, Any]] = None, signed: bool = True) -> Dict[str, Any]:
        return await self._request('DELETE', endpoint, params, signed)

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("REST client session closed")