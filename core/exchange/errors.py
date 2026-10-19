import re
from typing import Any, Dict, Optional

# Ордер уже виконаний/скасований/не існує — повторювати дію з ним безглуздо
GONE_ERROR_CODES = (100404, 100400, 109400, 109420)
_GONE_ERROR_SUBSTRINGS = ('not exist', 'not found')

RATE_LIMIT_CODE = 109429
SL_ALREADY_EXISTS_CODE = 110406
API_ORDERS_DISABLED_CODE = 109400


class BingXAPIError(Exception):
    def __init__(self, code: int, msg: str, endpoint: str = "", response: Optional[Dict[str, Any]] = None):
        self.code = code
        self.msg = msg
        self.endpoint = endpoint
        self.response = response or {}
        super().__init__(f"BingX API error {code} at {endpoint}: {msg}")


def is_gone_error(e: BingXAPIError) -> bool:
    if e.code in GONE_ERROR_CODES:
        return True
    msg = (e.msg or '').lower()
    return any(s in msg for s in _GONE_ERROR_SUBSTRINGS)


def parse_retry_after(msg: Optional[str]) -> Optional[float]:
    """Парсить 'can retry after time: 1786999966671' (мс epoch) з тіла помилки
    BingX і повертає unix timestamp у секундах, до якого варто утриматись
    від нових запитів."""
    if not msg:
        return None
    m = re.search(r'retry after time:\s*(\d+)', msg)
    if not m:
        return None
    try:
        return int(m.group(1)) / 1000.0
    except ValueError:
        return None
