"""
monthly_revenue_fetch.py
抓取台灣證交所(TWSE)與證券櫃檯買賣中心(TPEx)官方月營收公開資料，
過濾出觀察名單股票，並寫入 Google Sheets「月營收」分頁。

寫入規格比照 fetch_us_market.py：
同一份 Service Account 認證、同一支試算表，
新增一個獨立分頁，不影響「美股連動」「籌碼面資料」等既有分頁。

執行環境：GitHub Actions（每月1-20日，台灣時間早上9點）
資料來源（官方OpenAPI，免費、無流量限制，不依賴Finmind）：
- 上市公司每月營業收入彙總表: https://mopsfin.twse.com.tw/opendata/t187ap05_L.csv
- 上櫃公司每月營業收入彙總表: https://mopsfin.twse.com.tw/opendata/t187ap05_O.csv

更新規則：
公司須於每月10日前申報上月營收，證交所/櫃買中心通常在每月10-17日左右
更新此彙總表。這份CSV每次都是「整個市場最新一期」的快照，不是歷史序列，
所以寫入時會比對Sheet既有的(代號, 資料年月)組合，只附加真正新的月份資料，
避免每天重複跑同一個月的舊資料造成洗版。
"""

import io
import json
import os
from datetime import datetime

import gspread
import pandas as pd
import requests
from google.oauth2.service_account import Credentials

# ══════════ 設定區 ══════════
SHEET_NAME = "月營收"

WATCHLIST = {
    "8081": {"name": "致新", "market": "TWSE"},
    "6719": {"name": "力智", "market": "TWSE"},
    "6415": {"name": "矽力*-KY", "market": "TWSE"},
    "6138": {"name": "茂達", "market": "TPEx"},
}

URLS = {
    "TWSE": "https://mopsfin.twse.com.tw/opendata/t187ap05_L.csv",
    "TPEx": "https://mopsfin.twse.com.tw/opendata/t187ap05_O.csv",
}

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# 官方CSV原始欄位 -> 程式內部欄位名稱
COLUMN_MAP = {
    "出表日期": "report_date",
    "資料年月": "data_ym",
    "公司代號": "stock_id",
    "公司名稱": "company_name",
    "產業別": "industry",
    "營業收入-當月營收": "revenue_this_month",
    "營業收入-上月營收": "revenue_last_month",
    "營業收入-去年當月營收": "revenue_same_month_last_year",
    "營業收入-上月比較增減(%)": "mom_pct",
    "營業收入-去年同月增減(%)": "yoy_pct",
    "累計營業收入-當月累計營收": "cumulative_revenue",
    "累計營業收入-去年累計營收": "cumulative_revenue_last_year",
    "累計營業收入-前期比較增減(%)": "cumulative_yoy_pct",
    "備註": "note",
}

SHEET_HEADERS = [
    "更新時間", "資料年月", "代號", "名稱",
    "當月營收(千元)", "月增率(%)", "年增率(%)",
    "累計營收(千元)", "累計年增率(%)",
]


# ══════════ 連接 Google Sheets（與 fetch_us_market.py 同規格）══════════
def connect_sheets():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise ValueError("找不到 GOOGLE_CREDENTIALS 環境變數")
    creds_dict = json.loads(creds_json)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise ValueError("找不到 GOOGLE_SHEET_ID 環境變數")
    return gc.open_by_key(sheet_id)


# ══════════ 抓取月營收 CSV ══════════
def fetch_revenue_csv(market: str) -> pd.DataFrame:
    """抓取指定市場(TWSE/TPEx)的月營收彙總CSV，回傳DataFrame。失敗時回傳空表。"""
    url = URLS[market]
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  [{market}] 下載失敗: {e}")
        return pd.DataFrame()

    resp.encoding = "utf-8-sig"  # 官方CSV為UTF-8 with BOM
    try:
        df = pd.read_csv(io.StringIO(resp.text), dtype=str)
    except Exception as e:
        print(f"  [{market}] 解析CSV失敗: {e}")
        return pd.DataFrame()

    df = df.rename(columns=COLUMN_MAP)
    if "stock_id" not in df.columns:
        print(f"  [{market}] CSV欄位結構異常，找不到公司代號欄位，"
              f"目前欄位: {df.columns.tolist()}")
        return pd.DataFrame()

    df["stock_id"] = df["stock_id"].astype(str).str.strip()
    return df


