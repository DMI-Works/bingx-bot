"""
Чи може користувач відкривати НОВІ угоди. Супровід уже відкритих позицій
(SL, фіксація прибутку, закриття по розвороту) працює завжди — чужі гроші
без захисту не кидаємо.

Сюди ж підключиться підписка (етап «Подписка»): неоплачений рахунок -> False.
"""

from __future__ import annotations

from typing import Optional, Tuple


def can_open_new(user: Optional[dict]) -> Tuple[bool, str]:
    if not user:
        return False, "користувача не знайдено"
    if user.get("status") != "active":
        return False, "акаунт заблоковано"
    return True, ""
