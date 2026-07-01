# -*- coding: utf-8 -*-
"""
diag_tpex_openapi.py
診斷用腳本：測試 TPEx 官方 OpenAPI 的三大法人端點是否可用，
並印出實際欄位名稱，方便確認要怎麼寫正式的解析程式碼。

用法：直接在 GitHub Actions 或本機執行，不需要 Google Sheets 憑證。
"""

import json
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"


def main():
    print("=" * 60)
    print("測試 TPEx OpenAPI 三大法人端點")
    print(f"URL: {URL}")
    print("=" * 60)

    try:
        res = requests.get(URL, headers=HEADERS, timeout=30)
        print(f"狀態碼: {res.status_code}")

        text = res.text
        if text.strip().startswith("<") or "安全性考量" in text:
            print("❌ 被安全機制擋下（回應是 HTML 而非 JSON）")
            print(f"回應前 300 字: {text[:300]}")
            return

        data = res.json()
        print(f"✅ 成功取得 JSON，共 {len(data)} 筆資料")

        if not data:
            print("⚠️ 資料是空陣列，端點存在但今日無資料（可能非交易日或尚未更新）")
            return

        first = data[0]
        print("\n第一筆資料的所有欄位名稱：")
        for key in first.keys():
            print(f"  - {key}: {first[key]}")

        print("\n完整第一筆資料（JSON）：")
        print(json.dumps(first, ensure_ascii=False, indent=2))

    except requests.exceptions.RequestException as e:
        print(f"❌ 連線例外: {e}")
    except json.JSONDecodeError as e:
        print(f"❌ JSON 解析失敗: {e}")
        print(f"原始回應前 300 字: {res.text[:300]}")


if __name__ == "__main__":
    main()
