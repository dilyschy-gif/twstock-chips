"""
fetch_volume_breakout.py
每日偵測「爆量創新高」訊號：
  條件 1（高檔）：當日收盤價為過去 60 個交易日內的新高
  條件 2（爆量）：當日成交量為過去 60 個交易日內最大量，
                  且超過 60 日均量

只對「今日籌碼面有三大法人買超」的候選股做偵測（與月營收/品質篩選同一批候選股），
避免對全市場 2000 檔都呼叫 Yahoo Finance，效率更好。

執行環境：GitHub Actions（每天台灣時間 15:05，緊接在品質篩選之後）
資料來源：Yahoo Finance Chart API（公開、無需 token、無次數限制）
寫入位置：Google Sheets「爆量訊號」分頁
"""

import requests
import json
import time
import os
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# ══════════ 設定區 ══════════
SHEET_NAME      = "爆量訊號"
CHIPS_SHEET     = "籌碼面資料"
MAX_CANDIDATES  = 400
LOOKBACK_DAYS   = 60     # 高檔與量能的回顧區間
REQUEST_DELAY   = 0.2

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
    gc = gspread.authorize(creds)

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise ValueError("找不到 GOOGLE_SHEET_ID 環境變數")
    return gc.open_by_key(sheet_id)

# ══════════ 取得候選股清單（與 fetch_revenue.py / fetch_quality.py 共用邏輯）══════════
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
        name_idx    = headers.index("名稱")
        market_idx  = headers.index("市場")
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

    sitc_buy, foreign_buy = [], []
    name_market = {}

    for row in all_values[1:]:
        if len(row) <= max(date_idx, code_idx, foreign_idx, sitc_idx):
            continue
        if row[date_idx] != latest_date:
            continue

        code = str(row[code_idx]).strip()
        if not code.isdigit() or len(code) != 4:
            continue

        name_market[code] = (row[name_idx], row[market_idx])

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
    return candidates, name_market

# ══════════ 抓單檔 K 線並判斷爆量創新高 ══════════
def check_volume_breakout(code, market):
    suffix = f"{code}.TW" if market != "上櫃" else f"{code}.TWO"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{suffix}?interval=1d&range=3mo"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            return None

        data = res.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        chart = result[0]
        quote = chart.get("indicators", {}).get("quote", [{}])[0]
        closes_raw  = quote.get("close", [])
        volumes_raw = quote.get("volume", [])

        # 過濾 None，保持 close/volume 對齊
        bars = []
        for c, v in zip(closes_raw, volumes_raw):
            if c is not None and v is not None:
                bars.append((c, v))

        if len(bars) < LOOKBACK_DAYS:
            return None  # 資料不足 60 天，無法判斷

        # 取最近 60 個交易日（含今日）
        recent = bars[-LOOKBACK_DAYS:]
        closes  = [b[0] for b in recent]
        volumes = [b[1] for b in recent]

        today_close = closes[-1]
        today_vol   = volumes[-1]

        # 條件 1：今日收盤為近 60 日新高
        is_price_high = today_close >= max(closes)

        # 條件 2：今日成交量為近 60 日最大量，且超過 60 日均量
        avg_vol = sum(volumes) / len(volumes)
        is_vol_max = today_vol >= max(volumes)
        is_vol_above_avg = today_vol > avg_vol

        breakout = is_price_high and is_vol_max and is_vol_above_avg

        return {
            "close": round(today_close, 2),
            "volume": int(today_vol),
            "avg_volume": int(avg_vol),
            "vol_ratio": round(today_vol / avg_vol, 2) if avg_vol > 0 else 0,
            "is_price_high": is_price_high,
            "is_vol_max": is_vol_max,
            "breakout": breakout
        }
    except Exception as e:
        print(f"  {code} 例外: {e}")
        return None

# ══════════ 寫入試算表（只記錄符合條件的訊號）══════════
def write_to_sheets(wb, rows):
    try:
        sheet = wb.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        sheet = wb.add_worksheet(title=SHEET_NAME, rows=2000, cols=8)
        headers = ["日期", "代號", "名稱", "收盤價", "成交量", "60日均量", "量比", "更新時間"]
        sheet.append_row(headers)
        print(f"已建立「{SHEET_NAME}」分頁")

    if rows:
        sheet.insert_rows(rows, row=2)
        print(f"寫入 {len(rows)} 檔爆量創新高訊號")
    else:
        print("今日無爆量創新高訊號")

    # 清理超過 30 天的舊資料
    prune_old_signals(sheet)

def prune_old_signals(sheet, keep_days=30):
    all_values = sheet.get_all_values()
    if len(all_values) < 2:
        return
    dates = sorted(set(row[0] for row in all_values[1:] if row[0]), reverse=True)
    if len(dates) <= keep_days:
        return
    cutoff = dates[keep_days - 1]
    rows_to_delete = [i for i, row in enumerate(all_values[1:], start=2) if row[0] and row[0] < cutoff]
    for row_num in reversed(rows_to_delete):
        sheet.delete_rows(row_num)
    if rows_to_delete:
        print(f"清理舊訊號：刪除 {len(rows_to_delete)} 列（{cutoff} 之前）")

# ══════════ 主程式 ══════════
def main():
    print("=" * 50)
    print("爆量創新高訊號偵測開始")
    print("=" * 50)

    wb = connect_sheets()
    candidates, name_market = get_candidate_codes(wb)

    if not candidates:
        print("今日無候選股，結束")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M")

    rows = []
    checked, hit = 0, 0

    for code in candidates:
        name, market = name_market.get(code, (code, "上市"))
        result = check_volume_breakout(code, market)
        checked += 1

        if result and result["breakout"]:
            hit += 1
            rows.append([
                today_str, code, name,
                result["close"], result["volume"], result["avg_volume"],
                result["vol_ratio"], now_str
            ])
            print(f"  🚀 {code}（{name}）爆量創新高！收盤 ${result['close']}，量比 {result['vol_ratio']}x")

        if checked % 50 == 0:
            print(f"進度：{checked}/{len(candidates)}（命中 {hit}）")

        time.sleep(REQUEST_DELAY)

    write_to_sheets(wb, rows)

    print("\n" + "=" * 50)
    print(f"完成！檢查 {checked} 檔，命中 {hit} 檔爆量創新高")
    print("=" * 50)

if __name__ == "__main__":
    main()
