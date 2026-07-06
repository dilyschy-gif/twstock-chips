# -*- coding: utf-8 -*-
"""
fetch_chips.py — v3（新增歷史回補模式）
每日自動抓取 TWSE + TPEX 三大法人買賣超資料
並寫入 Google Sheets「籌碼面資料」分頁
執行環境：GitHub Actions（cron 35 8 * * 1-5 UTC＝每個交易日台灣時間 16:35 自動執行）
資料來源：TWSE T86 + TPEX（官方免費，不需要帳號）
排程時間說明（勿改早於 16:05）：
  三大法人資料收盤後分批公布——投信約 15:00、外資與自營商約 16:00 完整揭曉。
  16:35 執行可確保抓到完整資料，並趕在 17:00 主掃描之前寫入完成。
v2 改動：
  fetch_tpex_chips() 補上 Referer、Accept、X-Requested-With 標頭。
v3 改動：
  新增「--backfill N」歷史回補模式：
    python fetch_chips.py --backfill 20
  會往回抓 N 個「有資料的交易日」（自動跳過週末與國定假日），
  已存在的日期自動跳過，可安全重複執行。
  背景：v2 修正欄位對照後，舊歷史（錯誤欄位時期）被清除，
  連買日數歸零導致右腳醞釀名單空白。跑一次回補即可還原。
  每個日期之間 sleep 4 秒，避免被 TWSE/TPEX 限流。
"""
import requests
import json
import time
import os
import sys
from datetime import datetime, timedelta, timezone
import gspread
from google.oauth2.service_account import Credentials
TAIPEI_TZ = timezone(timedelta(hours=8))
# ══════════ 設定區 ══════════
SHEET_NAME      = "籌碼面資料"   # Google Sheets 分頁名稱
HISTORY_DAYS    = 20             # 保留幾個交易日的歷史
# 2026-07 修正：原本 10 天會讓「投信連買 15 日」的評分階永遠達不到（死程式碼），
# 改為 20 天，連買天數計算上限與評分表對齊。
# ══════════ 取得最近交易日 ══════════
def get_last_trading_date():
    """取得最近一個交易日（排除週六、週日）。
    2026-07 修正：明確使用台北時區。GitHub Actions 主機在 UTC，
    若排程改到台北早上（UTC 仍是前一天），datetime.now() 會抓錯日期。
    """
    d = datetime.now(TAIPEI_TZ)
    # 若現在是週六(5)往回1天，週日(6)往回2天
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d"), d.strftime("%Y-%m-%d")
# ══════════ 共用工具 ══════════
def parse_num(v):
    """把 '1,234' 這類字串轉成整數，失敗回傳 0。"""
    try:
        return int(str(v).replace(",", "").strip())
    except Exception:
        return 0
def to_lots(shares):
    """股 → 張（1 張 = 1000 股），四捨五入並保留正負號。
    2026-07 修正：TWSE/TPEX API 回傳單位是「股」，
    但下游備註與顯示都寫「張」，統一在源頭轉換。
    """
    return int(round(shares / 1000))
def sanity_check(results, market_label):
    """自我驗算：外資+投信+自營商 應約等於 三大法人合計。
    容差 3 張（涵蓋零股進位誤差）。若大量不合，代表 API 欄位順序改了，
    立刻在 log 大聲警告，避免再發生「合計欄抓錯位置」而無人發現的狀況。
    """
    mismatch = 0
    sample = None
    for r in results:
        diff = abs((r["foreign"] + r["sitc"] + r["dealer"]) - r["total"])
        if diff > 3:
            mismatch += 1
            if sample is None:
                sample = r
    if mismatch:
        pct = mismatch * 100 // max(len(results), 1)
        print(f"⚠️⚠️ [{market_label}] 驗算不合 {mismatch} 檔（{pct}%）！"
              f"API 欄位順序可能已變動，請立即檢查。"
              f"樣本：{sample['code']} 外資{sample['foreign']}+投信{sample['sitc']}"
              f"+自營{sample['dealer']} ≠ 合計{sample['total']}")
    else:
        print(f"[{market_label}] 驗算通過：外資+投信+自營商 ≈ 合計（{len(results)} 檔）")
