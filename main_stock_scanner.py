# -*- coding: utf-8 -*-
"""
主升段候選股主掃描程式

目標：短期賺價差，而不是只找法人買超。

正式進場邏輯：
1. 三大法人合計買超 > 0，排除沒有法人支持的假訊號。
2. 投信/外資連買天數越長，籌碼分越高。
3. 法人買超同時，價格必須配合：N字頸線突破 + 創波段新高。
4. 帶寬收斂、KD偏多、量能放大是加分，不取代突破。

輸出分頁：
- 掃描結果：完整主掃描候選清單
- 選股結果：同步給逆勢抗跌掃描讀取
- 掃描進度：記錄處理狀態

必要環境變數：
- GOOGLE_SHEET_ID
- GOOGLE_CREDENTIALS  # service account JSON 字串

可選環境變數：
- SCAN_START_INDEX    # 從股票資料庫第幾檔開始，預設 0
- SCAN_LIMIT          # 最多掃描幾檔，預設全部
"""

import datetime
import json
import math
import os
import statistics
import time
from typing import Dict, List, Optional, Tuple

import gspread
import requests
from google.oauth2.service_account import Credentials

from v_reversal import evaluate_v_reversal

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
SHEET_STOCK_DB = "股票資料庫"
SHEET_CHIPS = "籌碼面資料"
SHEET_SCAN_RESULT = "掃描結果"
SHEET_SELECTION = "選股結果"
SHEET_V_REVERSAL = "V型反轉掃描"
SHEET_PROGRESS = "掃描進度"

MIN_VOLUME_LOTS = 300
FORMAL_THRESHOLD = 60
OBSERVE_THRESHOLD = 30
REQUEST_SLEEP_SECONDS = 0.12
TAIPEI_TZ = datetime.timezone(datetime.timedelta(hours=8))

OUTPUT_HEADERS = [
    "代號", "名稱", "市場", "產業", "現價", "BB訊號", "N字目標", "起漲點", "帶寬",
    "K值", "D值", "量比", "量能訊號", "命中率", "compositeScore", "techScore",
    "chipsScore", "usScore", "volScore", "badges", "chipsDetail", "usDetail", "volDetail"
]

V_OUTPUT_HEADERS = [
    "代號", "名稱", "市場", "產業", "現價", "V狀態", "V分數", "左臂跌幅",
    "RSI14", "黑K數", "紅K收盤位置", "上影占比", "量比", "相對大盤", "法人訊號",
    "左臂高點", "V底", "紅K中值", "V2確認價", "50%收復價", "61.8%收復價",
    "失效價", "轉折日", "badges", "chipsDetail", "備註",
]


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


def safe_text(value) -> str:
    return str(value).strip() if value is not None else ""


def cell(row: List, idx: Optional[int], default: str = "") -> str:
    """安全讀取欄位：idx 為 None、負數或超界時回傳 default。

    修正前用 row[col.get(key, -1)]，欄位不存在時 -1 會默默讀到該列最後一欄。
    """
    if idx is None or idx < 0 or idx >= len(row):
        return default
    return safe_text(row[idx])


def parse_num(value) -> float:
    try:
        text = safe_text(value).replace(",", "").replace("%", "")
        return float(text) if text else 0.0
    except (TypeError, ValueError):
        return 0.0


def normalize_code(value) -> str:
    """Normalize stock code from Google Sheets/Yahoo formats.

    Handles values like 50, 50.0, 0050, 2330.TW, 8069.TWO,
    leading apostrophes, whitespace, and invisible formatting characters.
    """
    text = safe_text(value)
    if not text:
        return ""

    text = text.upper().replace(".TW", "").replace(".TWO", "")
    text = text.replace("'", "").replace("\u200b", "").replace("\ufeff", "").strip()

    # Google Sheets sometimes returns a code-looking cell as 2330.0.
    if text.endswith(".0") and text[:-2].replace(".", "", 1).isdigit():
        text = text[:-2]

    digits = "".join(ch for ch in text if ch.isdigit())
    if digits:
        if len(digits) <= 4:
            return digits.zfill(4)
        return digits[:6]

    letters_digits = "".join(ch for ch in text if ch.isalnum())
    return letters_digits


