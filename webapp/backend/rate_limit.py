"""
Обмеження частоти запитів до Mini App API (у пам'яті: процес один).

- кожен користувач: загальний ліміт на весь /api (живе оновлення статистики
  ходить раз на 2 с — запас з головою);
- ендпоінти, що ходять на біржу: окремий, менший ліміт — інакше один
  користувач може вибрати ліміт BingX за IP сервера для всіх;
- збереження ключів: кілька спроб на 10 хвилин (кожна — запит на біржу);
- невдала авторизація: ліміт на IP, щоб сміттєві запити не засмічували лог.
Перевищення — 429 із Retry-After.
"""

import math
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from fastapi import HTTPException, Request

from .i18n import tr


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_sweep = time.monotonic()

    def hit(self, bucket: str, key: str, limit: int, window: float) -> float:
        """Рахує спробу. 0 — дозволено, інакше скільки секунд чекати."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits[(bucket, key)]
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return max(hits[0] + window - now, 0.1)
            hits.append(now)
            self._sweep(now)
            return 0.0

    def _sweep(self, now: float) -> None:
        # раз на 10 хв прибираємо порожні черги, щоб словник не ріс безмежно
        if now - self._last_sweep < 600:
            return
        self._last_sweep = now
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - 3600]:
            del self._hits[k]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()

# (ліміт, вікно в секундах)
USER_API_LIMIT = (300, 60)
EXCHANGE_LIMIT = (40, 60)
KEYS_LIMIT = (5, 600)
AUTH_FAIL_IP_LIMIT = (30, 60)


def client_ip(request: Request) -> str:
    """Railway/Cloudflare стоять перед застосунком: реальна адреса — в заголовках."""
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # останній запис додає наш проксі — його підробити найважче
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "?"


def enforce(bucket: str, key: str, limit_window: Tuple[int, float]) -> None:
    limit, window = limit_window
    retry_after = limiter.hit(bucket, key, limit, window)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=tr("err_rate_limited"),
            headers={"Retry-After": str(math.ceil(retry_after))},
        )
