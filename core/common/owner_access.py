"""
Хто є власником бота (однокористувацька версія).

Mini App і кнопки аварійної зупинки керують РЕАЛЬНИМ рахунком, тому пускаємо
лише Telegram id власника:
- ADMIN_TELEGRAM_ID — особистий id (можна кілька через кому);
- TELEGRAM_CHAT_ID — якщо це особистий чат (додатне число), id чату = id
  користувача. Груповий чат (від'ємний id) доступу не дає.
Нічого не задано — доступ закритий для всіх (fail closed).
"""

import os
from typing import Set


def owner_ids() -> Set[str]:
    ids = set()
    for part in (os.getenv("ADMIN_TELEGRAM_ID") or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(part)
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if chat_id.isdigit():
        ids.add(chat_id)
    return ids


def is_owner(user_id) -> bool:
    return user_id is not None and str(user_id) in owner_ids()
