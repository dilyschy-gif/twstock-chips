# -*- coding: utf-8 -*-
"""
test_day_trading.py — 當沖選股雷達規則測試（不連網，用合成快照）

執行：python -m unittest test_day_trading -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))

import day_trading as dt  # noqa: E402


def make_item(code, name, market, close, high, low, prev, volume, turnover,
              industry_placeholder=None, open_=None):
    return {
        "code": code, "name": name, "market": market,
        "open": open_ if open_ is not None else prev,
        "high": high, "low": low, "close": close, "prev": prev,
        "volume": volume, "turnover": turnover,
    }


def write_snapshot(out_dir, day, items):
    path = dt.daily_path(out_dir, day)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": day, "count": len(items), "items": items}, f,
                  ensure_ascii=False)


def trading_days(n, start_yyyymmdd=20260601):
    """產生 n 個「假交易日」字串（跳過週末不重要，只要遞增即可）。"""
    from datetime import date, timedelta
    y, m, d = (int(str(start_yyyymmdd)[:4]), int(str(start_yyyymmdd)[4:6]),
               int(str(start_yyyymmdd)[6:8]))
    cur = date(y, m, d)
    out = []
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(f"{cur:%Y%m%d}")
        cur = cur.__class__.fromordinal(cur.toordinal() + 1)
    return out


class TestHelpers(unittest.TestCase):
    def test_is_common_stock(self):
        self.assertTrue(dt.is_common_stock("2330"))
        self.assertTrue(dt.is_common_stock("8069"))
        self.assertFalse(dt.is_common_stock("0050"))    # ETF
        self.assertFalse(dt.is_common_stock("00893"))   # ETF
        self.assertFalse(dt.is_common_stock("078080"))  # 權證
        self.assertFalse(dt.is_common_stock("2330A"))   # 特別股
        self.assertFalse(dt.is_common_stock("910322"))  # DR

    def test_to_float_special(self):
        self.assertIsNone(dt.to_float("--"))
        self.assertIsNone(dt.to_float("除息"))
        self.assertEqual(dt.to_float("1,234.5"), 1234.5)
        self.assertEqual(dt.to_float("(3.2)"), -3.2)

    def test_daily_amplitude(self):
        item = {"high": 103.0, "low": 97.0, "prev": 100.0}
        self.assertAlmostEqual(dt.daily_amplitude(item, None), 6.0)
        item2 = {"high": 103.0, "low": 97.0, "prev": None}
        self.assertAlmostEqual(dt.daily_amplitude(item2, 100.0), 6.0)
        self.assertIsNone(dt.daily_amplitude(item2, None))


class TestScan(unittest.TestCase):
    """端到端：合成 32 個交易日快照，驗證三條件與族群排名。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        days = trading_days(32)
        self.latest = days[-1]

        # 產業別假資料
        self.industry = {
            "1101": "水泥", "1102": "水泥", "1103": "水泥",
            "2330": "半導體", "2454": "半導體", "3034": "半導體",
            "2603": "航運", "2609": "航運", "2615": "航運",
        }

        for i, day in enumerate(days):
            items = []
            # 半導體族群：大漲、大量 → 應成為熱門族群
            for code, base in (("2330", 1000), ("2454", 1200), ("3034", 500)):
                px = base * (1 + 0.01 * i)
                prev = base * (1 + 0.01 * (i - 1)) if i > 0 else base
                items.append(make_item(
                    code, f"S{code}", "上市",
                    close=round(px, 1),
                    high=round(px * 1.025, 1), low=round(px * 0.975, 1),
                    prev=round(prev, 1),
                    volume=30_000_000, turnover=int(px * 30_000_000)))
            # 航運族群：下跌 → 不應是熱門族群
            for code, base in (("2603", 200), ("2609", 150), ("2615", 180)):
                px = base * (1 - 0.005 * i)
                prev = base * (1 - 0.005 * (i - 1)) if i > 0 else base
                items.append(make_item(
                    code, f"S{code}", "上市",
                    close=round(px, 1),
                    high=round(px * 1.02, 1), low=round(px * 0.98, 1),
                    prev=round(prev, 1),
                    volume=20_000_000, turnover=int(px * 20_000_000)))
            # 水泥族群：牛皮 —— 量大但振幅只有 ~1%，條件二不過
            for code in ("1101", "1102", "1103"):
                items.append(make_item(
                    code, f"S{code}", "上市",
                    close=50.0, high=50.3, low=49.8, prev=50.0,
                    volume=5_000_000, turnover=250_000_000))
            # 低量小股：振幅夠但量不足（400 張、2000 萬）→ 條件一不過
            items.append(make_item(
                "6188", "小量股", "上櫃",
                close=50.0, high=52.0, low=48.0, prev=50.0,
                volume=400_000, turnover=20_000_000))
            write_snapshot(self.tmp, day, items)

        # 最後一天讓 6188 突然爆量（量比 >2、金額破億）→ 焦點資金股
        snap = json.load(open(dt.daily_path(self.tmp, self.latest),
                              encoding="utf-8"))
        for it in snap["items"]:
            if it["code"] == "6188":
                it["volume"] = 3_000_000
                it["turnover"] = 150_000_000
        with open(dt.daily_path(self.tmp, self.latest), "w",
                  encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)

        self.result = dt.scan(self.tmp, self.industry, {"2330": 15_000_000})
        self.by_code = {s["code"]: s for s in self.result["stocks"]}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_semiconductor_is_hot_and_passes_all(self):
        sectors = {r["name"]: r for r in self.result["sectors"]}
        self.assertTrue(sectors["半導體"]["hot"])
        s = self.by_code["2330"]
        self.assertEqual(s["passed"], 3)
        self.assertTrue(s["cond"]["vol"] and s["cond"]["amp"] and s["cond"]["hot"])
        # 30 日平均振幅 ~5%（2.5% 上下各半）
        self.assertGreaterEqual(s["amp30"], 3.0)

    def test_declining_sector_not_hot(self):
        sectors = {r["name"]: r for r in self.result["sectors"]}
        self.assertFalse(sectors["航運"]["hot"])  # w_change < 0

    def test_low_amplitude_fails_cond2(self):
        s = self.by_code.get("1101")
        if s is not None:  # 兩條件觀察股仍會出現在名單
            self.assertFalse(s["cond"]["amp"])
            self.assertLess(s["amp30"], 3.0)
            self.assertEqual(s["passed"], 2)

    def test_focus_stock_rule(self):
        """爆量小股：族群未分類，但量比>=2 且金額破億 → 條件三以焦點資金股成立。"""
        s = self.by_code["6188"]
        self.assertTrue(s["cond"]["hot"])
        self.assertIn("焦點資金股", s["hot_reason"])
        self.assertTrue(s["cond"]["vol"])   # 3000 張、1.5 億
        self.assertTrue(s["cond"]["amp"])   # 平均振幅 8%
        self.assertEqual(s["passed"], 3)

    def test_dt_ratio(self):
        s = self.by_code["2330"]
        self.assertAlmostEqual(s["dt_ratio"], 50.0)  # 1500萬/3000萬

    def test_ranking_pass3_first(self):
        stocks = self.result["stocks"]
        seen2 = False
        for s in stocks:
            if s["passed"] == 2:
                seen2 = True
            if seen2:
                self.assertEqual(s["passed"], 2)  # 三條件的不可排在兩條件之後

    def test_output_shape(self):
        self.assertEqual(self.result["data_date"],
                         f"{self.latest[:4]}-{self.latest[4:6]}-{self.latest[6:8]}")
        for key in ("params", "counts", "sectors", "stocks"):
            self.assertIn(key, self.result)
        s = self.by_code["2330"]
        self.assertLessEqual(len(s["closes"]), dt.SPARK_DAYS)
        self.assertEqual(s["industry"], "半導體")


if __name__ == "__main__":
    unittest.main()