def worksheet_or_create(sh, title: str, rows: int = 1000, cols: int = 30):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def build_col_map(headers: List[str], aliases: Dict[str, List[str]]) -> Dict[str, int]:
    result = {}
    for i, header in enumerate(headers):
        h = safe_text(header)
        for key, names in aliases.items():
            if h in names or any(name in h for name in names if len(name) >= 3):
                result.setdefault(key, i)
    return result


def read_stock_database(gc) -> List[Dict]:
    sh = get_sheet(gc)
    ws = sh.worksheet(SHEET_STOCK_DB)
    records = ws.get_all_values()
    if len(records) < 2:
        return []

    headers = records[0]
    col = build_col_map(headers, {
        "market": ["市場別", "市場"],
        "code": ["股票代號", "代號", "證券代號"],
        "name": ["股票名稱", "名稱", "證券名稱"],
        "industry": ["產業別", "產業"],
    })

    if "code" not in col:
        raise RuntimeError(f"無法解析『{SHEET_STOCK_DB}』表頭：{headers}")

    stocks = []
    for row in records[1:]:
        code = normalize_code(row[col["code"]] if col["code"] < len(row) else "")
        if not code:
            continue
        stocks.append({
            "market": cell(row, col.get("market"), "上市") or "上市",
            "code": code,
            "name": cell(row, col.get("name")),
            "industry": cell(row, col.get("industry")),
        })
    return stocks


