# -*- coding: utf-8 -*-
"""
fetch_chips.py — v2（TPEX 補上 Referer 標頭）
每日自動抓取 TWSE + TPEX 三大法人買賣超資料
並寫入 Google Sheets「籌碼面資料」分頁

執行環境：GitHub Actions（每天台灣時間 14:35 自動執行）
資料來源：TWSE T86 + TPEX（官方免費，不需要帳號）

v2 改動：
  fetch_tpex_chips() 補上 Referer、Accept、X-Requested-With 標頭。
  TPEX 的這個 AJAX 端點原本只帶 User-Agent 時會被判定為非瀏覽器請求，
  回應「無資料」（aaData 為空），實際上是被擋而非真的沒資料。
"""

import requests
import json
import time
import os
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials

# ══════════ 設定區 ══════════
SHEET_NAME      = "籌碼面資料"   # Google Sheets 分頁名稱
HISTORY_DAYS    = 20             # 保留幾個交易日的歷史
# 2026-07 修正：原本 10 天會讓「投信連買 15 日」的評分階永遠達不到（死程式碼），
# 改為 20 天，連買天數計算上限與評分表對齊。

# ══════════ 取得最近交易日 ══════════
def get_last_trading_date():
    """取得最近一個交易日（排除週六、週日）"""
    d = datetime.now()
    # 若現在是週六(5)往回1天，週日(6)往回2天
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d"), d.strftime("%Y-%m-%d")

# ══════════ 抓 TWSE 三大法人（上市）══════════
def fetch_twse_chips(date_str):
    """
    date_str: "20260615" 格式
    回傳 list of dict
    """
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={date_str}&selectType=ALLBUT0999&response=json"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.twse.com.tw/zh/trading/foreign/t86.html"
    }
    try:
        res = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        print(f"TWSE T86 回應碼: {res.status_code}")
        if res.status_code != 200:
            return []
        data = res.json()
        if data.get("stat") != "OK" or not data.get("data"):
            print(f"TWSE 無資料: {data.get('stat')}")
            return []

        date_label = data.get("date", date_str)
        # 轉換民國年到西元年
        if "/" in str(date_label):
            parts = str(date_label).split("/")
            if len(parts) == 3 and int(parts[0]) < 1000:
                date_label = f"{int(parts[0])+1911}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"

        results = []
        for row in data["data"]:
            code = str(row[0]).strip()
            if not code.isdigit() or len(code) != 4:
                continue
            def parse_num(v):
                try:
                    return int(str(v).replace(",", "").strip())
                except:
                    return 0
            results.append({
                "date":    date_label,
                "code":    code,
                "name":    str(row[1]).strip(),
                "market":  "上市",
                "foreign": parse_num(row[4]),   # 外資買賣超（股）
                "sitc":    parse_num(row[10]),   # 投信買賣超（股）
                "dealer":  parse_num(row[11]),   # 自營商買賣超（股）
                "total":   parse_num(row[12])    # 三法人合計
            })
        print(f"TWSE：取得 {len(results)} 檔")
        return results
    except Exception as e:
        print(f"TWSE 抓取失敗: {e}")
        return []

# ══════════ 抓 TPEX 三大法人（上櫃）══════════
def fetch_tpex_chips(date_str):
    """
    date_str: "20260615" 格式

    v2：補上 Referer / Accept / X-Requested-With 標頭。
    這支是網站內部的 AJAX 端點，原本只帶 User-Agent 時，
    伺服器會判定請求不是從網頁本身發出的，回應「無資料」
    （aaData 為空陣列），並非真的當天沒有交易資料。
    """
    # 轉換為民國年格式
    year  = int(date_str[:4]) - 1911
    mm    = date_str[4:6]
    dd    = date_str[6:8]
    tw_date = f"{year}/{mm}/{dd}"
    year_ad = date_str[:4]

    url = f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&se=EW&t=D&d={tw_date}&_={int(time.time()*1000)}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Referer": "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge.php?l=zh-tw",  # ← v2 新增
        "Accept": "application/json, text/javascript, */*; q=0.01",                                    # ← v2 新增
        "X-Requested-With": "XMLHttpRequest",                                                            # ← v2 新增
    }
    try:
        res = requests.get(url, headers=headers, timeout=30)
        print(f"TPEX 回應碼: {res.status_code}")
        if res.status_code != 200:
            return []

        text = res.text
        if text.strip().startswith("<") or "安全性考量" in text:
            print(f"TPEX 回應被安全機制擋下: {text[:150]}")
            return []

        data = res.json()
        if not data.get("aaData"):
            print("TPEX 無資料")
            return []

        date_label = f"{year_ad}-{mm}-{dd}"
        results = []
        for row in data["aaData"]:
            code = str(row[0]).strip()
            if not code.isdigit() or len(code) != 4:
                continue
            def parse_num(v):
                try:
                    return int(str(v).replace(",", "").strip())
                except:
                    return 0
            results.append({
                "date":    date_label,
                "code":    code,
                "name":    str(row[1]).strip(),
                "market":  "上櫃",
                "foreign": parse_num(row[4]),
                "sitc":    parse_num(row[10]),
                "dealer":  parse_num(row[11]),
                "total":   parse_num(row[12])
            })
        print(f"TPEX：取得 {len(results)} 檔")
        return results
    except Exception as e:
        print(f"TPEX 抓取失敗: {e}")
        return []

