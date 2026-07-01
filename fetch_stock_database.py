# -*- coding: utf-8 -*-
"""
fetch_stock_database.py
建立/更新「股票資料庫」分頁（上市 + 上櫃公司清單）

背景：
  原本用 GAS（Google Apps Script）呼叫 TWSE OpenAPI 建立這份清單，
  但 TWSE 的安全防護（WAF）會擋掉來自 Google Cloud IP 的請求，
  回應「因為安全性考量，您所執行的頁面無法呈現」的 HTML 頁面。
  改用 GitHub Actions（不同 IP 範圍）執行本腳本，避開封鎖。

資料來源：
  上市：TWSE OpenAPI - https://openapi.twse.com.tw/v1/opendata/t187ap03_L
  上櫃：TPEX OpenAPI  - https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O

執行環境：GitHub Actions
建議排程：每週一次即可（公司清單不會每天變動），或改版當天手動觸發

必要環境變數：
- GOOGLE_SHEET_ID
- GOOGLE_CREDENTIALS  # service account JSON 字串
"""

import json
import os
import time

import gspread
import requests
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
SHEET_STOCK_DB = "股票資料庫"

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}

MAX_RETRIES = 3
RETRY_SLEEP_SECONDS = 2


def get_gspread_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS 環境變數未設定")
    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_sheet(gc):
    if not SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID 環境變數未設定")
    return gc.open_by_key(SHEET_ID)


def safe_text(v) -> str:
    return str(v).strip() if v is not None else ""


def fetch_json_with_retry(url: str, label: str):
    """抓取 JSON，若拿到 HTML（代表被 WAF 擋下）視為失敗並重試。"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            res = requests.get(url, headers=HEADERS, timeout=30)
            print(f"[{label}] 第 {attempt} 次嘗試，狀態碼: {res.status_code}")

            text = res.text.strip()
            if text.startswith("<"):
                # 被 WAF 擋下會回傳 HTML 錯誤頁，不是 JSON
                snippet = text[:150].replace("\n", " ")
                print(f"[{label}] 回應不是 JSON（可能被安全機制擋下): {snippet}")
                time.sleep(RETRY_SLEEP_SECONDS)
                continue

            if res.status_code != 200:
                print(f"[{label}] 回應碼異常: {res.status_code}")
                time.sleep(RETRY_SLEEP_SECONDS)
                continue

            data = res.json()
            print(f"[{label}] ✅ 成功取得 {len(data)} 筆")
            return data

        except requests.exceptions.RequestException as e:
            print(f"[{label}] 第 {attempt} 次嘗試發生例外: {e}")
            time.sleep(RETRY_SLEEP_SECONDS)
        except json.JSONDecodeError as e:
            print(f"[{label}] 第 {attempt} 次嘗試 JSON 解析失敗: {e}")
            time.sleep(RETRY_SLEEP_SECONDS)

    print(f"[{label}] ❌ 三次嘗試均失敗")
    return None


def fetch_twse_list():
    data = fetch_json_with_retry(TWSE_URL, "上市")
    if not data:
        return []

    rows = []
    for item in data:
        code = safe_text(item.get("公司代號"))
        if len(code) == 4 and code.isdigit():
            rows.append([
                "上市",
                code,
                safe_text(item.get("公司簡稱")),
                safe_text(item.get("產業別")),
            ])
    return rows


def fetch_tpex_list():
    data = fetch_json_with_retry(TPEX_URL, "上櫃")
    if not data:
        return []

    rows = []
    for item in data:
        code = safe_text(
            item.get("SecuritiesCompanyCode") or item.get("公司代號")
        )
        name = safe_text(
            item.get("CompanyAbbreviation") or item.get("公司簡稱")
        )
        industry = safe_text(
            item.get("SecuritiesIndustryCode") or item.get("產業別")
        )
        if len(code) == 4 and code.isdigit():
            rows.append(["上櫃", code, name, industry])
    return rows


def worksheet_or_create(sh, title: str, rows: int = 2000, cols: int = 5):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def write_stock_database(sh, twse_rows, tpex_rows):
    ws = worksheet_or_create(sh, SHEET_STOCK_DB)
    ws.clear()

    headers = ["市場別", "股票代號", "股票名稱", "產業別"]
    all_rows = [headers] + twse_rows + tpex_rows

    ws.update(range_name="A1", values=all_rows)
    ws.freeze(rows=1)

    print(f"\n✅ 已寫入「{SHEET_STOCK_DB}」分頁")
    print(f"   上市：{len(twse_rows)} 檔")
    print(f"   上櫃：{len(tpex_rows)} 檔")
    print(f"   合計：{len(twse_rows) + len(tpex_rows)} 檔")


def main():
    print("=" * 60)
    print("股票資料庫建立/更新開始（Python / GitHub Actions 版）")
    print("=" * 60)

    gc = get_gspread_client()
    sh = get_sheet(gc)

    print("\n抓取上市清單...")
    twse_rows = fetch_twse_list()

    print("\n抓取上櫃清單...")
    tpex_rows = fetch_tpex_list()

    # ── 健檢：任一邊完全空手，就不要覆寫既有資料，避免比現況更糟 ──
    if not twse_rows and not tpex_rows:
        print("\n❌ 上市、上櫃皆抓取失敗，保留現有「股票資料庫」內容，不覆寫")
        return

    if not twse_rows:
        print("\n⚠️ 上市清單抓取失敗，僅上櫃成功。為避免覆蓋成不完整清單，本次不寫入，請稍後重跑。")
        return

    if not tpex_rows:
        print("\n⚠️ 上櫃清單抓取失敗，僅上市成功。為避免覆蓋成不完整清單，本次不寫入，請稍後重跑。")
        return

    write_stock_database(sh, twse_rows, tpex_rows)

    print("\n" + "=" * 60)
    print("完成！")
    print("=" * 60)


if __name__ == "__main__":
    main()
