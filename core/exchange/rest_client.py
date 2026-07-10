import aiohttp
import hmac
import hashlib
import time
import logging
import json
from typing import Dict, Any, Optional
from urllib.parse import urlencode


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

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False
    ) -> Dict[str, Any]:

        if params is None:
            params = {}

        if signed:
            params['timestamp'] = int(time.time() * 1000)
            params_str = self._parse_param(params)
            signature = self._generate_signature(params_str)
            params['signature'] = signature

        headers = {
            'X-BX-APIKEY': self.api_key
        }

        url = f"{self.base_url}{endpoint}"
        session = await self._get_session()

        for attempt in range(self.max_retries):
            try:
                if method == 'GET':
                    async with session.get(url, params=params, headers=headers) as response:
                        result = await response.json()
                        response.raise_for_status()
                        return result

                elif method == 'POST':
                    # For POST, add params to URL query string
                    full_url = f"{url}?{self._parse_param(params)}" if params else url
                    async with session.post(full_url, headers=headers) as response:
                        result = await response.json()
                        response.raise_for_status()
                        return result

                elif method == 'DELETE':
                    async with session.delete(url, params=params, headers=headers) as response:
                        result = await response.json()
                        response.raise_for_status()
                        return result

            except aiohttp.ClientError as e:
                logger.warning(f"Request failed (attempt {attempt + 1}/{self.max_retries}): {e}")

                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay)
                else:
                    logger.error(f"Request failed after {self.max_retries} attempts")
                    raise

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


import asyncio
