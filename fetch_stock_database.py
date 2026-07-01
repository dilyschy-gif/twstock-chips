# -*- coding: utf-8 -*-
"""
fetch_stock_database.py — v2
建立/更新「股票資料庫」分頁（上市 + 上櫃公司清單）

v2 改動：
  openapi.twse.com.tw 這個網址會被 TWSE 的安全防護（WAF）擋掉
  雲端資料中心的 IP（測試過 Google Cloud 和 GitHub Actions/Azure
  兩種不同來源都被擋，判斷是廣泛封鎖資料中心 IP，非針對特定廠商）。
  改用 mopsfin.twse.com.tw 這個不同子網域的 CSV 端點作為主要來源，
  原 openapi 端點保留作為次要備援（萬一哪天 mopsfin 也被納入封鎖範圍）。

資料來源：
  上市（主要）：mopsfin.twse.com.tw CSV
    https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv
  上市（備援）：TWSE OpenAPI JSON
    https://openapi.twse.com.tw/v1/opendata/t187ap03_L
  上櫃：TPEX OpenAPI
    https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O

執行環境：GitHub Actions
建議排程：每週一次即可（公司清單不會每天變動）

必要環境變數：
- GOOGLE_SHEET_ID
- GOOGLE_CREDENTIALS  # service account JSON 字串
"""

import csv
import io
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

# 上市：主要 CSV 端點 + 備援 JSON 端點
TWSE_CSV_URL = "https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv"
TWSE_JSON_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"

# 上櫃
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,application/json,*/*",
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


def is_blocked_response(text: str) -> bool:
    """判斷回應是不是被 WAF 擋下的 HTML 頁面，而不是真正的資料。"""
    stripped = text.strip()
    return stripped.startswith("<") or "安全性考量" in stripped


def fetch_with_retry(url: str, label: str, force_utf8: bool = False):
    """抓取原始文字內容，若被擋（回應 HTML）則重試，回傳 None 代表最終失敗。

    force_utf8: TWSE 的 CSV 端點會回傳帶 UTF-8 BOM 的內容，但 requests
                有時會依 Content-Type 猜成其他編碼，導致中文變亂碼。
                設為 True 時強制用 utf-8-sig 解碼（sig 會自動處理 BOM）。
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            res = requests.get(url, headers=HEADERS, timeout=30)
            print(f"[{label}] 第 {attempt} 次嘗試，狀態碼: {res.status_code}")

            if res.status_code != 200:
                print(f"[{label}] 回應碼異常: {res.status_code}")
                time.sleep(RETRY_SLEEP_SECONDS)
                continue

            if force_utf8:
                text = res.content.decode("utf-8-sig", errors="replace")
            else:
                text = res.text

            if is_blocked_response(text):
                snippet = text.strip()[:150].replace("\n", " ")
                print(f"[{label}] 回應被安全機制擋下: {snippet}")
                time.sleep(RETRY_SLEEP_SECONDS)
                continue

            print(f"[{label}] ✅ 成功取得回應（{len(text)} 字元）")
            return text

        except requests.exceptions.RequestException as e:
            print(f"[{label}] 第 {attempt} 次嘗試發生例外: {e}")
            time.sleep(RETRY_SLEEP_SECONDS)

    print(f"[{label}] ❌ 三次嘗試均失敗")
    return None


def parse_twse_csv(text: str):
    """解析 mopsfin CSV 格式，回傳 [市場, 代號, 名稱, 產業別] 列表。"""
    reader = csv.reader(io.StringIO(text))
    rows_raw = list(reader)
    if not rows_raw:
        return []

    header = rows_raw[0]
    try:
        code_idx = header.index("公司代號")
        name_idx = header.index("公司簡稱")
        industry_idx = header.index("產業別")
    except ValueError as e:
        print(f"[上市CSV] 表頭解析失敗: {e}，表頭內容：{header}")
        return []

    rows = []
    for r in rows_raw[1:]:
        if len(r) <= max(code_idx, name_idx, industry_idx):
            continue
        code = safe_text(r[code_idx])
        if len(code) == 4 and code.isdigit():
            rows.append(["上市", code, safe_text(r[name_idx]), safe_text(r[industry_idx])])
    return rows


def parse_twse_json(text: str):
    """解析 openapi JSON 格式（備援用），回傳同樣格式。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[上市JSON備援] JSON 解析失敗: {e}")
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


def fetch_twse_list():
    """先試 CSV 端點，失敗再試 JSON 端點。"""
    text = fetch_with_retry(TWSE_CSV_URL, "上市-CSV", force_utf8=True)
    if text:
        rows = parse_twse_csv(text)
        if rows:
            return rows
        print("[上市-CSV] 解析成功但沒有取得任何資料列，改嘗試備援端點")

    print("\n[上市] CSV 端點失敗，改嘗試備援 JSON 端點...")
    text = fetch_with_retry(TWSE_JSON_URL, "上市-JSON備援")
    if text:
        return parse_twse_json(text)

    return []


def fetch_tpex_list():
    text = fetch_with_retry(TPEX_URL, "上櫃")
    if not text:
        return []

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[上櫃] JSON 解析失敗: {e}")
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
    print("股票資料庫建立/更新開始（Python / GitHub Actions 版 v2）")
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
        print("\n⚠️ 上市清單抓取失敗（CSV 與 JSON 備援皆失敗），僅上櫃成功。"
              "為避免覆蓋成不完整清單，本次不寫入，請稍後重跑。")
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
