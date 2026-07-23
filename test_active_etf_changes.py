import unittest

from fetch_active_etf_changes import build_report, merge_history


TITLE = ["日期", "標的代號", "標的名稱", "權重(%)", "持有數", "單位"]


class ActiveEtfChangesTest(unittest.TestCase):
    def test_detects_added_and_removed_constituents(self):
        rows = [
            ["20260722", "2330", "台積電", "10", "100000", "股"],
            ["20260722", "2303", "聯電", "2", "50000", "股"],
            ["20260722", "CASH_NTD", "現金", "", "1000000", "元"],
            ["20260723", "2330", "台積電", "11", "110000", "股"],
            ["20260723", "2454", "聯發科", "3", "25000", "股"],
            ["20260723", "CASH_NTD", "現金", "", "800000", "元"],
        ]

        report = build_report("00999A", "測試ETF", "https://example.com", TITLE, rows)

        self.assertEqual("20260723", report["data_date"])
        self.assertEqual("20260722", report["previous_date"])
        self.assertEqual(["2454"], [item["symbol"] for item in report["added"]])
        self.assertEqual(25, report["added"][0]["lots"])
        self.assertEqual(["2303"], [item["symbol"] for item in report["removed"]])
        self.assertEqual(50, report["removed"][0]["lots"])

    def test_foreign_stock_keeps_shares_without_taiwan_lot_conversion(self):
        rows = [
            ["20260721", "2330", "台積電", "1", "10000", "股"],
            ["20260722", "2330", "台積電", "1", "10000", "股"],
            ["20260722", "AMD US", "AMD", "6", "205000", "股"],
        ]

        report = build_report("00988A", "全球創新", "https://example.com", TITLE, rows)
        added = report["added"][0]

        self.assertEqual("AMD US", added["symbol"])
        self.assertEqual("US", added["market_code"])
        self.assertEqual("美國", added["market"])
        self.assertEqual(205000, added["shares"])
        self.assertIsNone(added["lots"])

    def test_requires_two_portfolio_dates(self):
        rows = [["20260723", "2330", "台積電", "10", "100000", "股"]]
        with self.assertRaises(ValueError):
            build_report("00999A", "測試ETF", "https://example.com", TITLE, rows)

    def test_history_replaces_same_batch_and_keeps_other_dates(self):
        old_entry = {"batch_date": "20260722", "generated_at": "old", "etfs": []}
        payload = {
            "generated_at": "new",
            "source": {},
            "etfs": [{"data_date": "20260723"}],
        }
        merged = merge_history(payload, {"history": [old_entry]})
        replaced = merge_history(payload, merged)

        self.assertEqual(["20260722", "20260723"], [item["batch_date"] for item in merged["history"]])
        self.assertEqual(2, len(replaced["history"]))
        self.assertEqual("new", replaced["history"][-1]["generated_at"])


if __name__ == "__main__":
    unittest.main()
