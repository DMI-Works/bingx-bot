"""
Розрахунок комісії за тиждень (чисті функції, без БД).

Комісія береться з ЧИСТОГО підсумку тижня (усі угоди з комісіями біржі) з
перенесенням: збиток попередніх тижнів спершу треба відіграти, і лише прибуток
понад нього оплачується. Якщо сума комісії менша за мінімальний рахунок,
результат теж переноситься на наступний тиждень, нічого не втрачається.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PeriodResult:
    chargeable: float  # прибуток, з якого взято комісію
    amount: float      # сума рахунку в USDT (0 — рахунку немає)
    carry_after: float # що переходить на наступний тиждень


def calculate_period(net_pnl: float, carry_before: float, fee_percent: float, min_invoice: float) -> PeriodResult:
    balance = round(carry_before + net_pnl, 8)
    if balance <= 0:
        return PeriodResult(chargeable=0.0, amount=0.0, carry_after=balance)
    amount = round(balance * fee_percent / 100.0, 2)
    if amount <= 0 or amount < min_invoice:
        return PeriodResult(chargeable=0.0, amount=0.0, carry_after=balance)
    return PeriodResult(chargeable=balance, amount=amount, carry_after=0.0)
