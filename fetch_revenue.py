"""
fetch_revenue.py
每月自動抓取全市場月營收資料（逐檔查詢）
並寫入 Google Sheets「基本面資料」分頁

執行環境：GitHub Actions（每月 11 日自動執行，不受 GAS 6 分鐘限制）
資料來源：Finmind TaiwanStockMonthRevenue（免費版，每檔需 1 次 API 呼叫）

設計重點：
  - 逐檔查詢，但完全不受時間限制，可以慢慢跑完全部 2000 檔
  - 已有當月資料的股票自動跳過（讀取 Google Sheets 比對）
  - 每次呼叫間隔 0.3 秒，避免被 Finmind 限流
"""

import requests
import json
import time
import os
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# ══════════ 設定區 ══════════
SHEET_NAME      = "基本面資料"
STOCK_DB_SHEET  = "股票資料庫"   # 讀取全市場代號清單
HISTORY_MONTHS  = 13             # 保留幾個月歷史
REQUEST_DELAY   = 0.3            # 每次 API 呼叫間隔（秒）

# ══════════ 連接 Google Sheets ══════════
def connect_sheets():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise ValueError("找不到 GOOGLE_CREDENTIALS 環境變數")

    creds_dict = json.loads(creds_json)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc    = gspread.authorize(creds)

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise ValueError("找不到 GOOGLE_SHEET_ID 環境變數")

    return gc.open_by_key(sheet_id)

# ══════════ 取得 Finmind Token ══════════
def get_finmind_token():
    token = os.environ.get("FINMIND_TOKEN")
    if not token:
        raise ValueError("找不到 FINMIND_TOKEN 環境變數")
    return token

# ══════════ 取得全市場股票代號清單 ══════════
def get_all_stock_codes(wb):
    try:
        sheet = wb.worksheet(STOCK_DB_SHEET)
    except gspread.WorksheetNotFound:
        print(f"❌ 找不到「{STOCK_DB_SHEET}」分頁")
        return []

    all_values = sheet.get_all_values()
    if len(all_values) < 2:
        return []

    headers = all_values[0]
    try:
        code_idx = headers.index("股票代號")
    except ValueError:
        print("❌ 找不到「股票代號」欄位")
        return []

    codes = []
    for row in all_values[1:]:
        if len(row) > code_idx:
            code = str(row[code_idx]).strip()
            if code.isdigit() and len(code) == 4:
                codes.append(code)

    print(f"取得全市場股票代號：{len(codes)} 檔")
    return codes

# ══════════ 取得已有當月資料的代號（避免重複抓取）══════════
def get_existing_codes(wb, year_month):
    try:
        sheet = wb.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        return set(), None

    all_values = sheet.get_all_values()
    if len(all_values) < 2:
        return set(), sheet

    headers = all_values[0]
    ym_idx   = headers.index("年月")   if "年月" in headers else 0
    code_idx = headers.index("代號")   if "代號" in headers else 1

    existing = set()
    for row in all_values[1:]:
        if len(row) > max(ym_idx, code_idx) and row[ym_idx] == year_month:
            existing.add(row[code_idx])

    return existing, sheet

# ══════════ 單一股票月營收查詢 ══════════
def fetch_revenue_for_stock(code, token, start_date):
    url = (
        "https://api.finmindtrade.com/api/v4/data"
        f"?dataset=TaiwanStockMonthRevenue"
        f"&data_id={code}"
        f"&start_date={start_date}"
        f"&token={token}"
    )
    try:
        res = requests.get(url, timeout=15)
        if res.status_code == 402:
            return "quota_exceeded", []
        if res.status_code != 200:
            return "error", []

        data = res.json()
        rows = data.get("data", [])
        if not rows:
            return "no_data", []

        return "ok", rows
    except Exception as e:
        print(f"  例外 {code}: {e}")
        return "error", []

