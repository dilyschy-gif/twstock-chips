# -*- coding: utf-8 -*-
"""
逆勢抗跌標的掃描模組 (fetch_contrarian.py)

整合邏輯：
1. 大盤燈號（紅 / 黃 / 綠 / 平）
2. 法人連買
3. N字 / BB 訊號
4. 抗跌 / 領先強勢
5. 正式名單 + 觀察名單

資料來源工作表：
- 選股結果
- 籌碼面資料
- 逆勢抗跌掃描
"""

import os
import json
import time
import datetime
import requests
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

SHEET_SCAN = "選股結果"
SHEET_CHIPS = "籌碼面資料"
SHEET_OUTPUT = "逆勢抗跌掃描"

MIN_VOLUME_LOTS = 300
OBSERVE_THRESHOLD = 30


def get_gspread_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS 環境變數未設定")
    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_sheet(gc):
    if not SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID 環境變數未設定")
    return gc.open_by_key(SHEET_ID)


def debug_list_worksheets(sh):
    names = [ws.title for ws in sh.worksheets()]
    print(f"[DEBUG] 目前試算表分頁：{names}")
    return names


def parse_num(v):
    try:
        s = str(v).replace(",", "").replace("%", "").strip()
        if s == "":
            return 0
        return float(s)
    except (ValueError, TypeError):
        return 0


def safe_text(v):
    return str(v).strip() if v is not None else ""


