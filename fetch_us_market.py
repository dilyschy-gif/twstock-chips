"""
fetch_us_market.py
每日自動抓取追蹤美股清單的股價、漲跌幅、MA5/MA20、RSI
並寫入 Google Sheets「美股連動」分頁

執行環境：GitHub Actions（每天台灣時間 06:30，美股收盤後）
資料來源：Yahoo Finance Chart API（公開、無需 token）
"""

import requests
import json
import os
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# ══════════ 設定區 ══════════
SHEET_NAME = "美股連動"

US_WATCHLIST = [
    {"ticker": "NVDA", "name": "輝達",       "industries": "電腦及週邊設備業/其他電子業/電子通路業/AI 伺服器/散熱/電源/PCB/伺服器"},
    {"ticker": "AMD",  "name": "超微",       "industries": "半導體業/IC 設計"},
    {"ticker": "TSM",  "name": "台積電 ADR", "industries": "半導體業/晶圓代工"},
    {"ticker": "AVGO", "name": "博通",       "industries": "通信網路業/電子零組件業/網通/交換器/光纖"},
    {"ticker": "MSFT", "name": "微軟",       "industries": "軟體工業/資訊服務業/雲端/軟體/資安"},
    {"ticker": "AAPL", "name": "蘋果",       "industries": "光電業/電子零組件業/手機/消費電子/組裝/鏡頭/面板"},
    {"ticker": "TSLA", "name": "特斯拉",     "industries": "電機機械/汽車工業/電動車/電池/充電樁/車用電子/馬達"},
    {"ticker": "SPCX", "name": "SpaceX",     "industries": "衛星通訊/低軌衛星/航太", "is_new": True},
]

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

# ══════════ 計算 RSI ══════════
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

# ══════════ 抓單一美股資料 ══════════
def fetch_stock_data(ticker, is_new=False):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=3mo"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=20)
        if res.status_code != 200:
            print(f"  {ticker} 回應碼: {res.status_code}")
            return None

        data = res.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        chart = result[0]
        quote = chart.get("indicators", {}).get("quote", [{}])[0]
        closes_raw = quote.get("close", [])
        closes = [c for c in closes_raw if c is not None]

        min_bars = 1 if is_new else 20
        if len(closes) < min_bars:
            print(f"  {ticker} 資料不足（{len(closes)} 根，需要 {min_bars} 根）")
            return None

        close = closes[-1]
        prev_close = closes[-2] if len(closes) >= 2 else close
        change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0

        last5  = closes[-min(5, len(closes)):]
        last20 = closes[-min(20, len(closes)):]
        ma5  = round(sum(last5) / len(last5), 2)
        ma20 = round(sum(last20) / len(last20), 2)

        rsi = calc_rsi(closes) if len(closes) >= 15 else None
        bar_count = len(closes)

        return {
            "close": close, "prev_close": prev_close, "change_pct": change_pct,
            "ma5": ma5, "ma20": ma20, "rsi": rsi, "bar_count": bar_count
        }
    except Exception as e:
        print(f"  {ticker} 例外: {e}")
        return None

# ══════════ 判斷趨勢 ══════════
def calc_trend(close, ma5, ma20):
    if close > ma5 > ma20:
        return "多頭排列"
    elif close < ma5 < ma20:
        return "空頭排列"
    elif close > ma20:
        return "震盪偏多"
    else:
        return "震盪偏空"

# ══════════ 判斷訊號 ══════════
def calc_signal(change_pct, trend, rsi):
    if rsi is not None and rsi >= 80:
        return "⚠️ 超買"
    if rsi is not None and rsi <= 20:
        return "⚠️ 超賣"
    if change_pct >= 3 and trend in ("多頭排列", "震盪偏多"):
        return "🚀 強勢"
    if change_pct <= -3 and trend in ("空頭排列", "震盪偏空"):
        return "📉 弱勢"
    return "➡️ 中性"

# ══════════ 寫入試算表 ══════════
def write_to_sheets(wb, rows):
    try:
        sheet = wb.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        sheet = wb.add_worksheet(title=SHEET_NAME, rows=1000, cols=12)
        headers = ["更新時間", "代號", "名稱", "收盤價", "漲跌幅(%)", "前收",
                   "MA5", "MA20", "趨勢", "RSI", "訊號", "對應台股產業"]
        sheet.append_row(headers)
        print(f"已建立「{SHEET_NAME}」分頁")

    # 清空舊資料（保留表頭），整批覆寫最新資料
    last_row = sheet.row_count
    if sheet.acell("A2").value:
        sheet.batch_clear([f"A2:L{max(last_row, 100)}"])

    sheet.update("A2", rows, value_input_option="USER_ENTERED")
    print(f"✅ 寫入 {len(rows)} 檔美股資料")

# ══════════ 主程式 ══════════
def main():
    print("=" * 50)
    print("美股連動資料抓取開始")
    print("=" * 50)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []

    for item in US_WATCHLIST:
        print(f"抓取 {item['ticker']}（{item['name']}）...")
        data = fetch_stock_data(item["ticker"], item.get("is_new", False))

        if data is None:
            print(f"  ❌ 無法取得資料，跳過")
            continue

        trend = calc_trend(data["close"], data["ma5"], data["ma20"])
        signal = calc_signal(data["change_pct"], trend, data["rsi"])

        is_new = item.get("is_new", False)
        rsi_display = f"{data['rsi']}" if data["rsi"] is not None else f"建立中({data['bar_count']}日)"
        trend_display = "建立中" if (is_new and data["bar_count"] < 5) else trend

        rows.append([
            now_str, item["ticker"], item["name"],
            round(data["close"], 2), data["change_pct"], round(data["prev_close"], 2),
            data["ma5"], data["ma20"], trend_display,
            rsi_display, signal, item["industries"]
        ])
        print(f"  ✅ {item['ticker']}: ${data['close']} ({data['change_pct']:+.2f}%)")

    if not rows:
        print("❌ 無任何美股資料，結束")
        return

    wb = connect_sheets()
    write_to_sheets(wb, rows)

    print("\n" + "=" * 50)
    print(f"完成！共更新 {len(rows)} 檔")
    print("=" * 50)

if __name__ == "__main__":
    main()