# ══════════ 抓 TWSE 三大法人（上市）══════════
def fetch_twse_chips(date_str):
    """
    date_str: "20260615" 格式
    回傳 list of dict（單位：張）
    T86 欄位對照（selectType=ALLBUT0999，共 19 欄）：
      [0]證券代號 [1]證券名稱
      [2-4]  外陸資 買進/賣出/買賣超（不含外資自營商）
      [5-7]  外資自營商 買進/賣出/買賣超
      [8-10] 投信 買進/賣出/買賣超
      [11]   自營商買賣超（合計）
      [12-14]自營商(自行買賣) 買進/賣出/買賣超
      [15-17]自營商(避險) 買進/賣出/買賣超
      [18]   三大法人買賣超股數 ← 真正的合計
    2026-07 修正：原版把 [12]（自營商「買進」自行買賣）誤當成三法人合計，
    導致合計恆為非負數、「法人買超」加分形同虛設。現改抓 [18]，
    外資改為 [4]+[7]（含外資自營商），使 外資+投信+自營商 = 合計 可驗算。
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
            if len(row) < 19:
                continue  # 欄位數不足，跳過並依靠 sanity_check 察覺異常
            results.append({
                "date":    date_label,
                "code":    code,
                "name":    str(row[1]).strip(),
                "market":  "上市",
                "foreign": to_lots(parse_num(row[4]) + parse_num(row[7])),  # 外陸資+外資自營商（張）
                "sitc":    to_lots(parse_num(row[10])),                      # 投信買賣超（張）
                "dealer":  to_lots(parse_num(row[11])),                      # 自營商買賣超合計（張）
                "total":   to_lots(parse_num(row[18]))                       # 三大法人買賣超（張）
            })
        print(f"TWSE：取得 {len(results)} 檔")
        sanity_check(results, "TWSE")
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
        "Referer": "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge.php?l=zh-tw",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
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
            if len(row) < 24:
                continue  # 欄位數不足，跳過並依靠 sanity_check 察覺異常
            # TPEX 3itrade_hedge 欄位對照（買/賣/淨 三欄一組，共 24 欄）：
            #   [0]代號 [1]名稱
            #   [2-4]  外資及陸資(不含外資自營商) 買/賣/淨
            #   [5-7]  外資自營商 買/賣/淨
            #   [8-10] 外資及陸資合計 買/賣/淨
            #   [11-13]投信 買/賣/淨
            #   [14-16]自營商(自行買賣) 買/賣/淨
            #   [17-19]自營商(避險) 買/賣/淨
            #   [20-22]自營商合計 買/賣/淨
            #   [23]   三大法人買賣超合計
            # 2026-07 修正：原版沿用 TWSE 的 index（4/10/11/12），
            # 在 TPEX 上等於把「外資合計淨額」當投信、「投信買進」當自營商、
            # 「投信賣出」當三法人合計——上櫃股的投信連買紀錄因此全是錯的。
            results.append({
                "date":    date_label,
                "code":    code,
                "name":    str(row[1]).strip(),
                "market":  "上櫃",
                "foreign": to_lots(parse_num(row[10])),   # 外資及陸資合計買賣超（張）
                "sitc":    to_lots(parse_num(row[13])),   # 投信買賣超（張）
                "dealer":  to_lots(parse_num(row[22])),   # 自營商合計買賣超（張）
                "total":   to_lots(parse_num(row[23]))    # 三大法人合計（張）
            })
        print(f"TPEX：取得 {len(results)} 檔")
        sanity_check(results, "TPEX")
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
def write_to_sheets(wb, all_data, date_label, prune=True):
    """把當日資料寫入試算表。
    v3：新增 prune 參數——回補模式下逐日寫入時不清理，
    全部寫完後再統一 prune，減少 API 呼叫次數。
    """
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
    if prune:
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
# ══════════ 歷史回補模式（v3 新增）══════════
def backfill_main(target_days):
    """往回抓 target_days 個「有資料的交易日」寫入試算表。
    - 已存在的日期由 write_to_sheets 的重複檢查自動跳過（可安全重跑）
    - 國定假日 API 會回無資料，自動略過、不計入天數
    - 從最舊往最新寫入，讓「最新在最上方」的排列習慣保持一致
    - 最多往回掃 target_days*2+10 個日曆日，避免無限迴圈
    用法：python fetch_chips.py --backfill 20
    """
    print("=" * 50)
    print(f"歷史回補模式：目標 {target_days} 個交易日")
    print("=" * 50)
    today = datetime.now(TAIPEI_TZ)
    collected = []   # [(date_label, all_data)]，先收集再由舊到新寫入
    d = today
    scanned = 0
    max_scan = target_days * 2 + 10
    while len(collected) < target_days and scanned < max_scan:
        if d.weekday() < 5:  # 跳過週末
            ds = d.strftime("%Y%m%d")
            dl = d.strftime("%Y-%m-%d")
            print(f"\n--- 抓取 {dl} ---")
            twse = fetch_twse_chips(ds)
            time.sleep(2)
            tpex = fetch_tpex_chips(ds)
            day_data = twse + tpex
            if day_data:
                collected.append((dl, day_data))
                print(f"{dl}：{len(day_data)} 檔（進度 {len(collected)}/{target_days}）")
            else:
                print(f"{dl}：無資料（假日或未公布），略過")
            time.sleep(4)  # 日期之間降速，避免被限流
        d -= timedelta(days=1)
        scanned += 1
    if not collected:
        print("❌ 未取得任何資料，結束")
        return
    print(f"\n共取得 {len(collected)} 個交易日，開始寫入 Google Sheets...")
    wb = connect_sheets()
    # 由舊到新寫入（insert_rows 在第 2 列，最後寫的日期會在最上方）
    for dl, day_data in reversed(collected):
        write_to_sheets(wb, day_data, dl, prune=False)
        time.sleep(2)
    # 全部寫完後統一清理一次
    sheet = wb.worksheet(SHEET_NAME)
    prune_old_data(sheet)
    print("\n✅ 回補完成！下次 17:00 主掃描後，連買日數即恢復正常。")
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
    if len(sys.argv) >= 2 and sys.argv[1] == "--backfill":
        days = int(sys.argv[2]) if len(sys.argv) >= 3 else HISTORY_DAYS
        backfill_main(days)
    else:
        main()
