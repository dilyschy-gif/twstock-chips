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
2026-07 修正：
寫入前用 _json_safe() 清洗 NaN/inf——新上市或去年無基期的股票，
年增率等欄位經 pd.to_numeric 會變成 NaN（或除以零變 inf），
這類浮點數不符合 JSON 規格，直接送 gspread 會炸
InvalidJSONError: Out of range float values are not JSON compliant。
清洗後以空字串寫入（沒有基期本來就算不出年增率，空白是正確表達）。
"""
import io
import json
import math
import os
from datetime import datetime, timezone, timedelta
import gspread
import pandas as pd
import requests
from google.oauth2.service_account import Credentials
# ══════════ 設定區 ══════════
SHEET_NAME = "月營收"
DATABASE_SHEET = "股票資料庫"   # 觀察名單來源：與主掃描共用同一份股票清單
# 2026-07 修正：原本 WATCHLIST 寫死 4 檔，導致「月營收」分頁只有零星資料、
# 無法支撐條件②（營收成長動能）篩選。改為執行時從「股票資料庫」分頁動態
# 載入全部代號；讀取失敗時退回以下 4 檔，確保程式不會空轉。
FALLBACK_WATCHLIST = {
    "8081": "致新",
    "6719": "力智",
    "6415": "矽力*-KY",
    "6138": "茂達",
}
TAIPEI_TZ = timezone(timedelta(hours=8))
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
# ══════════ 共用工具 ══════════
def _json_safe(v):
    """把 NaN/inf 轉成空字串，其他值原樣回傳。
    2026-07 新增：pandas 對缺值給 NaN、除以零給 inf，
    這些浮點數不符合 JSON 規格，送進 gspread 會炸 InvalidJSONError。
    """
    if v is None:
        return ""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return ""
    return v
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
# ══════════ 載入觀察名單（股票資料庫分頁）══════════
def load_watchlist(wb) -> dict:
    """從「股票資料庫」分頁讀取代號→名稱對照。失敗時退回 FALLBACK_WATCHLIST。"""
    try:
        sheet = wb.worksheet(DATABASE_SHEET)
        records = sheet.get_all_values()
    except Exception as e:
        print(f"  ⚠️ 讀取「{DATABASE_SHEET}」失敗（{e}），退回內建 {len(FALLBACK_WATCHLIST)} 檔名單")
        return dict(FALLBACK_WATCHLIST)
    if len(records) < 2:
        print(f"  ⚠️ 「{DATABASE_SHEET}」沒有資料，退回內建名單")
        return dict(FALLBACK_WATCHLIST)
    header = [h.strip() for h in records[0]]
    code_idx = name_idx = None
    for i, h in enumerate(header):
        if code_idx is None and ("代號" in h or "代碼" in h):
            code_idx = i
        if name_idx is None and "名稱" in h:
            name_idx = i
    if code_idx is None:
        print(f"  ⚠️ 「{DATABASE_SHEET}」表頭找不到代號欄位，退回內建名單")
        return dict(FALLBACK_WATCHLIST)
    watchlist = {}
    for row in records[1:]:
        if code_idx >= len(row):
            continue
        code = str(row[code_idx]).strip()
        if not code or not code[:1].isdigit():
            continue
        name = str(row[name_idx]).strip() if (name_idx is not None and name_idx < len(row)) else ""
        watchlist[code] = name
    if not watchlist:
        print(f"  ⚠️ 「{DATABASE_SHEET}」解析後沒有有效代號，退回內建名單")
        return dict(FALLBACK_WATCHLIST)
    print(f"  已從「{DATABASE_SHEET}」載入 {len(watchlist)} 檔觀察名單")
    return watchlist
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
def get_watchlist_revenue(watchlist: dict) -> pd.DataFrame:
    """合併上市+上櫃資料，過濾出觀察名單，回傳整理後的DataFrame。
    以代號直接對兩個市場的 CSV 過濾（同一代號只會出現在其中一邊），
    不再依賴人工維護的市場別欄位，避免上市/上櫃設定錯誤漏抓。
    """
    target_ids = set(watchlist.keys())
    all_rows = []
    found_ids = set()
    for market in ("TWSE", "TPEx"):
        df = fetch_revenue_csv(market)
        if df.empty:
            continue
        matched = df[df["stock_id"].isin(target_ids)].copy()
        if not matched.empty:
            found_ids |= set(matched["stock_id"].tolist())
            all_rows.append(matched)
    missing_ids = target_ids - found_ids
    if missing_ids:
        preview = sorted(missing_ids)[:10]
        print(f"  ⚠️ 有 {len(missing_ids)} 檔在兩個市場的月營收CSV都找不到"
              f"（例如 {preview}），可能是當月資料尚未公告、興櫃/ETF、或代號有誤")
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
def write_to_sheets(wb, df: pd.DataFrame, watchlist: dict):
    try:
        sheet = wb.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        sheet = wb.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(SHEET_HEADERS))
        sheet.append_row(SHEET_HEADERS)
        print(f"已建立「{SHEET_NAME}」分頁")
    existing_keys = get_existing_keys(sheet)
    now_str = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M")
    new_rows = []
    skipped = 0
    for _, r in df.iterrows():
        key = (str(r["stock_id"]).strip(), str(r["data_ym"]).strip())
        if key in existing_keys:
            skipped += 1
            continue
        code = str(r["stock_id"]).strip()
        name = watchlist.get(code) or r.get("company_name", "")
        # 2026-07 修正：數值欄位過 _json_safe 清洗 NaN/inf，
        # 否則新上市/無去年基期的股票會讓 gspread 炸 InvalidJSONError
        new_rows.append([
            now_str, r["data_ym"], code, name,
            _json_safe(r["revenue_this_month"]), _json_safe(r["mom_pct"]), _json_safe(r["yoy_pct"]),
            _json_safe(r["cumulative_revenue"]), _json_safe(r["cumulative_yoy_pct"]),
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
    wb = connect_sheets()
    watchlist = load_watchlist(wb)
    df = get_watchlist_revenue(watchlist)
    if df.empty:
        print("❌ 沒有抓到任何資料，結束（可能是當月資料尚未公告）")
        return
    data_periods = df["data_ym"].unique().tolist()
    print(f"成功抓到 {len(df)} 檔股票資料，資料年月：{data_periods}")
    print(df[["data_ym", "stock_id", "revenue_this_month", "yoy_pct"]].head(20).to_string(index=False))
    if len(df) > 20:
        print(f"  ...（其餘 {len(df) - 20} 檔省略顯示）")
    write_to_sheets(wb, df, watchlist)
    print("\n" + "=" * 50)
    print("完成")
    print("=" * 50)
if __name__ == "__main__":
    main()
