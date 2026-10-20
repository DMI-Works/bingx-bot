"""Зведення /api/stats за період (webapp/backend/services/trade_stats.py)."""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webapp.backend.services.trade_stats import period_cutoff, summarize_closed_trades  # noqa: E402


def row(net, hours_ago, symbol='A-USDT'):
    return {
        'net_pnl': net, 'realized_pnl': net, 'symbol': symbol, 'metadata': '{}',
        'commission_usdt': 0.0, 'closed_at': datetime.utcnow() - timedelta(hours=hours_ago),
    }


class TestSummarizeClosedTrades(unittest.TestCase):
    def setUp(self):
        # від нових до старих, як get_all_closed_positions
        self.rows = [row(5.0, 2), row(-3.0, 30, 'B-USDT'), row(10.0, 100)]

    def test_day_period_excludes_older_trades(self):
        s = summarize_closed_trades(self.rows, period_cutoff('1D'))
        self.assertEqual(s['total_trades'], 1)
        self.assertEqual(s['total_net_pnl'], 5.0)
        self.assertEqual(s['cumulative_pnl_raw'], 12.0)  # весь час
        self.assertEqual([p['v'] for p in s['equity']], [0.0, 5.0])  # графік від 0 і з однією угодою
        self.assertEqual([x['symbol'] for x in s['symbol_pnl']], ['A-USDT'])

    def test_curve_ends_at_period_pnl(self):
        s = summarize_closed_trades(self.rows, period_cutoff('1W'))
        self.assertEqual([p['v'] for p in s['equity']], [0.0, 10.0, 7.0, 12.0])
        self.assertEqual(s['equity'][-1]['v'], s['total_net_pnl'])

    def test_empty_period(self):
        s = summarize_closed_trades([row(1.0, 50)], period_cutoff('1D'))
        self.assertEqual(s['equity'], [])
        self.assertEqual(s['total_trades'], 0)


if __name__ == '__main__':
    unittest.main()
