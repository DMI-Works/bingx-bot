"""
Стан позиції SimpleTrader зберігається JSON-рядком у positions.metadata.
Тут — єдине місце, де цей рядок читається й пишеться (раніше json.dumps/
json.loads з однаковою обробкою помилок були в трейдері, трейлінгу і webapp).
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def load_metadata(raw: Any) -> dict:
    """metadata-рядок -> dict ({} для порожнього або зіпсованого значення)."""
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def dump_metadata(position: dict) -> str:
    return json.dumps(position)


def persist_position_metadata(db, position: dict, context: str) -> None:
    """Перезаписує positions.metadata поточним станом позиції. Помилку БД
    лише логуємо: торгова логіка не повинна падати через збій запису."""
    try:
        db.update_position_metadata(order_id=position['order_id'], metadata=dump_metadata(position))
    except Exception as e:
        logger.error(
            f"Failed to persist position metadata ({context}) for "
            f"{position.get('symbol')}_{position.get('side')}: {e}", exc_info=True
        )
