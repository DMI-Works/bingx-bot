import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TrailState:
    """Стан супроводу SL однієї позиції (в пам'яті TrailingStopManager)."""
    initial_stop_price: Optional[float] = None
    last_positive_stop_price: Optional[float] = None
    last_positive_roi_percent: Optional[float] = None
    fallback_notice_key: Optional[str] = None
    critical_notice_key: Optional[str] = None
    # --- режим roi_gap: найкращий досягнутий ROI позиції (%) ---
    peak_roi_percent: Optional[float] = None
    roi_gap_last_move_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reset_notices(self) -> None:
        """Стоп штатно просунувся — наступні аварійні сповіщення знову доречні."""
        self.fallback_notice_key = None
        self.critical_notice_key = None
