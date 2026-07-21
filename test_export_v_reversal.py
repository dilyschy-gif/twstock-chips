# -*- coding: utf-8 -*-

import unittest

from export_sheet_to_data_json import build_col_map, row_to_stock


class ExportVReversalTest(unittest.TestCase):
    def test_v_sheet_row_is_exported_with_levels(self):
        headers = [
            "代號", "名稱", "市場", "現價", "V狀態", "V分數", "左臂跌幅", "RSI14",
            "黑K數", "紅K收盤位置", "上影占比", "量比", "相對大盤", "法人訊號",
            "左臂高點", "V底", "紅K中值", "V2確認價", "50%收復價", "61.8%收復價",
            "失效價", "轉折日", "badges", "chipsDetail", "備註",
        ]
        row = [
            "3037", "欣興", "上市", "825", "V1", "89", "22.19", "32.5",
            "3", "100", "0", "0.6", "5.8", "外資翻多、三法人買超",
            "960", "747", "800", "842", "853.5", "878.63", "747", "2026-07-21",
            "V1、連3黑、收近最高、無長上影、低量漲停例外", "投信買超573張",
            "第一根轉折紅K成立；先守紅K中值。",
        ]

        stock = row_to_stock(row, build_col_map(headers), "v_reversal")

        self.assertEqual("3037", stock["code"])
        self.assertEqual("V1", stock["v_state"])
        self.assertEqual("strong", stock["signal"])
        self.assertEqual(747, stock["v_bottom"])
        self.assertEqual(800, stock["trigger_mid"])
        self.assertEqual(842, stock["v2_confirm"])
        self.assertIn("低量漲停例外", stock["note"])


if __name__ == "__main__":
    unittest.main()
