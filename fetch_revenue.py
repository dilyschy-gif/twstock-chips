"""
fetch_revenue.py — v2 候選股版
每日自動抓取「籌碼面有三大法人買超」股票的月營收
並寫入 Google Sheets「基本面資料」分頁
"""

import requests
import json
import time
import os
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

SHEET_NAME      = "基本面資料"
CHIPS_SHEET     = "籌碼面資料"
HISTORY_MONTHS  = 13
REQUEST_DELAY   = 0.3
MAX_CANDIDATES  = 400

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
    gc = gspread.authorize(creds)

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise ValueError("找不到 GOOGLE_SHEET_ID 環境變數")
    return gc.open_by_key(sheet_id)

def get_finmind_token():
    token = os.environ.get("FINMIND_TOKEN")
    if not token:
        raise ValueError("找不到 FINMIND_TOKEN 環境變數")
    return token

def get_candidate_codes(wb):
    try:
        sheet = wb.worksheet(CHIPS_SHEET)
    except gspread.WorksheetNotFound:
        print(f"找不到「{CHIPS_SHEET}」分頁")
        return []

    all_values = sheet.get_all_values()
    if len(all_values) < 2:
        return []

    headers = all_values[0]
    try:
        date_idx    = headers.index("日期")
        code_idx    = headers.index("代號")
        foreign_idx = headers.index("外資買賣超(張)")
        sitc_idx    = headers.index("投信買賣超(張)")
    except ValueError as e:
        print(f"籌碼面資料欄位缺失: {e}")
        return []

    dates = [row[date_idx] for row in all_values[1:] if row[date_idx]]
    if not dates:
        return []
    latest_date = max(dates)
    print(f"籌碼面最新日期：{latest_date}")

    sitc_buy    = []
    foreign_buy = []

    for row in all_values[1:]:
        if len(row) <= max(date_idx, code_idx, foreign_idx, sitc_idx):
            continue
        if row[date_idx] != latest_date:
            continue

        code = str(row[code_idx]).strip()
        if not code.isdigit() or len(code) != 4:
            continue

        try:
            sitc_val    = float(str(row[sitc_idx]).replace(",", "") or 0)
            foreign_val = float(str(row[foreign_idx]).replace(",", "") or 0)
        except ValueError:
            continue

        if sitc_val > 0:
            sitc_buy.append((code, sitc_val))
        elif foreign_val > 0:
            foreign_buy.append((code, foreign_val))

    sitc_buy.sort(key=lambda x: x[1], reverse=True)
    foreign_buy.sort(key=lambda x: x[1], reverse=True)

    candidates = [c[0] for c in sitc_buy] + [c[0] for c in foreign_buy]
    candidates = candidates[:MAX_CANDIDATES]

    print(f"候選股：投信買超 {len(sitc_buy)} 檔起，外資補位，"
          f"共選 {len(candidates)} 檔（上限 {MAX_CANDIDATES}）")
    return candidates

def get_existing_codes(wb, year_month):
    try:
        sheet = wb.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        return set(), None

    all_values = sheet.get_all_values()
    if len(all_values) < 2:
        return set(), sheet

    headers  = all_values[0]
    ym_idx   = headers.index("年月") if "年月" in headers else 0
    code_idx = headers.index("代號") if "代號" in headers else 1

    existing = set()
    for row in all_values[1:]:
        if len(row) > max(ym_idx, code_idx) and row[ym_idx] == year_month:
            existing.add(row[code_idx])

    return existing, sheet

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

def build_rows(code, raw_rows, now_str):
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
            ym, code, r.get("stock_name", code), "上市",
            round(this_month / 1000),
            round(last_year  / 1000) if last_year  > 0 else "",
            yoy,
            round(prev_month / 1000) if prev_month > 0 else "",
            mom, now_str
        ])
    return rows

def append_rows_to_sheet(sheet, rows):
    if not rows:
        return
    sheet.append_rows(rows, value_input_option="USER_ENTERED")

def main():
    print("=" * 50)
    print("候選股月營收抓取開始")
    print("=" * 50)

    now = datetime.now()
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
    start_year  = target.year - 1
    start_month = target.month - 1 if target.month > 1 else 12
    start_date  = f"{start_year}-{str(start_month).zfill(2)}-01"

    print(f"目標年月：{year_month}")

    wb    = connect_sheets()
    token = get_finmind_token()

    candidates = get_candidate_codes(wb)
    if not candidates:
        print("今日無候選股（無三大法人買超紀錄），結束")
        return

    existing, sheet = get_existing_codes(wb, year_month)
    to_fetch = [c for c in candidates if c not in existing]
    print(f"候選股 {len(candidates)} 檔，已有資料 {len(candidates) - len(to_fetch)} 檔，"
          f"待抓取 {len(to_fetch)} 檔")

    if sheet is None:
        sheet = wb.add_worksheet(title=SHEET_NAME, rows=20000, cols=10)
        headers = ["年月","代號","名稱","市場",
                   "當月營收(千元)","去年同月(千元)","YoY成長率(%)",
                   "上月營收(千元)","MoM成長率(%)","更新時間"]
        sheet.append_row(headers)
        print(f"已建立「{SHEET_NAME}」分頁")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    success, failed, quota_hit = 0, 0, False

    for i, code in enumerate(to_fetch):
        if quota_hit:
            failed += 1
            continue

        status, raw_rows = fetch_revenue_for_stock(code, token, start_date)

        if status == "quota_exceeded":
            print(f"⚠️ Finmind 額度用完，停止後續抓取（已完成 {success} 檔）")
            quota_hit = True
            failed += 1
            continue

        if status == "ok":
            rows = build_rows(code, raw_rows, now_str)
            append_rows_to_sheet(sheet, rows)
            success += 1
        else:
            failed += 1

        if (i + 1) % 50 == 0:
            print(f"進度：{i+1}/{len(to_fetch)}（成功 {success}，失敗 {failed}）")

        time.sleep(REQUEST_DELAY)

    print("\n" + "=" * 50)
    print(f"完成！候選股 {len(candidates)} 檔，新抓 {success} 檔，"
          f"快取跳過 {len(candidates) - len(to_fetch)} 檔，失敗 {failed} 檔")
    print("=" * 50)

if __name__ == "__main__":
    main()
