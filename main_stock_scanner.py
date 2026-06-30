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

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
SHEET_STOCK_DB = "股票資料庫"
SHEET_CHIPS = "籌碼面資料"
SHEET_SCAN_RESULT = "掃描結果"
SHEET_SELECTION = "選股結果"
SHEET_PROGRESS = "掃描進度"

MIN_VOLUME_LOTS = 300
FORMAL_THRESHOLD = 60
OBSERVE_THRESHOLD = 30
REQUEST_SLEEP_SECONDS = 0.12

OUTPUT_HEADERS = [
    "代號", "名稱", "市場", "產業", "現價", "BB訊號", "N字目標", "起漲點", "帶寬",
    "K值", "D值", "量比", "量能訊號", "命中率", "compositeScore", "techScore",
    "chipsScore", "usScore", "volScore", "badges", "chipsDetail", "usDetail", "volDetail"
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


def parse_num(value) -> float:
    try:
        text = safe_text(value).replace(",", "").replace("%", "")
        return float(text) if text else 0.0
    except (TypeError, ValueError):
        return 0.0


def normalize_code(value) -> str:
    code = safe_text(value).replace(".TW", "").replace(".TWO", "")
    return code.zfill(4) if code.isdigit() and len(code) < 4 else code


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
            "market": safe_text(row[col.get("market", -1)]) if col.get("market", -1) < len(row) else "上市",
            "code": code,
            "name": safe_text(row[col.get("name", -1)]) if col.get("name", -1) < len(row) else "",
            "industry": safe_text(row[col.get("industry", -1)]) if col.get("industry", -1) < len(row) else "",
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
        })

    result = {}
    positive_latest_count = 0
    for code, entries in by_code.items():
        entries.sort(key=lambda x: x["date_key"], reverse=True)
        trust_streak = count_positive_streak(entries, "trust")
        foreign_streak = count_positive_streak(entries, "foreign")
        total_streak = count_positive_streak(entries, "total")
        latest = entries[0] if entries else {}
        latest_total = latest.get("total", 0)
        if latest_total > 0:
            positive_latest_count += 1

        chips_score = 0
        if latest_total > 0:
            chips_score += 10
        if trust_streak >= 15:
            chips_score += 35
        elif trust_streak >= 10:
            chips_score += 28
        elif trust_streak >= 5:
            chips_score += 20
        elif trust_streak >= 3:
            chips_score += 12
        elif trust_streak >= 1:
            chips_score += 5
        elif foreign_streak >= 5:
            chips_score += 10
        elif foreign_streak >= 3:
            chips_score += 6

        result[code] = {
            "latest_total": latest_total,
            "latest_foreign": latest.get("foreign", 0),
            "latest_trust": latest.get("trust", 0),
            "latest_dealer": latest.get("dealer", 0),
            "latest_chip_date": latest.get("date", ""),
            "trust_streak": trust_streak,
            "foreign_streak": foreign_streak,
            "total_streak": total_streak,
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
                "date": datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
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


def calc_kd(highs: List[float], lows: List[float], closes: List[float]) -> Tuple[float, float]:
    rsv_values = []
    for end in range(max(9, len(closes) - 5), len(closes) + 1):
        window_high = max(highs[end - 9:end])
        window_low = min(lows[end - 9:end])
        close = closes[end - 1]
        rsv = 50 if window_high == window_low else (close - window_low) / (window_high - window_low) * 100
        rsv_values.append(rsv)

    if not rsv_values:
        return 50.0, 50.0
    k = sum(rsv_values[-3:]) / min(3, len(rsv_values))
    d = sum(rsv_values[-5:]) / min(5, len(rsv_values))
    return round(k, 2), round(d, 2)


def calc_n_structure(highs: List[float], lows: List[float], closes: List[float]) -> Dict:
    close = closes[-1]
    prior_20_high = max(highs[-21:-1]) if len(highs) >= 21 else max(highs[:-1])
    prior_60_high = max(highs[-61:-1]) if len(highs) >= 61 else max(highs[:-1])
    recent_low_window = lows[-45:-5] if len(lows) >= 45 else lows[:-5] or lows[:-1]
    start_price = min(recent_low_window) if recent_low_window else min(lows)

    neckline = max(prior_20_high, prior_60_high)
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


def write_table(sh, title: str, rows: List[List]):
    ws = worksheet_or_create(sh, title, rows=max(len(rows) + 10, 1000), cols=len(OUTPUT_HEADERS) + 5)
    ws.clear()
    values = [OUTPUT_HEADERS] + rows
    ws.update(range_name="A1", values=values)
    ws.freeze(rows=1)


def write_progress(sh, status: str, start_index: int, passed_count: int):
    ws = worksheet_or_create(sh, SHEET_PROGRESS, rows=20, cols=5)
    now = datetime.datetime.now().strftime("%Y/%m/%d %H:%M:%S")
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
    stock_codes = {stock["code"] for stock in stocks}
    matched_chip_codes = stock_codes.intersection(set(chips.keys()))
    positive_chip_codes = {code for code in matched_chip_codes if chips.get(code, {}).get("latest_total", 0) > 0}
    print(f"[DEBUG] 股票資料庫對到籌碼：{len(matched_chip_codes)}/{len(stock_codes)} 檔")
    print(f"[DEBUG] 本次掃描範圍最近一筆三法人合計買超：{len(positive_chip_codes)} 檔")

    formal_rows = []
    observe_rows = []
    stats = {
        "processed": 0,
        "no_daily": 0,
        "low_volume": 0,
        "no_inst_buy": 0,
        "no_breakout": 0,
        "below_observe": 0,
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
            print(f"已處理 {idx}/{len(stocks)} 檔；正式 {len(formal_rows)}；觀察 {len(observe_rows)}")
            write_progress(sh, "執行中", start_index, len(formal_rows) + len(observe_rows))

        time.sleep(REQUEST_SLEEP_SECONDS)

    all_rows = formal_rows + observe_rows
    all_rows.sort(key=lambda row: parse_num(row[14]), reverse=True)

    write_table(sh, SHEET_SCAN_RESULT, all_rows)
    write_table(sh, SHEET_SELECTION, all_rows)
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
    print(f"已寫入「{SHEET_SCAN_RESULT}」與「{SHEET_SELECTION}」")

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
