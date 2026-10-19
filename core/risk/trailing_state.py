import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TrailState:
    """Стан супроводу SL однієї позиції (в пам'яті TrailingStopManager)."""
    last_applied_level_index: int = -1
    initial_stop_price: Optional[float] = None
    last_positive_stop_price: Optional[float] = None
    last_positive_roi_percent: Optional[float] = None
    fallback_notice_key: Optional[str] = None
    critical_notice_key: Optional[str] = None
    # --- режим atr_3step: кеш кроку 3 (свінг-стоп за екстремумом свічок) ---
    atr_swing_stop: Optional[float] = None
    atr_candle_bucket: Optional[int] = None
    atr_refresh_inflight: bool = False
    atr_refresh_retry_after: float = 0.0
    atr_not_ready_count: int = 0
    atr_invalid_notice: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reset_notices(self) -> None:
        """Стоп штатно просунувся — наступні аварійні сповіщення знову доречні."""
        self.fallback_notice_key = None
        self.critical_notice_key = None
