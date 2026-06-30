"""
逆勢抗跌標的掃描模組 (fetch_contrarian.py)
觸發條件：大盤跌幅 >= 3% 時自動啟動
核心邏輯：不是找跌最少的股票，而是找最先轉強的股票
評分：法人連買(35%) + N字突破(45%) + 抗跌(20%)
作者：R2 for Dilys
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


def get_gspread_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS 環境變數未設定")
    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def parse_num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


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
        return "黃燈", 60
    elif current_price > ma20 and change