def read_chip_streaks(gc) -> Dict[str, Dict]:
    sh = get_sheet(gc)
    try:
        ws = sh.worksheet(SHEET_CHIPS)
    except gspread.exceptions.WorksheetNotFound:
        print(f"[WARN] 找不到『{SHEET_CHIPS}』分頁，籌碼分會全部為 0")
        return {}

    records = ws.get_all_values()
    if len(records) < 2:
        return {}

    headers = records[0]
    col = build_col_map(headers, {
        "date": ["日期"],
        "code": ["代號", "股票代號", "證券代號"],
        "foreign": ["外資買賣超", "外資"],
        "trust": ["投信買賣超", "投信"],
        "dealer": ["自營商買賣超", "自營商"],
        "total": ["三法人合計", "三大法人合計", "法人合計", "合計"],
        "name": ["名稱", "股票名稱", "證券名稱"],
        "market": ["市場", "市場別"],
    })

    if "code" not in col:
        print(f"[WARN] 無法解析『{SHEET_CHIPS}』欄位，籌碼分會全部為 0。表頭：{headers}")
        return {}

    print(f"[DEBUG] 籌碼欄位對應：{col}")

    by_code: Dict[str, List[Dict]] = {}
    for row in records[1:]:
        code_idx = col["code"]
        code = normalize_code(row[code_idx] if code_idx < len(row) else "")
        if not code:
            continue

        def get(key, default="0"):
            idx = col.get(key)
            return row[idx] if idx is not None and idx < len(row) else default

        foreign = parse_num(get("foreign"))
        trust = parse_num(get("trust"))
        dealer = parse_num(get("dealer"))
        total_from_col = parse_num(get("total")) if "total" in col else 0
        total_recalc = foreign + trust + dealer
        total = total_from_col if total_from_col != 0 else total_recalc

        by_code.setdefault(code, []).append({
            "date": safe_text(get("date", "")),
            "date_key": parse_chip_date_key(safe_text(get("date", ""))),
            "foreign": foreign,
            "trust": trust,
            "dealer": dealer,
            "total": total,
            "total_from_col": total_from_col,
            "total_recalc": total_recalc,
            "name": safe_text(get("name", "")),
            "market": safe_text(get("market", "")),
        })

    result = {}
    positive_latest_count = 0
    for code, entries in by_code.items():
        entries.sort(key=lambda x: x["date_key"], reverse=True)
        trust_streak = count_positive_streak(entries, "trust")
        foreign_streak = count_positive_streak(entries, "foreign")
        total_streak = count_positive_streak(entries, "total")
        trust_positive_days_5 = sum(entry.get("trust", 0) > 0 for entry in entries[:5])
        foreign_positive_days_2 = sum(entry.get("foreign", 0) > 0 for entry in entries[:2])
        foreign_turn_buy = (
            len(entries) >= 2
            and foreign_positive_days_2 == 2
            and any(entry.get("foreign", 0) < 0 for entry in entries[2:5])
        )
        latest = entries[0] if entries else {}
        latest_total = latest.get("total", 0)
        if latest_total > 0:
            positive_latest_count += 1

        chips_score = 0
        if latest_total > 0:
            chips_score += 10
        # 投信連買（主要權重）
        if trust_streak >= 15:
            chips_score += 30
        elif trust_streak >= 10:
            chips_score += 24
        elif trust_streak >= 5:
            chips_score += 18
        elif trust_streak >= 3:
            chips_score += 10
        elif trust_streak >= 1:
            chips_score += 4
        # 外資連買（獨立加分，不再被投信連買 1 日就整個蓋掉）
        if foreign_streak >= 10:
            chips_score += 8
        elif foreign_streak >= 5:
            chips_score += 5
        elif foreign_streak >= 3:
            chips_score += 3

        result[code] = {
            "latest_total": latest_total,
            "latest_foreign": latest.get("foreign", 0),
            "latest_trust": latest.get("trust", 0),
            "latest_dealer": latest.get("dealer", 0),
            "latest_chip_date": latest.get("date", ""),
            "name": latest.get("name", ""),
            "market": latest.get("market", ""),
            "trust_streak": trust_streak,
            "foreign_streak": foreign_streak,
            "total_streak": total_streak,
            "trust_positive_days_5": trust_positive_days_5,
            "foreign_positive_days_2": foreign_positive_days_2,
            "foreign_turn_buy": foreign_turn_buy,
            "chips_score": min(chips_score, 45),
            "chips_detail": (
                f"{latest.get('date', '')} 三法人合計{latest_total:.0f}張；"
                f"外資{latest.get('foreign', 0):.0f}；投信{latest.get('trust', 0):.0f}；自營商{latest.get('dealer', 0):.0f}；"
                f"投信連買{trust_streak}日；外資連買{foreign_streak}日；合計連買{total_streak}日"
            ),
            "max_inst_streak": max(trust_streak, foreign_streak),
        }

    print(f"[DEBUG] 籌碼資料代號數：{len(result)}；最近一筆三法人合計買超：{positive_latest_count} 檔")
    return result


def parse_chip_date_key(text: str) -> str:
    text = safe_text(text)
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.datetime.strptime(text[:10], fmt).strftime("%Y%m%d")
        except ValueError:
            pass
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:8]


def count_positive_streak(entries: List[Dict], key: str) -> int:
    streak = 0
    for entry in entries:
        if entry.get(key, 0) > 0:
            streak += 1
        else:
            break
    return streak


def build_scan_universe(stocks: List[Dict], chips: Dict[str, Dict]) -> List[Dict]:
    """Use stock database first; if it has no overlap with chips, scan chip universe.

    The current sheet can have 股票資料庫 = 上櫃 universe while 籌碼面資料 = 上市 universe.
    If we keep using only 股票資料庫, every stock will miss the institutional gate.
    """
    stock_codes = {stock["code"] for stock in stocks}
    chip_codes = set(chips.keys())
    matched = stock_codes.intersection(chip_codes)

    if matched:
        return stocks

    print("[WARN] 股票資料庫與籌碼面資料完全沒有交集，改用籌碼面資料建立掃描清單。")
    chip_stocks = []
    for code, chip in chips.items():
        market = chip.get("market") or "上市"
        chip_stocks.append({
            "market": market,
            "code": code,
            "name": chip.get("name", ""),
            "industry": "",
        })
    chip_stocks.sort(key=lambda x: x["code"])
    print(f"[DEBUG] 改用籌碼清單掃描：{len(chip_stocks)} 檔")
    return chip_stocks


