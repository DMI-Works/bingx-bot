"""
Юніт-тести на формулу net_pnl (ТЗ TZ_fix_pnl_accounting.md, задача 1).

Критерій приймання задачі 1 (дослівно з ТЗ):
    "юнит-тест на примере из данных: realized_pnl=0.384118, комиссии открытия
    и закрытия по -0.0498 каждая, funding 0, итог net_pnl ≈ 0.2845, а не 0.4339"

Запуск: python -m unittest tests.test_pnl_accounting -v
(або pytest tests/test_pnl_accounting.py -v, якщо pytest встановлено —
тест навмисно написаний на stdlib unittest, щоб не додавати нову залежність
лише заради цього).

ПРИМІТКА про MONGO_URI: core/database/database.py валідує змінну оточення
MONGO_URI вже на рівні імпорту модуля (raise RuntimeError, якщо не задана).
SimpleTrader імпортує Database транзитивно (через core.risk -> RiskManager),
тож для самого імпорту core.trading.simple_trader потрібен хоч якийсь
MONGO_URI в оточенні — реального з'єднання з Mongo тест НЕ відкриває
(жоден метод Database не викликається, перевіряється лише статична
математика _calc_net_pnl).
"""
import os
import unittest

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")

from core.trading.simple_trader import SimpleTrader  # noqa: E402


class TestNetPnlFormula(unittest.TestCase):
    def test_acceptance_example_from_tz(self):
        """Приклад із ТЗ: реальна угода зі звірки бот/біржа."""
        net_pnl = SimpleTrader._calc_net_pnl(
            realized_pnl=0.384118,
            commission_open=-0.0498,
            commission_close=-0.0498,
            funding_fee=0.0,
        )
        self.assertAlmostEqual(net_pnl, 0.2845, places=4)
        # Старий (хибний) результат для контрасту — net_pnl НЕ повинен
        # випадково збігтись зі старою, завищеною формулою.
        old_buggy_result = 0.384118 - (-0.0498 + -0.0498)
        self.assertAlmostEqual(old_buggy_result, 0.4837, places=4)
        self.assertNotAlmostEqual(net_pnl, old_buggy_result, places=2)

    def test_sign_convention_fees_are_negative(self):
        """Комісії та funding — від'ємні списання, формула додає їх (а не
        віднімає), тож збиткова по комісії угода коректно зменшує net_pnl."""
        net_pnl = SimpleTrader._calc_net_pnl(
            realized_pnl=1.0,
            commission_open=-0.05,
            commission_close=-0.05,
            funding_fee=-0.01,
        )
        self.assertAlmostEqual(net_pnl, 0.89, places=4)

    def test_positive_funding_increases_net_pnl(self):
        """Funding може бути і нарахуванням (позитивним) для шорт-позицій
        при від'ємній ставці фінансування — формула має це коректно додати."""
        net_pnl = SimpleTrader._calc_net_pnl(
            realized_pnl=0.0,
            commission_open=-0.05,
            commission_close=-0.05,
            funding_fee=0.02,
        )
        self.assertAlmostEqual(net_pnl, -0.08, places=4)

    def test_missing_values_default_to_zero(self):
        """None на будь-якому з полів (напр. ще не заповнений funding_fee
        для старих позицій) не повинен ламати розрахунок — трактується як 0."""
        net_pnl = SimpleTrader._calc_net_pnl(
            realized_pnl=0.5,
            commission_open=None,
            commission_close=-0.05,
            funding_fee=None,
        )
        self.assertAlmostEqual(net_pnl, 0.45, places=4)

    def test_zero_commissions_net_pnl_equals_realized_pnl(self):
        net_pnl = SimpleTrader._calc_net_pnl(realized_pnl=1.2345)
        self.assertAlmostEqual(net_pnl, 1.2345, places=4)


if __name__ == "__main__":
    unittest.main()