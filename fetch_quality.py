"""
fetch_quality.py
每月自動抓取全市場 EPS（損益表）與負債比（資產負債表）
做品質篩選（近四季 EPS 合計 > 0 且 負債比 ≤ 70%）
並寫入 Google Sheets「品質篩選快取」分頁

執行環境：GitHub Actions（每月 11 日，財報每季更新，搭配月營收一起跑）
資料來源：
  - Finmind TaiwanStockFinancialStatements（損益表，取 EPS）
  - Finmind TaiwanStockBalanceSheet（資產負債表，取總資產/總負債）

設計重點：
  - 每檔股票需 2 次 API 呼叫（EPS + 負債比）
  - 不受 GAS 6 分鐘限制，可以慢慢跑完全部
  - 快取 90 天：90 天內已抓過的股票直接跳過
  - 402 額度用完時安全停止，已完成的部分不會遺失
"""

import requests
import json
import time
import os
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials

# ══════════ 設定區 ══════════
SHEET_NAME      = "品質篩選快取"
STOCK_DB_SHEET  = "股票資料庫"
CACHE_DAYS      = 90
REQUEST_DELAY   = 0.3

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

# ══════════ 取得快取中仍有效的代號（90 天內）══════════
def get_cached_codes(wb):
    try:
        sheet = wb.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        return set(), None

    all_values = sheet.get_all_values()
    if len(all_values) < 2:
        return set(), sheet

    headers   = all_values[0]
    code_idx  = headers.index("代號")     if "代號"     in headers else 0
    date_idx  = headers.index("快取日期") if "快取日期" in headers else 1

    cutoff = (datetime.now() - timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%d")

    valid_codes = set()
    for row in all_values[1:]:
        if len(row) > max(code_idx, date_idx):
            cache_date = row[date_idx]
            if cache_date >= cutoff:
                valid_codes.add(row[code_idx])

    return valid_codes, sheet

# ══════════ 抓近四季 EPS ══════════
def fetch_eps_4q(code, token, start_date, end_date):
    url = (
        "https://api.finmindtrade.com/api/v4/data"
        f"?dataset=TaiwanStockFinancialStatements"
        f"&data_id={code}"
        f"&start_date={start_date}"
        f"&end_date={end_date}"
        f"&token={token}"
    )
    try:
        res = requests.get(url, timeout=15)
        if res.status_code == 402:
            return "quota_exceeded", None
        if res.status_code != 200:
            return "error", None

        data = res.json()
        rows = data.get("data", [])
        if not rows:
            return "no_data", None

        eps_rows = [r for r in rows if r.get("type") in ("EPS", "BasicEPS") and r.get("value") is not None]
        if not eps_rows:
            return "no_data", None

        latest4 = eps_rows[-4:]
        total = round(sum(float(r["value"]) for r in latest4), 2)
        return "ok", total
    except Exception as e:
        print(f"  EPS 例外 {code}: {e}")
        return "error", None

# ══════════ 抓最新負債比 ══════════
def fetch_debt_ratio(code, token, start_date, end_date):
    url = (
        "https://api.finmindtrade.com/api/v4/data"
        f"?dataset=TaiwanStockBalanceSheet"
        f"&data_id={code}"
        f"&start_date={start_date}"
        f"&end_date={end_date}"
        f"&token={token}"
    )
    try:
        res = requests.get(url, timeout=15)
        if res.status_code == 402:
            return "quota_exceeded", None
        if res.status_code != 200:
            return "error", None

        data = res.json()
        rows = data.get("data", [])
        if not rows:
            return "no_data", None

        dates = sorted(set(r["date"] for r in rows), reverse=True)
        if not dates:
            return "no_data", None
        latest_date = dates[0]
        latest_rows = [r for r in rows if r["date"] == latest_date]

        total_assets = None
        total_liab   = None
        for r in latest_rows:
            t = r.get("type", "")
            if t in ("TotalAssets", "total_assets"):
                total_assets = float(r["value"])
            if t in ("TotalLiabilities", "total_liabilities"):
                total_liab = float(r["value"])

        if not total_assets or not total_liab or total_assets == 0:
            return "no_data", None

        ratio = round(total_liab / total_assets * 100, 1)
        return "ok", ratio
    except Exception as e:
        print(f"  負債比例外 {code}: {e}")
        return "error", None

# ══════════ 寫入快取（更新或新增）══════════
def upsert_cache_row(sheet, code, cache_date, result_json):
    # 簡化策略：直接 append，不檢查重複
    # （讀取時用最新一筆即可，定期清理舊資料）
    sheet.append_row([code, cache_date, result_json], value_input_option="USER_ENTERED")

# ══════════ 主程式 ══════════
def main():
    print("=" * 50)
    print("台股全市場品質篩選資料抓取開始")
    print("=" * 50)

    now = datetime.now()
    end_date   = now.strftime("%Y-%m-%d")
    start_date = (now - timedelta(days=400)).strftime("%Y-%m-%d")  # 抓近 13 個月確保有 4 季

    wb    = connect_sheets()
    token = get_finmind_token()

    all_codes = get_all_stock_codes(wb)
    if not all_codes:
        print("❌ 無股票代號清單，結束")
        return

    cached, sheet = get_cached_codes(wb)
    print(f"90 天內已有快取：{len(cached)} 檔，待抓取：{len(all_codes) - len(cached)} 檔")

    if sheet is None:
        sheet = wb.add_worksheet(title=SHEET_NAME, rows=20000, cols=3)
        sheet.append_row(["代號", "快取日期", "資料"])
        print(f"已建立「{SHEET_NAME}」分頁")

    cache_date = now.strftime("%Y-%m-%d")
    success, skipped, failed, quota_hit = 0, 0, 0, False

    for i, code in enumerate(all_codes):
        if code in cached:
            skipped += 1
            continue

        if quota_hit:
            failed += 1
            continue

        eps_status, eps_4q = fetch_eps_4q(code, token, start_date, end_date)
        if eps_status == "quota_exceeded":
            print(f"⚠️ Finmind 額度用完（EPS），停止後續抓取（已完成 {success} 檔）")
            quota_hit = True
            failed += 1
            continue
        time.sleep(REQUEST_DELAY)

        debt_status, debt_ratio = fetch_debt_ratio(code, token, start_date, end_date)
        if debt_status == "quota_exceeded":
            print(f"⚠️ Finmind 額度用完（負債比），停止後續抓取（已完成 {success} 檔）")
            quota_hit = True
            failed += 1
            continue
        time.sleep(REQUEST_DELAY)

        if eps_4q is None and debt_ratio is None:
            result = {
                "pass": True, "status": "no_data",
                "eps4Q": None, "debtRatio": None,
                "detail": "無財報資料", "failReason": ""
            }
        else:
            fail_reasons = []
            if eps_4q is not None and eps_4q <= 0:
                fail_reasons.append(f"近四季 EPS 合計 {eps_4q} ≤ 0（虧損）")
            if debt_ratio is not None and debt_ratio > 70:
                fail_reasons.append(f"負債比 {debt_ratio}% > 70%（高槓桿）")

            detail_parts = []
            detail_parts.append(f"近四季EPS {'+' if eps_4q and eps_4q > 0 else ''}{eps_4q}" if eps_4q is not None else "EPS無資料")
            detail_parts.append(f"負債比 {debt_ratio}%" if debt_ratio is not None else "負債比無資料")

            result = {
                "pass": len(fail_reasons) == 0,
                "status": "ok",
                "eps4Q": eps_4q,
                "debtRatio": debt_ratio,
                "detail": "　|　".join(detail_parts),
                "failReason": "；".join(fail_reasons)
            }

        upsert_cache_row(sheet, code, cache_date, json.dumps(result, ensure_ascii=False))
        success += 1

        if (i + 1) % 50 == 0:
            print(f"進度：{i+1}/{len(all_codes)}（成功 {success}，跳過 {skipped}，失敗 {failed}）")

    print("\n" + "=" * 50)
    print(f"完成！成功 {success} 檔，快取跳過 {skipped} 檔，失敗 {failed} 檔")
    print("=" * 50)

if __name__ == "__main__":
    main()