def get_watchlist_revenue() -> pd.DataFrame:
    """合併上市+上櫃資料，過濾出觀察名單，回傳整理後的DataFrame。"""
    all_rows = []

    for market in ("TWSE", "TPEx"):
        df = fetch_revenue_csv(market)
        if df.empty:
            continue

        target_ids = [sid for sid, info in WATCHLIST.items() if info["market"] == market]
        matched = df[df["stock_id"].isin(target_ids)].copy()

        found_ids = set(matched["stock_id"].tolist())
        missing_ids = set(target_ids) - found_ids
        if missing_ids:
            missing_names = [WATCHLIST[i]["name"] for i in missing_ids]
            print(f"  ⚠️ [{market}] 找不到以下股票的資料: {missing_names} "
                  f"({sorted(missing_ids)})，可能是當月資料尚未公告，"
                  f"或股票實際所屬市場(上市/上櫃)設定有誤，請手動核對")

        if not matched.empty:
            all_rows.append(matched)

    if not all_rows:
        return pd.DataFrame()

    result = pd.concat(all_rows, ignore_index=True)

    numeric_cols = [
        "revenue_this_month", "revenue_last_month", "revenue_same_month_last_year",
        "mom_pct", "yoy_pct", "cumulative_revenue",
        "cumulative_revenue_last_year", "cumulative_yoy_pct",
    ]
    for col in numeric_cols:
        result[col] = pd.to_numeric(result[col], errors="coerce")

    return result.sort_values("stock_id").reset_index(drop=True)


# ══════════ 重複資料檢查 ══════════
def get_existing_keys(sheet) -> set:
    """讀取Sheet既有的(代號, 資料年月)組合，避免同一個月重複寫入。"""
    try:
        records = sheet.get_all_values()
    except Exception as e:
        print(f"  讀取既有資料失敗（視為空表）: {e}")
        return set()

    if len(records) < 2:
        return set()

    header = records[0]
    try:
        ym_idx = header.index("資料年月")
        id_idx = header.index("代號")
    except ValueError:
        print("  既有表頭欄位跟預期不符，無法比對重複，本次將直接附加全部資料")
        return set()

    keys = set()
    for row in records[1:]:
        if len(row) > max(ym_idx, id_idx):
            keys.add((row[id_idx].strip(), row[ym_idx].strip()))
    return keys


# ══════════ 寫入試算表 ══════════
def write_to_sheets(wb, df: pd.DataFrame):
    try:
        sheet = wb.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        sheet = wb.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(SHEET_HEADERS))
        sheet.append_row(SHEET_HEADERS)
        print(f"已建立「{SHEET_NAME}」分頁")

    existing_keys = get_existing_keys(sheet)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_rows = []
    skipped = 0

    for _, r in df.iterrows():
        key = (str(r["stock_id"]).strip(), str(r["data_ym"]).strip())
        if key in existing_keys:
            skipped += 1
            continue
        new_rows.append([
            now_str, r["data_ym"], r["stock_id"], WATCHLIST.get(r["stock_id"], {}).get("name", r.get("company_name", "")),
            r["revenue_this_month"], r["mom_pct"], r["yoy_pct"],
            r["cumulative_revenue"], r["cumulative_yoy_pct"],
        ])

    if skipped:
        print(f"  {skipped} 筆資料月份已存在，跳過避免重複")

    if not new_rows:
        print("✅ 沒有新月份資料需要寫入（本月資料已是最新）")
        return

    sheet.append_rows(new_rows, value_input_option="USER_ENTERED")
    print(f"✅ 新增 {len(new_rows)} 筆月營收資料至「{SHEET_NAME}」分頁")


# ══════════ 主程式 ══════════
def main():
    print("=" * 50)
    print("觀察名單月營收抓取開始")
    print("=" * 50)

    df = get_watchlist_revenue()

    if df.empty:
        print("❌ 沒有抓到任何資料，結束（可能是當月資料尚未公告）")
        return

    data_periods = df["data_ym"].unique().tolist()
    print(f"成功抓到 {len(df)} 檔股票資料，資料年月：{data_periods}")
    print(df[["data_ym", "stock_id", "revenue_this_month", "yoy_pct"]].to_string(index=False))

    wb = connect_sheets()
    write_to_sheets(wb, df)

    print("\n" + "=" * 50)
    print("完成")
    print("=" * 50)


if __name__ == "__main__":
    main()