def yahoo_symbol(code: str, market: str) -> str:
    suffix = ".TW" if "上市" in market else ".TWO"
    return f"{code}{suffix}"


def fetch_daily_history(code: str, market: str) -> Optional[List[Dict]]:
    symbol = yahoo_symbol(code, market)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "6mo", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=12)
        r.raise_for_status()
        data = r.json()
        chart = data.get("chart", {})
        results = chart.get("result") or []
        if not results:
            return None

        result = results[0]
        timestamps = result.get("timestamp") or []
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        rows = []
        for i, ts in enumerate(timestamps):
            close = closes[i] if i < len(closes) else None
            high = highs[i] if i < len(highs) else None
            low = lows[i] if i < len(lows) else None
            volume = volumes[i] if i < len(volumes) else None
            if close is None or high is None or low is None:
                continue
            rows.append({
                "date": datetime.datetime.fromtimestamp(ts, tz=TAIPEI_TZ).strftime("%Y-%m-%d"),
                "open": opens[i] if i < len(opens) and opens[i] is not None else close,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume or 0,
            })
        return rows if len(rows) >= 30 else None
    except Exception as exc:
        print(f"[WARN] {code} Yahoo 日資料失敗：{exc}")
        return None


def fetch_market_change_pct() -> float:
    """讀取加權指數當日漲跌，避免大盤大漲時把被動反彈誤判為個股轉強。"""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
    params = {"range": "5d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, params=params, headers=headers, timeout=12)
        response.raise_for_status()
        result = (response.json().get("chart", {}).get("result") or [])[0]
        closes = [
            float(value)
            for value in result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            if value is not None
        ]
        if len(closes) < 2 or not closes[-2]:
            return 0.0
        return round((closes[-1] - closes[-2]) / closes[-2] * 100, 2)
    except Exception as exc:
        print(f"[WARN] 加權指數資料失敗，V型相對強弱暫以0%計：{exc}")
        return 0.0


def calc_indicators(history: List[Dict]) -> Dict:
    closes = [x["close"] for x in history]
    highs = [x["high"] for x in history]
    lows = [x["low"] for x in history]
    volumes = [x["volume"] for x in history]

    close = closes[-1]
    prev_close = closes[-2]
    change_pct = ((close - prev_close) / prev_close * 100) if prev_close else 0
    volume = volumes[-1]
    volume_lots = volume / 1000
    avg_volume_20 = sum(volumes[-21:-1]) / min(20, len(volumes[-21:-1])) if len(volumes) > 1 else volume
    volume_ratio = volume / avg_volume_20 if avg_volume_20 else 0

    ma20_values = closes[-20:]
    ma20 = sum(ma20_values) / len(ma20_values)
    std20 = statistics.pstdev(ma20_values) if len(ma20_values) >= 2 else 0
    upper = ma20 + 2 * std20
    lower = ma20 - 2 * std20
    bandwidth = ((upper - lower) / ma20 * 100) if ma20 else 0

    k_value, d_value = calc_kd(highs, lows, closes)
    n_data = calc_n_structure(highs, lows, closes)

    return {
        "close": close,
        "change_pct": change_pct,
        "volume": volume,
        "volume_lots": volume_lots,
        "volume_ratio": volume_ratio,
        "ma20": ma20,
        "upper": upper,
        "lower": lower,
        "bandwidth": bandwidth,
        "k_value": k_value,
        "d_value": d_value,
        **n_data,
    }


def calc_kd(highs: List[float], lows: List[float], closes: List[float],
            period: int = 9) -> Tuple[float, float]:
    """標準 KD(9,3,3)：K = 2/3·前K + 1/3·RSV；D = 2/3·前D + 1/3·K。

    修正前用 RSV 簡單平均近似，數值會跟看盤軟體對不上；改為遞迴平滑後一致。
    """
    if len(closes) < period:
        return 50.0, 50.0

    k, d = 50.0, 50.0
    for end in range(period, len(closes) + 1):
        window_high = max(highs[end - period:end])
        window_low = min(lows[end - period:end])
        close = closes[end - 1]
        rsv = 50.0 if window_high == window_low else (close - window_low) / (window_high - window_low) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
    return round(k, 2), round(d, 2)


def find_recent_pivot_high(highs: List[float], left: int = 3, right: int = 3,
                           skip_recent: int = 1) -> Optional[int]:
    """找最近一個波段轉折高點（pivot high）的索引。

    pivot high 定義：該日高點 >= 左右各 left/right 天窗口內的最高價。
    skip_recent 避免把最近一兩天（可能就是突破當天）誤當成頸線。
    找不到時回傳 None。
    """
    n = len(highs)
    for i in range(n - right - skip_recent - 1, left - 1, -1):
        window = highs[i - left:i + right + 1]
        if highs[i] >= max(window):
            return i
    return None


def calc_n_structure(highs: List[float], lows: List[float], closes: List[float]) -> Dict:
    """N 字結構（2026-07 修正版）。

    修正前：neckline = max(20日前高, 60日前高)，數學上恆等於 60 日前高，
    導致「N字頸線突破」與「創波段新高」是同一個條件——只會抓到已經噴出的股票。

    修正後：
      - neckline    = 最近一個波段轉折高點（左腳的頂，pivot high）
      - start_price = 該轉折高點之後的回檔低點（右腳起漲點）
      - n_target    = 頸線 + (頸線 - 右腳低點)
      - swing_new_high 維持「收盤 > 60 日前高」，與頸線突破為兩個獨立條件。
        右腳醞釀股會呈現「接近/剛突破頸線、但尚未創 60 日新高」的組合。
    """
    close = closes[-1]
    prior_60_high = max(highs[-61:-1]) if len(highs) >= 61 else max(highs[:-1])

    pivot_idx = find_recent_pivot_high(highs)
    if pivot_idx is not None:
        neckline = highs[pivot_idx]
        pullback_lows = lows[pivot_idx + 1:-1]
        if not pullback_lows:
            pullback_lows = lows[pivot_idx:]
        start_price = min(pullback_lows)
    else:
        # 資料太短、找不到轉折點時的退回邏輯
        neckline = max(highs[-21:-1]) if len(highs) >= 21 else max(highs[:-1])
        recent_low_window = lows[-45:-5] if len(lows) >= 45 else lows[:-5] or lows[:-1]
        start_price = min(recent_low_window) if recent_low_window else min(lows)

    neckline_breakout = close > neckline
    swing_new_high = close > prior_60_high
    n_target = neckline + max(neckline - start_price, 0)

    return {
        "neckline": neckline,
        "start_price": start_price,
        "n_target": n_target,
        "neckline_breakout": neckline_breakout,
        "swing_new_high": swing_new_high,
    }


def score_stock(stock: Dict, indicators: Dict, chip: Dict) -> Dict:
    latest_total = chip.get("latest_total", 0)
    trust_streak = chip.get("trust_streak", 0)
    foreign_streak = chip.get("foreign_streak", 0)
    chips_score = chip.get("chips_score", 0)

    institutional_buy = latest_total > 0
    neckline_breakout = indicators["neckline_breakout"]
    swing_new_high = indicators["swing_new_high"]
    volume_ok = indicators["volume_lots"] >= MIN_VOLUME_LOTS
    kd_bull = indicators["k_value"] >= indicators["d_value"] and indicators["k_value"] >= 40
    band_contract = 0 < indicators["bandwidth"] <= 12
    above_ma20 = indicators["close"] >= indicators["ma20"]
    volume_expand = indicators["volume_ratio"] >= 1.2

    tech_score = 0
    if neckline_breakout:
        tech_score += 30
    if swing_new_high:
        tech_score += 20
    if band_contract:
        tech_score += 10
    if kd_bull:
        tech_score += 10
    if above_ma20:
        tech_score += 10
    tech_score = min(tech_score, 60)

    vol_score = 0
    if volume_ok:
        vol_score += 10
    if volume_expand:
        vol_score += 10
    vol_score = min(vol_score, 20)

    composite = min(round(tech_score + chips_score + vol_score, 1), 100)

    formal_ready = institutional_buy and neckline_breakout and swing_new_high and volume_ok and composite >= FORMAL_THRESHOLD
    observe_ready = institutional_buy and composite >= OBSERVE_THRESHOLD

    badges = []
    if formal_ready:
        badges.append("進場訊號")
    elif observe_ready:
        badges.append("觀察")
    if institutional_buy:
        badges.append("法人買超")
    if trust_streak >= 15:
        badges.append("投信連買15日")
    elif trust_streak >= 10:
        badges.append("投信連買10日")
    elif trust_streak >= 5:
        badges.append("投信連買5日")
    elif foreign_streak >= 5:
        badges.append("外資連買5日")
    if neckline_breakout:
        badges.append("N字突破")
    if swing_new_high:
        badges.append("波段新高")
    if band_contract:
        badges.append("帶寬收斂")
    if volume_expand:
        badges.append("量能放大")

    if neckline_breakout and swing_new_high:
        bb_signal = "起漲突破"
    elif band_contract:
        bb_signal = "收斂觀察"
    elif above_ma20:
        bb_signal = "多頭觀察"
    else:
        bb_signal = "尚未突破"

    if indicators["volume_ratio"] >= 1.5:
        volume_signal = "明顯放量"
    elif indicators["volume_ratio"] >= 1.2:
        volume_signal = "溫和放量"
    elif indicators["volume_ratio"] >= 0.8:
        volume_signal = "量能正常"
    else:
        volume_signal = "量能不足"

    category = "淘汰"
    if formal_ready:
        category = "正式"
    elif observe_ready:
        category = "觀察"

    block_reason = ""
    if institutional_buy and not formal_ready:
        missing = []
        if not neckline_breakout:
            missing.append("未突破N字頸線")
        if not swing_new_high:
            missing.append("未創波段新高")
        if not volume_ok:
            missing.append(f"成交量低於{MIN_VOLUME_LOTS}張")
        if composite < FORMAL_THRESHOLD:
            missing.append("分數未達正式門檻")
        block_reason = "；".join(missing)

    return {
        "category": category,
        "formal_ready": formal_ready,
        "observe_ready": observe_ready,
        "institutional_buy": institutional_buy,
        "bb_signal": bb_signal,
        "volume_signal": volume_signal,
        "badges": "、".join(badges),
        "chips_detail": chip.get("chips_detail", ""),
        "tech_score": tech_score,
        "chips_score": chips_score,
        "vol_score": vol_score,
        "composite": composite,
        "block_reason": block_reason,
    }


def build_output_row(stock: Dict, indicators: Dict, score: Dict) -> List:
    return [
        stock["code"],
        stock["name"],
        stock["market"],
        stock["industry"],
        round(indicators["close"], 2),
        score["bb_signal"],
        round(indicators["n_target"], 2),
        round(indicators["start_price"], 2),
        round(indicators["bandwidth"], 2),
        round(indicators["k_value"], 2),
        round(indicators["d_value"], 2),
        round(indicators["volume_ratio"], 2),
        score["volume_signal"],
        score["category"],
        score["composite"],
        score["tech_score"],
        score["chips_score"],
        0,
        score["vol_score"],
        score["badges"],
        score["chips_detail"],
        "",
        score["block_reason"],
    ]


def build_v_output_row(stock: Dict, result: Dict) -> List:
    def value(key: str, percent: bool = False):
        raw = result.get(key)
        if raw is None:
            return ""
        number = raw * 100 if percent else raw
        return round(number, 2)

    return [
        stock["code"],
        stock["name"],
        stock["market"],
        stock["industry"],
        value("close"),
        result["state"],
        result["score"],
        value("left_drop_pct"),
        value("rsi14"),
        result["black_count"],
        value("close_location", percent=True),
        value("upper_wick_ratio", percent=True),
        value("volume_ratio"),
        value("relative_strength"),
        result["institutional_signal"],
        value("left_peak"),
        value("v_bottom"),
        value("trigger_mid"),
        value("v2_confirm"),
        value("recover_50"),
        value("recover_618"),
        value("invalid_price"),
        result["trigger_date"],
        result["badges"],
        result["chips_detail"],
        result["note"],
    ]


def write_table(sh, title: str, rows: List[List], headers: Optional[List[str]] = None):
    table_headers = headers or OUTPUT_HEADERS
    ws = worksheet_or_create(sh, title, rows=max(len(rows) + 10, 1000), cols=len(table_headers) + 5)
    ws.clear()
    values = [table_headers] + rows
    ws.update(range_name="A1", values=values)
    ws.freeze(rows=1)


def write_progress(sh, status: str, start_index: int, passed_count: int):
    ws = worksheet_or_create(sh, SHEET_PROGRESS, rows=20, cols=5)
    now = datetime.datetime.now(TAIPEI_TZ).strftime("%Y/%m/%d %H:%M:%S")
    ws.clear()
    ws.update(range_name="A1", values=[
        ["狀態", "起始索引", "已通過數", "最後更新"],
        [status, start_index, passed_count, now],
    ])


def run_main_scan():
    print("=" * 60)
    print("主升段候選股主掃描 啟動")
    print("核心：法人買超是門檻，N字突破 + 波段新高才是進場訊號")
    print("=" * 60)

    start_index = int(os.environ.get("SCAN_START_INDEX", "0") or 0)
    limit_text = os.environ.get("SCAN_LIMIT", "").strip()
    scan_limit = int(limit_text) if limit_text else None

    gc = get_gspread_client()
    sh = get_sheet(gc)

    stocks = read_stock_database(gc)
    if start_index:
        stocks = stocks[start_index:]
    if scan_limit:
        stocks = stocks[:scan_limit]

    print(f"讀取股票資料庫：{len(stocks)} 檔")
    chips = read_chip_streaks(gc)
    print(f"讀取籌碼資料：{len(chips)} 檔")
    market_change_pct = fetch_market_change_pct()
    print(f"加權指數當日漲跌：{market_change_pct:+.2f}%")
    stocks = build_scan_universe(stocks, chips)
    stock_codes = {stock["code"] for stock in stocks}
    matched_chip_codes = stock_codes.intersection(set(chips.keys()))
    positive_chip_codes = {code for code in matched_chip_codes if chips.get(code, {}).get("latest_total", 0) > 0}
    print(f"[DEBUG] 股票資料庫對到籌碼：{len(matched_chip_codes)}/{len(stock_codes)} 檔")
    print(f"[DEBUG] 本次掃描範圍最近一筆三法人合計買超：{len(positive_chip_codes)} 檔")
    if len(matched_chip_codes) == 0:
        sample_stock_codes = sorted(list(stock_codes))[:20]
        sample_chip_codes = sorted(list(chips.keys()))[:20]
        print(f"[DEBUG] 股票資料庫代號樣本：{sample_stock_codes}")
        print(f"[DEBUG] 籌碼資料代號樣本：{sample_chip_codes}")

    formal_rows = []
    observe_rows = []
    v_rows = []
    include_failed_v = os.environ.get("V_INCLUDE_FAILED", "0") == "1"
    stats = {
        "processed": 0,
        "no_daily": 0,
        "low_volume": 0,
        "no_inst_buy": 0,
        "no_breakout": 0,
        "below_observe": 0,
        "v_failed": 0,
    }

    for idx, stock in enumerate(stocks, 1):
        stats["processed"] += 1
        code = stock["code"]
        market = stock["market"] or "上市"

        history = fetch_daily_history(code, market)
        if not history:
            stats["no_daily"] += 1
            continue

        indicators = calc_indicators(history)
        chip = chips.get(code, {})
        score = score_stock(stock, indicators, chip)
        v_result = evaluate_v_reversal(history, chip, market_change_pct)
        if v_result:
            if v_result["state"] == "VX":
                stats["v_failed"] += 1
                if include_failed_v:
                    v_rows.append(build_v_output_row(stock, v_result))
            else:
                v_rows.append(build_v_output_row(stock, v_result))

        if indicators["volume_lots"] < MIN_VOLUME_LOTS:
            stats["low_volume"] += 1

        if not score["institutional_buy"]:
            stats["no_inst_buy"] += 1
        elif not (indicators["neckline_breakout"] and indicators["swing_new_high"]):
            stats["no_breakout"] += 1

        row = build_output_row(stock, indicators, score)
        if score["formal_ready"]:
            formal_rows.append(row)
        elif score["observe_ready"]:
            observe_rows.append(row)
        else:
            stats["below_observe"] += 1

        if idx % 50 == 0:
            print(
                f"已處理 {idx}/{len(stocks)} 檔；正式 {len(formal_rows)}；"
                f"觀察 {len(observe_rows)}；V型 {len(v_rows)}"
            )
            write_progress(sh, "執行中", start_index, len(formal_rows) + len(observe_rows))

        time.sleep(REQUEST_SLEEP_SECONDS)

    all_rows = formal_rows + observe_rows
    all_rows.sort(key=lambda row: parse_num(row[14]), reverse=True)
    v_state_order = {"V1": 0, "V0": 1, "V2": 2, "V3": 3, "VX": 4}
    v_rows.sort(key=lambda row: (v_state_order.get(safe_text(row[5]), 9), -parse_num(row[6])))

    # 只寫「選股結果」一個分頁：逆勢抗跌掃描與 data.json 匯出都改讀這一頁
    write_table(sh, SHEET_SELECTION, all_rows)
    write_table(sh, SHEET_V_REVERSAL, v_rows, V_OUTPUT_HEADERS)
    write_progress(sh, "完成", start_index, len(all_rows))

    print("\n淘汰/保留統計：")
    print(f"處理總數：{stats['processed']}")
    print(f"取不到日資料：{stats['no_daily']}")
    print(f"成交量不足(<{MIN_VOLUME_LOTS}張)：{stats['low_volume']}")
    print(f"三法人未買超：{stats['no_inst_buy']}")
    print(f"法人買但價格未突破：{stats['no_breakout']}")
    print(f"分數低於觀察門檻{OBSERVE_THRESHOLD}：{stats['below_observe']}")
    print(f"正式名單：{len(formal_rows)}")
    print(f"觀察名單：{len(observe_rows)}")
    print(f"V型反轉名單：{len(v_rows)}（失敗型態另計：{stats['v_failed']}）")
    print(f"已寫入「{SHEET_SELECTION}」")
    print(f"已寫入「{SHEET_V_REVERSAL}」")

    return formal_rows, observe_rows


if __name__ == "__main__":
    formal, observe = run_main_scan()
    if formal:
        print("\nTop 10 正式候選股：")
        for i, row in enumerate(formal[:10], 1):
            print(f"{i}. {row[0]} {row[1]} | 分數 {row[14]} | {row[19]}")
    else:
        print("\n今日無正式進場候選股")

    if observe:
        print("\nTop 10 觀察名單：")
        for i, row in enumerate(observe[:10], 1):
            print(f"{i}. {row[0]} {row[1]} | 分數 {row[14]} | {row[22]}")
