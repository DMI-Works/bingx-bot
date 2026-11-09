"""
Чи може користувач відкривати НОВІ угоди. Супровід уже відкритих позицій
(SL, фіксація прибутку, закриття по розвороту) працює завжди — чужі гроші
без захисту не кидаємо.

Неоплачений рахунок підписки (core/billing) теж забороняє нові входи.
"""

from __future__ import annotations

from typing import Optional, Tuple


def can_open_new(user: Optional[dict]) -> Tuple[bool, str]:
    if not user:
        return False, "користувача не знайдено"
    if user.get("status") != "active":
        return False, "акаунт заблоковано"
    if user.get("role") != "admin" and (user.get("billing") or {}).get("unpaid"):
        return False, "є неоплачений рахунок"
    return True, ""