# ══════════ 計算 YoY / MoM 並組裝寫入列 ══════════
def build_rows(code, raw_rows, now_str):
    # 按日期排序（新到舊）
    sorted_rows = sorted(raw_rows, key=lambda r: r["date"], reverse=True)[:14]

    rows = []
    for i, r in enumerate(sorted_rows):
        ym         = r["date"][:7]
        this_month = float(r.get("revenue", 0) or 0)
        prev_month = float(sorted_rows[i+1]["revenue"])  if i+1  < len(sorted_rows) else 0
        last_year  = float(sorted_rows[i+12]["revenue"]) if i+12 < len(sorted_rows) else 0

        yoy = round((this_month - last_year)  / last_year  * 100, 2) if last_year  > 0 else ""
        mom = round((this_month - prev_month) / prev_month * 100, 2) if prev_month > 0 else ""

        rows.append([
            ym,
            code,
            r.get("stock_name", code),
            "上市",
            round(this_month / 1000),
            round(last_year  / 1000) if last_year  > 0 else "",
            yoy,
            round(prev_month / 1000) if prev_month > 0 else "",
            mom,
            now_str
        ])
    return rows

# ══════════ 寫入試算表 ══════════
def append_rows_to_sheet(sheet, rows):
    if not rows:
        return
    # gspread append_rows 一次寫入多列，效率較高
    sheet.append_rows(rows, value_input_option="USER_ENTERED")

# ══════════ 主程式 ══════════
def main():
    print("=" * 50)
    print("台股全市場月營收抓取開始")
    print("=" * 50)

    now = datetime.now()
    # 10 日後才有上月資料，10 日前抓前前月
    if now.day >= 10:
        target = datetime(now.year, now.month - 1, 1) if now.month > 1 else datetime(now.year - 1, 12, 1)
    else:
        m = now.month - 2
        y = now.year
        if m <= 0:
            m += 12
            y -= 1
        target = datetime(y, m, 1)

    year_month = target.strftime("%Y-%m")
    # 抓 14 個月資料確保有 YoY/MoM 比較基準
    start_year  = target.year - 1
    start_month = target.month - 1 if target.month > 1 else 12
    start_date  = f"{start_year}-{str(start_month).zfill(2)}-01"

    print(f"目標年月：{year_month}")
    print(f"查詢起始日：{start_date}")

    wb    = connect_sheets()
    token = get_finmind_token()

    all_codes = get_all_stock_codes(wb)
    if not all_codes:
        print("❌ 無股票代號清單，結束")
        return

    existing, sheet = get_existing_codes(wb, year_month)
    print(f"已有資料：{len(existing)} 檔，待抓取：{len(all_codes) - len(existing)} 檔")

    # 若分頁不存在，建立並寫表頭
    if sheet is None:
        sheet = wb.add_worksheet(title=SHEET_NAME, rows=20000, cols=10)
        headers = ["年月","代號","名稱","市場",
                   "當月營收(千元)","去年同月(千元)","YoY成長率(%)",
                   "上月營收(千元)","MoM成長率(%)","更新時間"]
        sheet.append_row(headers)
        print(f"已建立「{SHEET_NAME}」分頁")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    success, skipped, failed, quota_hit = 0, 0, 0, False

    for i, code in enumerate(all_codes):
        if code in existing:
            skipped += 1
            continue

        if quota_hit:
            failed += 1
            continue

        status, raw_rows = fetch_revenue_for_stock(code, token, start_date)

        if status == "quota_exceeded":
            print(f"⚠️ Finmind 額度已用完，停止後續抓取（已完成 {success} 檔）")
            quota_hit = True
            failed += 1
            continue

        if status == "ok":
            rows = build_rows(code, raw_rows, now_str)
            append_rows_to_sheet(sheet, rows)
            success += 1
        else:
            failed += 1

        # 進度顯示
        if (i + 1) % 50 == 0:
            print(f"進度：{i+1}/{len(all_codes)}（成功 {success}，跳過 {skipped}，失敗 {failed}）")

        time.sleep(REQUEST_DELAY)

    print("\n" + "=" * 50)
    print(f"完成！成功 {success} 檔，快取跳過 {skipped} 檔，失敗 {failed} 檔")
    print("=" * 50)

if __name__ == "__main__":
    main()
