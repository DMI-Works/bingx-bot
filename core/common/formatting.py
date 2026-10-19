def price_decimals(*values: float) -> int:
    """Кількість знаків після коми для ціни: за першим ненульовим значенням
    (4 для цін ≥ 1, 6 для ≥ 0.01, інакше 8)."""
    ref = 0.0
    for value in values:
        if value:
            ref = abs(value)
            break
    if ref >= 1:
        return 4
    if ref >= 0.01:
        return 6
    return 8