def fetch_market_index_change():
    """從 Yahoo Finance 取得台灣加權指數當日漲跌幅"""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
    params = {"range": "5d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        valid_closes = [c for c in closes if c is not None]

        if len(valid_closes) < 2:
            return 0, 0, 0

        current = valid_closes[-1]
        previous = valid_closes[-2]
        change_pct = ((current - previous) / previous) * 100
        change_points = current - previous
        return round(change_pct, 2), round(change_points, 2), round(current, 2)
    except Exception as e:
        print(f"[WARN] 無法取得加權指數: {e}")
        return 0, 0, 0


def fetch_market_index_ma20():
    """取得加權指數近20日均線"""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
    params = {"range": "1mo", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        valid = [c for c in closes if c is not None]
        if not valid:
            return 0
        if len(valid) >= 20:
            return round(sum(valid[-20:]) / 20, 2)
        return round(sum(valid) / len(valid), 2)
    except Exception as e:
        print(f"[WARN] 無法取得 MA20: {e}")
        return 0


def determine_market_light(change_pct, current_price, ma20):
    if change_pct <= -3:
        return "紅燈", 70
    elif change_pct <= -1.5:
        return "黃燈", 50
    elif current_price > ma20 and change_pct > 0:
        return "綠燈", 40
    else:
        return "平燈", 50


def calc_institutional_streaks(gc):
    """讀取『籌碼面資料』分頁，計算每支股票的投信/外資連續買超天數"""
    sh = get_sheet(gc)

    try:
        ws = sh.worksheet(SHEET_CHIPS)
    except gspread.exceptions.WorksheetNotFound:
        names = debug_list_worksheets(sh)
        print(f"[WARN] 找不到『{SHEET_CHIPS}』分頁，現有分頁：{names}")
        return {}

    records = ws.get_all_values()
    if len(records) < 2:
        print(f"[WARN] 『{SHEET_CHIPS}』分頁沒有資料")
        return {}

    header = records[0]
    rows = records[1:]

    col_map = {}
    for i, h in enumerate(header):
        h_clean = safe_text(h)
        if "日期" in h_clean:
            col_map["date"] = i
        elif h_clean == "代號":
            col_map["code"] = i
        elif "投信" in h_clean:
            col_map["trust"] = i
        elif "外資" in h_clean:
            col_map["foreign"] = i
        elif "合計" in h_clean or "三法人" in h_clean:
            col_map["total"] = i

    if "code" not in col_map:
        print(f"[WARN] 無法解析『{SHEET_CHIPS}』欄位，header={header}")
        return {}

    stock_data = {}
    for row in rows:
        try:
            code = safe_text(row[col_map["code"]]) if col_map["code"] < len(row) else ""
            if not code:
                continue

            date_str = safe_text(row[col_map["date"]]) if "date" in col_map and col_map["date"] < len(row) else ""
            trust_val = row[col_map["trust"]] if "trust" in col_map and col_map["trust"] < len(row) else "0"
            foreign_val = row[col_map["foreign"]] if "foreign" in col_map and col_map["foreign"] < len(row) else "0"
            total_val = row[col_map["total"]] if "total" in col_map and col_map["total"] < len(row) else "0"

            stock_data.setdefault(code, []).append({
                "date": date_str,
                "trust": parse_num(trust_val),
                "foreign": parse_num(foreign_val),
                "total": parse_num(total_val),
            })
        except Exception:
            continue

    streaks = {}
    for code, entries in stock_data.items():
        entries.sort(key=lambda x: x["date"], reverse=True)

        trust_streak = 0
        foreign_streak = 0

        for entry in entries:
            if entry["trust"] > 0:
                trust_streak += 1
            else:
                break

        for entry in entries:
            if entry["foreign"] > 0:
                foreign_streak += 1
            else:
                break

        max_streak = max(trust_streak, foreign_streak)
        if max_streak >= 15:
            score = 35
        elif max_streak >= 10:
            score = 28
        elif max_streak >= 5:
            score = 20
        elif max_streak >= 3:
            score = 12
        elif max_streak >= 1:
            score = 5
        else:
            score = 0

        latest_total = entries[0]["total"] if entries else 0

        streaks[code] = {
            "trust_streak": trust_streak,
            "foreign_streak": foreign_streak,
            "institutional_score": score,
            "latest_total": latest_total,
        }

    return streaks


def read_existing_scan_results(gc):
    """讀取『選股結果』分頁"""
    sh = get_sheet(gc)

    try:
        ws = sh.worksheet(SHEET_SCAN)
    except gspread.exceptions.WorksheetNotFound:
        names = debug_list_worksheets(sh)
        print(f"[WARN] 找不到『{SHEET_SCAN}』分頁，現有分頁：{names}")
        return {}

    records = ws.get_all_values()
    if len(records) < 2:
        print(f"[WARN] 『{SHEET_SCAN}』分頁沒有資料")
        return {}

    header = records[0]
    rows = records[1:]

    col_map = {}
    for i, h in enumerate(header):
        h_clean = safe_text(h)
        if h_clean == "代號":
            col_map["code"] = i
        elif h_clean == "名稱":
            col_map["name"] = i
        elif h_clean in ("現價", "收盤價"):
            col_map["price"] = i
        elif h_clean in ("BB訊號", "訊號"):
            col_map["signal"] = i
        elif "N字目標" in h_clean:
            col_map["n_target"] = i
        elif "起漲點" in h_clean:
            col_map["start_point"] = i
        elif "帶寬" in h_clean:
            col_map["bandwidth"] = i
        elif "量比" in h_clean:
            col_map["vol_ratio"] = i
        elif "市場" in h_clean:
            col_map["market"] = i
        elif h_clean == "badges":
            col_map["badges"] = i

    if "code" not in col_map:
        print(f"[WARN] 無法解析『{SHEET_SCAN}』欄位，header={header}")
        return {}

    results = {}
    for row in rows:
        try:
            code_idx = col_map.get("code")
            if code_idx is None or code_idx >= len(row):
                continue

            code = safe_text(row[code_idx])
            if not code:
                continue

            def safe_get(key, default=""):
                idx = col_map.get(key)
                if idx is None or idx >= len(row):
                    return default
                return row[idx]

            def safe_float(key):
                return parse_num(safe_get(key, "0"))

            results[code] = {
                "name": safe_text(safe_get("name")),
                "price": safe_float("price"),
                "signal": safe_text(safe_get("signal")),
                "n_target": safe_float("n_target"),
                "start_point": safe_float("start_point"),
                "bandwidth": safe_float("bandwidth"),
                "vol_ratio": safe_float("vol_ratio"),
                "market": safe_text(safe_get("market")) or "上市",
                "badges": safe_text(safe_get("badges")),
            }
        except Exception:
            continue

    return results


def fetch_stock_daily_change(code, market="上市"):
    """取得個股當日漲跌幅和成交量"""
    suffix = ".TW" if market == "上市" else ".TWO"
    symbol = f"{code}{suffix}"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "2d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        volumes = result["indicators"]["quote"][0]["volume"]

        valid_closes = [c for c in closes if c is not None]
        valid_volumes = [v for v in volumes if v is not None]

        if len(valid_closes) < 2:
            return None

        current = valid_closes[-1]
        previous = valid_closes[-2]
        change_pct = ((current - previous) / previous) * 100
        volume = valid_volumes[-1] if valid_volumes else 0

        return {
            "price": round(current, 2),
            "change_pct": round(change_pct, 2),
            "volume": volume,
        }
    except Exception:
        return None


def calc_contrarian_score(stock_change_pct, market_change_pct):
    score_a = 0
    if stock_change_pct > 0:
        score_a = 15
    if stock_change_pct > 2:
        score_a = 20

    if stock_change_pct > 0:
        rel_strength = abs(market_change_pct) + stock_change_pct
    else:
        rel_strength = abs(market_change_pct) - abs(stock_change_pct)

    score_b = 0
    if 
