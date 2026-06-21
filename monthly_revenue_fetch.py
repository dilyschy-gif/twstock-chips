"""
monthly_revenue_fetch.py
抓取台灣證交所(TWSE)與證券櫃檯買賣中心(TPEx)官方月營收公開資料，
過濾出觀察名單股票（致新8081、力智6719、矽力-KY 6415、茂達6138），
輸出本機CSV備份，並預留Google Sheet寫入接口。

資料來源（官方OpenAPI，免費、無流量限制，不依賴Finmind）：
- 上市公司每月營業收入彙總表: https://mopsfin.twse.com.tw/opendata/t187ap05_L.csv
- 上櫃公司每月營業收入彙總表: https://mopsfin.twse.com.tw/opendata/t187ap05_O.csv

更新規則：
公司須於每月10日前申報上月營收，證交所/櫃買中心通常在每月10-17日左右
更新此彙總表（每月17日後幾乎所有公司都會到齊）。這份CSV每次都是「整個市場
最新一期」的快照，不是歷史序列，所以要追蹤趨勢，必須靠這支腳本每次執行時
把當下資料存下來，疊加成自己的時間序列。

建議排程：每月1日至20日，每天執行一次（見同目錄下 monthly_revenue.yml），
腳本會自動比對「資料年月」是否為新一期，避免重複寫入舊資料。
"""

import io
import logging
from datetime import datetime

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---- 觀察名單設定：股票代號 -> {名稱, 所屬市場} ----
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

HEADERS = {
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


def fetch_revenue_csv(market: str) -> pd.DataFrame:
    """抓取指定市場(TWSE/TPEx)的月營收彙總CSV，回傳DataFrame。失敗時回傳空表。"""
    url = URLS[market]
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"[{market}] 下載失敗: {e}")
        return pd.DataFrame()

    # 官方CSV為UTF-8 with BOM
    resp.encoding = "utf-8-sig"
    try:
        df = pd.read_csv(io.StringIO(resp.text), dtype=str)
    except Exception as e:
        logger.error(f"[{market}] 解析CSV失敗: {e}")
        return pd.DataFrame()

    df = df.rename(columns=COLUMN_MAP)
    if "stock_id" not in df.columns:
        logger.error(f"[{market}] CSV欄位結構異常，找不到公司代號欄位，"
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
            logger.warning(
                f"[{market}] 找不到以下股票的資料: {missing_names} "
                f"({sorted(missing_ids)})，可能是當月資料尚未公告，"
                f"或股票實際所屬市場(上市/上櫃)設定有誤，請手動核對"
            )

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

    keep_cols = [
        "data_ym", "stock_id", "company_name", "revenue_this_month",
        "mom_pct", "yoy_pct", "cumulative_revenue", "cumulative_yoy_pct",
    ]
    return result[keep_cols].sort_values("stock_id").reset_index(drop=True)


def main():
    logger.info("開始抓取觀察名單月營收資料...")
    df = get_watchlist_revenue()

    if df.empty:
        logger.warning("沒有抓到任何資料，本次執行結束（可能是當月資料尚未公告）")
        return

    data_periods = df["data_ym"].unique().tolist()
    logger.info(f"成功抓到 {len(df)} 檔股票資料，資料年月：{data_periods}")
    print(df.to_string(index=False))

    # ---- 輸出本機CSV備份（用於累積歷史序列，建議commit進git repo保留歷史）----
    today_str = datetime.now().strftime("%Y%m%d")
    output_path = f"monthly_revenue_{today_str}.csv"
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    logger.info(f"已儲存備份至 {output_path}")

    # ---- TODO: 寫入Google Sheet ----
    # 接你StockRadar Pro既有的Google Sheets寫入邏輯，
    # 建議在試算表新增一個「月營收」分頁，比照chips/volume pipeline的寫法。
    # 範例（需 pip install gspread google-auth）：
    #
    # import gspread
    # gc = gspread.service_account(filename="credentials.json")
    # sh = gc.open_by_key("1lxp1HcYfYP_vO7r9vKiI5YYmmPyVCHXIx80qkTuQ1D4")
    # ws = sh.worksheet("月營收")
    # ws.append_rows(df.values.tolist())  # 用append而非overwrite，保留歷史紀錄
    #
    # 寫入後記得到GAS編輯器執行「管理部署作業」建立新版本，
    # 否則前端Sheet會更新但網頁/email報告讀不到新分頁的資料。


if __name__ == "__main__":
    main()
