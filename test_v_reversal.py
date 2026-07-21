# -*- coding: utf-8 -*-

import copy
import datetime
import unittest

from v_reversal import evaluate_v_reversal, sanitize_history


def build_unipcb_like_history():
    """建立類似 3037：連三黑後，低量漲停且收最高的第一根紅 K。"""
    start = datetime.date(2026, 6, 1)
    rows = []
    close = 978.0
    for index in range(26):
        close -= 1.0
        rows.append({
            "date": (start + datetime.timedelta(days=index)).isoformat(),
            "open": close + 2,
            "high": close + 7,
            "low": close - 5,
            "close": close,
            "volume": 30_000_000,
        })

    rows.extend([
        {"date": "2026-07-15", "open": 882, "high": 960, "low": 870, "close": 936, "volume": 34_000_000},
        {"date": "2026-07-16", "open": 911, "high": 915, "low": 872, "close": 882, "volume": 39_000_000},
        {"date": "2026-07-17", "open": 809, "high": 842, "low": 794, "close": 794, "volume": 43_000_000},
        {"date": "2026-07-20", "open": 778, "high": 812, "low": 747, "close": 750, "volume": 31_000_000},
        {"date": "2026-07-21", "open": 785, "high": 825, "low": 775, "close": 825, "volume": 18_000_000},
    ])
    return rows


class VReversalTest(unittest.TestCase):
    def test_before_first_red_candle_is_v0(self):
        result = evaluate_v_reversal(build_unipcb_like_history()[:-1], market_change_pct=-1.0)
        self.assertIsNotNone(result)
        self.assertEqual("V0", result["state"])
        self.assertIsNone(result["trigger_mid"])

    def test_3037_shape_is_v1_even_with_low_limit_up_volume(self):
        result = evaluate_v_reversal(
            build_unipcb_like_history(),
            chip={
                "latest_total": 1331,
                "latest_trust": 573,
                "trust_positive_days_5": 3,
                "foreign_turn_buy": True,
            },
            market_change_pct=4.2,
        )

        self.assertIsNotNone(result)
        self.assertEqual("V1", result["state"])
        self.assertAlmostEqual(747, result["v_bottom"])
        self.assertAlmostEqual(800, result["trigger_mid"])
        self.assertAlmostEqual(842, result["v2_confirm"])
        self.assertLess(result["volume_ratio"], 1.2)
        self.assertIn("低量漲停例外", result["badges"])

    def test_long_upper_wick_is_not_v1(self):
        rows = build_unipcb_like_history()
        rows[-1].update({"high": 900, "close": 825})
        result = evaluate_v_reversal(rows, market_change_pct=4.2)
        self.assertTrue(result is None or result["state"] != "V1")

    def test_follow_through_becomes_v2(self):
        rows = build_unipcb_like_history()
        rows.append({
            "date": "2026-07-22",
            "open": 822,
            "high": 850,
            "low": 812,
            "close": 845,
            "volume": 25_000_000,
        })
        result = evaluate_v_reversal(rows, market_change_pct=0.5)
        self.assertIsNotNone(result)
        self.assertEqual("V2", result["state"])

    def test_two_consecutive_closes_below_midpoint_become_vx(self):
        rows = build_unipcb_like_history()
        rows.extend([
            {"date": "2026-07-22", "open": 808, "high": 815, "low": 782, "close": 790, "volume": 25_000_000},
            {"date": "2026-07-23", "open": 792, "high": 798, "low": 770, "close": 780, "volume": 24_000_000},
        ])
        result = evaluate_v_reversal(rows, market_change_pct=-0.5)
        self.assertIsNotNone(result)
        self.assertEqual("VX", result["state"])

    def test_zero_volume_placeholder_is_ignored(self):
        rows = build_unipcb_like_history()
        placeholder = copy.deepcopy(rows[-1])
        placeholder.update({"date": "2026-07-20z", "volume": 0, "close": 1, "open": 1, "high": 1, "low": 1})
        rows.insert(-1, placeholder)
        clean = sanitize_history(rows)
        self.assertEqual(len(rows) - 1, len(clean))
        self.assertEqual("V1", evaluate_v_reversal(rows, market_change_pct=4.2)["state"])


if __name__ == "__main__":
    unittest.main()