# ══════════ 連接 Google Sheets ══════════
def connect_sheets():
    """
    從環境變數讀取 Google Service Account 憑證
    憑證存放在 GitHub Secrets: GOOGLE_CREDENTIALS
    """
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

    # 從環境變數取得試算表 ID
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise ValueError("找不到 GOOGLE_SHEET_ID 環境變數")

    wb = gc.open_by_key(sheet_id)
    return wb

# ══════════ 寫入 Google Sheets ══════════
def write_to_sheets(wb, all_data, date_label):
    """把當日資料寫入試算表"""
    try:
        sheet = wb.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        # 分頁不存在就新建
        sheet = wb.add_worksheet(title=SHEET_NAME, rows=10000, cols=10)
        headers = ["日期","代號","名稱","市場",
                   "外資買賣超(張)","投信買賣超(張)","自營商買賣超(張)",
                   "三法人合計(張)","更新時間"]
        sheet.append_row(headers)
        print(f"已建立「{SHEET_NAME}」分頁")

    # 確認今日資料是否已存在
    existing = sheet.col_values(1)  # 第 1 欄（日期）
    if date_label in existing:
        print(f"{date_label} 資料已存在，跳過寫入")
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    for d in all_data:
        rows.append([
            date_label,
            d["code"],
            d["name"],
            d["market"],
            d["foreign"],
            d["sitc"],
            d["dealer"],
            d["total"],
            now_str
        ])

    if rows:
        # 在第 2 列前插入新資料（最新在最上方）
        # gspread 沒有直接 insertRows，用 insert_rows 方法
        sheet.insert_rows(rows, row=2)
        print(f"✅ 寫入 {len(rows)} 筆，日期 {date_label}")

    # 清理超過 HISTORY_DAYS 的舊資料
    prune_old_data(sheet)

# ══════════ 清理舊資料 ══════════
def prune_old_data(sheet):
    """保留最近 HISTORY_DAYS 個交易日的資料"""
    all_values = sheet.get_all_values()
    if len(all_values) < 2:
        return

    # 找出所有不重複的日期（跳過表頭）
    dates = sorted(set(
        row[0] for row in all_values[1:] if row[0]
    ), reverse=True)

    if len(dates) <= HISTORY_DAYS:
        return

    cutoff = dates[HISTORY_DAYS - 1]
    rows_to_delete = []
    for i, row in enumerate(all_values[1:], start=2):
        if row[0] and row[0] < cutoff:
            rows_to_delete.append(i)

    if not rows_to_delete:
        return

    # Batch contiguous row deletions to avoid Google Sheets write quota errors.
    ranges = []
    start = prev = rows_to_delete[0]
    for row_num in rows_to_delete[1:]:
        if row_num == prev + 1:
            prev = row_num
            continue
        ranges.append((start, prev))
        start = prev = row_num
    ranges.append((start, prev))

    for start, end in reversed(ranges):
        sheet.delete_rows(start, end)
        time.sleep(1)

    print(f"清理舊資料：刪除 {len(rows_to_delete)} 列（{cutoff} 之前）")

# ══════════ 主程式 ══════════
def main():
    print("=" * 50)
    print("台股三大法人資料抓取開始")
    print("=" * 50)

    # 取得最近交易日
    date_str, date_label = get_last_trading_date()
    print(f"目標日期：{date_label}（{date_str}）")

    # 抓資料（TWSE 和 TPEX 各試一次，失敗往前一天）
    all_data = []
    for days_back in range(6):
        d = datetime.strptime(date_str, "%Y%m%d") - timedelta(days=days_back)
        if d.weekday() >= 5:
            continue
        ds = d.strftime("%Y%m%d")
        dl = d.strftime("%Y-%m-%d")

        twse = fetch_twse_chips(ds)
        time.sleep(2)
        tpex = fetch_tpex_chips(ds)

        all_data = twse + tpex
        if all_data:
            date_label = dl
            print(f"成功取得 {dl} 資料，共 {len(all_data)} 檔")
            break
        else:
            print(f"{dl} 無資料，往前找...")
            time.sleep(3)

    if not all_data:
        print("❌ 近 6 個交易日均無資料，結束")
        return

    # 連接 Google Sheets 並寫入
    print("\n連接 Google Sheets...")
    wb = connect_sheets()
    write_to_sheets(wb, all_data, date_label)
    print("\n✅ 全部完成！")

if __name__ == "__main__":
    main()
