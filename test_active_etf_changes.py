import unittest

from fetch_active_etf_changes import (
    build_consensus,
    build_report,
    build_report_from_snapshots,
    manager_from_name,
    market_from_symbol,
    merge_history,
    merge_snapshot_fund,
    parse_portfolios,
)


TITLE = ["日期", "標的代號", "標的名稱", "權重(%)", "持有數", "單位"]


def fund(code="00999A", name="主動野村測試", category="domestic"):
    return {
        "code": code,
        "name": name,
        "manager": manager_from_name(name),
        "category": category,
        "region": "台股型" if category == "domestic" else "海外型",
        "priority": False,
        "official_url": "https://example.com",
    }


class ActiveEtfChangesTest(unittest.TestCase):
    def test_detects_added_removed_and_share_changes(self):
        rows = [
            ["20260722", "2330", "台積電", "10.00", "100000", "股"],
            ["20260722", "2303", "聯電", "2.00", "50000", "股"],
            ["20260722", "2317", "鴻海", "4.00", "60000", "股"],
            ["20260722", "CASH_NTD", "現金", "", "1000000", "元"],
            ["20260723", "2330", "台積電", "9.50", "110000", "股"],
            ["20260723", "2317", "鴻海", "4.20", "59000", "股"],
            ["20260723", "2454", "聯發科", "3.00", "25000", "股"],
            ["20260723", "CASH_NTD", "現金", "", "800000", "元"],
        ]

        report = build_report("00999A", "主動野村測試", "https://example.com", TITLE, rows)

        self.assertEqual("20260723", report["data_date"])
        self.assertEqual("20260722", report["previous_date"])
        self.assertEqual(["2454"], [item["symbol"] for item in report["added"]])
        self.assertEqual(25, report["added"][0]["lots"])
        self.assertEqual(["2303"], [item["symbol"] for item in report["removed"]])
        self.assertEqual(50, report["removed"][0]["lots"])
        self.assertEqual(["2330"], [item["symbol"] for item in report["increased"]])
        self.assertEqual(10000, report["increased"][0]["adjusted_share_change"])
        self.assertAlmostEqual(10, report["increased"][0]["position_change_pct"])
        self.assertAlmostEqual(-0.5, report["increased"][0]["weight_drift_pp"])
        self.assertEqual(["2317"], [item["symbol"] for item in report["decreased"]])
        self.assertEqual(-1000, report["decreased"][0]["adjusted_share_change"])
        self.assertAlmostEqual(-1.6667, report["decreased"][0]["position_change_pct"])
        self.assertAlmostEqual(0.2, report["decreased"][0]["weight_drift_pp"])

    def test_ignores_weight_change_when_shares_are_unchanged(self):
        rows = [
            ["20260722", "2330", "台積電", "10.00", "100000", "股"],
            ["20260723", "2330", "台積電", "12.00", "100000", "股"],
        ]
        report = build_report("00999A", "主動野村測試", "https://example.com", TITLE, rows)
        self.assertEqual([], report["increased"])
        self.assertEqual([], report["decreased"])

    def test_foreign_stock_keeps_shares_without_taiwan_lot_conversion(self):
        rows = [
            ["20260721", "2330", "台積電", "1", "10000", "股"],
            ["20260722", "2330", "台積電", "1", "10000", "股"],
            ["20260722", "AMD US", "AMD", "6", "205000", "股"],
        ]
        report = build_report("00988A", "主動統一全球創新", "https://example.com", TITLE, rows)
        added = report["added"][0]
        self.assertEqual("AMD US", added["symbol"])
        self.assertEqual("US", added["market_code"])
        self.assertEqual("美國", added["market"])
        self.assertEqual(205000, added["shares"])
        self.assertIsNone(added["lots"])

    def test_korea_and_germany_market_suffixes(self):
        self.assertEqual(("KP", "韓國"), market_from_symbol("000660 KP"))
        self.assertEqual(("GR", "德國"), market_from_symbol("IFX GR"))

    def test_one_date_creates_baseline_instead_of_blocking_all_funds(self):
        rows = [["20260723", "2330", "台積電", "10", "100000", "股"]]
        report = build_report("00999A", "主動野村測試", "https://example.com", TITLE, rows)
        self.assertEqual("baseline", report["status"])
        self.assertEqual([], report["added"])
        self.assertEqual([], report["removed"])

    def test_three_and_five_day_trends_use_shares_not_weight(self):
        snapshots = []
        for index, (shares, weight) in enumerate(
            zip(
                [100000, 110000, 120000, 130000, 140000],
                [5.0, 4.8, 4.6, 4.4, 4.2],
            ),
            start=1,
        ):
            snapshots.append(
                {
                    "date": f"202607{index:02d}",
                    "holdings": [
                        {
                            "symbol": "2330",
                            "name": "台積電",
                            "market_code": "TW",
                            "market": "台灣",
                            "shares": shares,
                            "lots": shares / 1000,
                            "weight": weight,
                            "unit": "股",
                        }
                    ],
                }
            )
        report = build_report_from_snapshots(fund(), snapshots)
        self.assertAlmostEqual(
            16.6667,
            report["trend_3d"][0]["position_change_pct"],
        )
        self.assertAlmostEqual(40, report["trend_5d"][0]["position_change_pct"])
        self.assertGreaterEqual(report["increased"][0]["streak"], 3)

    def test_proportional_etf_growth_is_removed_before_action_detection(self):
        rows = []
        for index in range(6):
            symbol = str(2300 + index)
            rows.append(
                ["20260722", symbol, f"股票{index}", "2", "100000", "股"]
            )
            current_shares = 120000 if index == 5 else 110000
            rows.append(
                [
                    "20260723",
                    symbol,
                    f"股票{index}",
                    "2",
                    str(current_shares),
                    "股",
                ]
            )
        report = build_report(
            "00999A",
            "主動野村測試",
            "https://example.com",
            TITLE,
            rows,
        )
        self.assertTrue(report["flow_adjusted"])
        self.assertAlmostEqual(1.1, report["flow_scale"])
        self.assertEqual(["2305"], [item["symbol"] for item in report["increased"]])
        self.assertEqual([], report["decreased"])

    def test_consensus_caps_duplicate_funds_from_same_manager(self):
        base_item = {
            "symbol": "2330",
            "name": "台積電",
            "market_code": "TW",
            "market": "台灣",
            "shares": 100000,
            "lots": 100,
            "weight": 4,
            "previous_shares": 100000,
            "current_shares": 110000,
            "raw_share_change": 10000,
            "adjusted_share_change": 10000,
            "position_change_pct": 10,
            "flow_scale": 1,
            "flow_adjusted": False,
            "previous_weight": 3.5,
            "current_weight": 4,
            "weight_drift_pp": 0.5,
            "rank": 1,
            "previous_rank": 2,
            "streak": 1,
        }

        def report(code, manager):
            return {
                **fund(code=code, name=f"主動{manager}測試"),
                "manager": manager,
                "status": "ok",
                "added": [],
                "removed": [],
                "increased": [base_item],
                "decreased": [],
                "top10_entered": [base_item],
                "top10_exited": [],
            }

        consensus = build_consensus(
            [report("A1", "野村"), report("A2", "野村"), report("A3", "群益")],
            "domestic",
        )
        item = consensus["bullish"][0]
        self.assertEqual(2, item["manager_count"])
        self.assertEqual(3, item["etf_count"])
        self.assertLessEqual(item["score"], 8)

    def test_snapshot_merge_keeps_old_dates_and_replaces_same_date(self):
        old_state = {
            "snapshots": [
                {
                    "date": "20260722",
                    "holdings": [
                        {
                            "symbol": "2330",
                            "name": "台積電",
                            "market_code": "TW",
                            "market": "台灣",
                            "shares": 100000,
                            "lots": 100,
                            "weight": 10,
                            "unit": "股",
                        }
                    ],
                }
            ]
        }
        fetched = parse_portfolios(
            TITLE,
            [
                ["20260722", "2330", "台積電", "11", "110000", "股"],
                ["20260723", "2454", "聯發科", "5", "50000", "股"],
            ],
        )
        merged = merge_snapshot_fund(fund(), old_state, fetched)
        self.assertEqual(["20260722", "20260723"], [s["date"] for s in merged["snapshots"]])
        self.assertEqual(11, merged["snapshots"][0]["holdings"][0]["weight"])

    def test_history_replaces_same_batch_and_keeps_other_dates(self):
        old_entry = {"batch_date": "20260722", "generated_at": "old", "etfs": []}
        payload = {
            "batch_date": "20260723",
            "generated_at": "new",
            "summary": {},
            "consensus": {},
            "etfs": [],
        }
        merged = merge_history(payload, {"history": [old_entry]})
        replaced = merge_history(payload, merged)
        self.assertEqual(
            ["20260722", "20260723"],
            [item["batch_date"] for item in merged["history"]],
        )
        self.assertEqual(2, len(replaced["history"]))
        self.assertEqual("new", replaced["history"][-1]["generated_at"])

    def test_manager_name_parsing(self):
        self.assertEqual("第一金", manager_from_name("主動第一金優股息"))
        self.assertEqual("統一", manager_from_name("主動統一升級50"))


if __name__ == "__main__":
    unittest.main()
