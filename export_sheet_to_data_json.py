# -*- coding: utf-8 -*-
"""Export Google Sheet scan results to Cloudflare Pages data.json.

This script reads the sheet tabs written by the scanners and converts them to
frontend-friendly JSON for app.js.
"""

import datetime
import json
import os
from typing import Dict, List, Optional, Tuple

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ModuleNotFoundError:  # 允許純轉換測試在未安裝 Google 套件時執行
    gspread = None
    Credentials = None

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
SHEET_SELECTION = os.environ.get("EXPORT_SHEET_NAME", "選股結果")
OUTPUT_PATH = os.environ.get("DATA_JSON_PATH", "data.json")
CONTRARIAN_SHEET_CANDIDATES = [
    name.strip()
    for name in os.environ.get(
        "CONTRARIAN_SHEET_NAMES",
        "逆勢抗跌掃描,逆勢抗跌,抗跌掃描,Contrarian,Contrarian Scanner",
    ).split(",")
    if name.strip()
]
V_REVERSAL_SHEET_CANDIDATES = [
    name.strip()
    for name in os.environ.get(
        "V_REVERSAL_SHEET_NAMES",
        "V型反轉掃描,V型反轉,V Reversal",
    ).split(",")
    if name.strip()
]


def safe_text(value) -> str:
    return str(value).strip() if value is not None else ""


def parse_num(value) -> float:
    try:
        text = safe_text(value).replace(",", "").replace("%", "")
        return float(text) if text else 0.0
    except (TypeError, ValueError):
        return 0.0


def get_client():
    if gspread is None or Credentials is None:
        raise RuntimeError("請先安裝 gspread 與 google-auth")
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not SHEET_ID:
        raise RuntimeError("GOOGLE_SHEET_ID 環境變數未設定")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS 環境變數未設定")

    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def build_col_map(headers: List[str]) -> Dict[str, int]:
    aliases = {
        "code": ["代號", "股票代號", "證券代號", "code", "stock_id"],
        "name": ["名稱", "股票名稱", "證券名稱", "name", "stock_name"],
        "market": ["市場", "市場別", "market"],
        "industry": ["產業", "產業別", "industry"],
        "price": ["現價", "收盤價", "close", "price"],
        "bb_signal": ["BB訊號", "BB", "訊號"],
        "category": ["命中率", "分類", "category", "結果", "狀態"],
        "score": ["compositeScore", "totalScore", "score", "分數", "總分", "抗跌分數", "V分數"],
        "tech_score": ["techScore", "技術分"],
        "chips_score": ["chipsScore", "籌碼分"],
        "vol_score": ["volScore", "量能分"],
        "relative_score": ["relativeStrength", "相對強度", "抗跌", "抗跌分"],
        "market_light": ["大盤燈號", "marketLight", "燈號"],
        "badges": ["badges", "標籤", "條件"],
        "chips_detail": ["chipsDetail", "籌碼細節", "法人", "投信", "外資"],
        "block_reason": ["volDetail", "blockReason", "原因", "備註", "note"],
        "v_state": ["V狀態", "vState", "v_state"],
        "left_drop_pct": ["左臂跌幅", "leftDropPct", "left_drop_pct"],
        "rsi14": ["RSI14", "RSI", "rsi14"],
        "black_count": ["黑K數", "blackCount", "black_count"],
        "close_location": ["紅K收盤位置", "closeLocation", "close_location"],
        "upper_wick_ratio": ["上影占比", "upperWickRatio", "upper_wick_ratio"],
        "volume_ratio": ["量比", "volumeRatio", "volume_ratio"],
        "relative_strength": ["相對大盤", "relativeStrength", "relative_strength"],
        "institutional_signal": ["法人訊號", "institutionalSignal", "institutional_signal"],
        "left_peak": ["左臂高點", "leftPeak", "left_peak"],
        "v_bottom": ["V底", "vBottom", "v_bottom"],
        "trigger_mid": ["紅K中值", "triggerMid", "trigger_mid"],
        "v2_confirm": ["V2確認價", "v2Confirm", "v2_confirm"],
        "recover_50": ["50%收復價", "recover50", "recover_50"],
        "recover_618": ["61.8%收復價", "recover618", "recover_618"],
        "invalid_price": ["失效價", "invalidPrice", "invalid_price"],
        "trigger_date": ["轉折日", "triggerDate", "trigger_date"],
    }

    result = {}
    for i, header in enumerate(headers):
        h = safe_text(header)
        lower_h = h.lower()
        for key, names in aliases.items():
            for name in names:
                if h == name or lower_h == name.lower() or (len(name) >= 3 and name in h):
                    result.setdefault(key, i)
                    break
    return result


def extract_sheet_meta(values: List[List[str]], header_row_index: int) -> Dict[str, str]:
    meta = {}
    for row in values[:header_row_index]:
        for cell in row:
            text = safe_text(cell)
            separator = "：" if "：" in text else ":" if ":" in text else ""
            if not separator:
                continue
            key, value = text.split(separator, 1)
            key = safe_text(key)
            value = safe_text(value)
            if key and value:
                meta[key] = value
    return meta


def find_header_row(values: List[List[str]], sheet_name: str) -> Tuple[int, List[str], Dict[str, int]]:
    for row_index, row in enumerate(values[:40]):
        col = build_col_map(row)
        if "code" in col:
            return row_index, row, col

    preview = [row for row in values[:5] if any(safe_text(cell) for cell in row)]
    raise RuntimeError(f"無法解析 {sheet_name} 表頭：{preview}")


def get_cell(row: List[str], col: Dict[str, int], key: str, default: str = "") -> str:
    idx = col.get(key)
    if idx is None or idx >= len(row):
        return default
    return safe_text(row[idx])


def frontend_signal(category: str, badges: str, score: float, mode: str = "main") -> str:
    text = f"{category} {badges}"
    if mode == "v_reversal":
        if "VX" in text:
            return "risk"
        if "V1" in text or "V2" in text:
            return "strong"
        return "watch"

    if mode == "contrarian":
        if "紅" in text or "淘汰" in text or score < 40:
            return "risk"
        if "綠" in text or "強" in text or "通過" in text or score >= 70:
            return "strong"
        return "watch"

    if "正式" in text or "進場" in text:
        return "strong"
    if "淘汰" in text or score < 30:
        return "risk"
    return "watch"


def build_note(row: List[str], col: Dict[str, int], mode: str, defaults: Optional[Dict[str, str]] = None) -> str:
    defaults = defaults or {}
    keys = ["bb_signal", "badges", "chips_detail", "block_reason"]
    if mode == "contrarian":
        keys = ["market_light", "badges", "chips_detail", "block_reason", "bb_signal"]
    elif mode == "v_reversal":
        keys = ["v_state", "badges", "institutional_signal", "block_reason"]

    parts = []
    for key in keys:
        value = get_cell(row, col, key)
        if not value and key == "market_light":
            value = defaults.get("大盤燈號", "")
        if value:
            parts.append(value)
    return "；".join(parts) if parts else "Google Sheet 掃描結果"


def row_to_stock(row: List[str], col: Dict[str, int], mode: str, defaults: Optional[Dict[str, str]] = None) -> Optional[Dict]:
    defaults = defaults or {}
    code = get_cell(row, col, "code")
    if not code:
        return None

    score = parse_num(get_cell(row, col, "score"))
    category = get_cell(row, col, "v_state") if mode == "v_reversal" else get_cell(row, col, "category")
    badges = get_cell(row, col, "badges")
    market_light = get_cell(row, col, "market_light") or defaults.get("大盤燈號", "")
    stock = {
        "code": code,
        "name": get_cell(row, col, "name") or code,
        "signal": frontend_signal(category, badges, score, mode),
        "score": score,
        "note": build_note(row, col, mode, defaults),
        "mode": mode,
        "market": get_cell(row, col, "market"),
        "industry": get_cell(row, col, "industry"),
        "price": parse_num(get_cell(row, col, "price")),
        "category": category,
        "badges": badges,
        "market_light": market_light,
        "tech_score": parse_num(get_cell(row, col, "tech_score")),
        "chips_score": parse_num(get_cell(row, col, "chips_score")),
        "vol_score": parse_num(get_cell(row, col, "vol_score")),
        "relative_score": parse_num(get_cell(row, col, "relative_score")),
    }
    if mode == "v_reversal":
        stock.update({
            "v_state": category,
            "left_drop_pct": parse_num(get_cell(row, col, "left_drop_pct")),
            "rsi14": parse_num(get_cell(row, col, "rsi14")),
            "black_count": int(parse_num(get_cell(row, col, "black_count"))),
            "close_location": parse_num(get_cell(row, col, "close_location")),
            "upper_wick_ratio": parse_num(get_cell(row, col, "upper_wick_ratio")),
            "volume_ratio": parse_num(get_cell(row, col, "volume_ratio")),
            "relative_strength": parse_num(get_cell(row, col, "relative_strength")),
            "institutional_signal": get_cell(row, col, "institutional_signal"),
            "left_peak": parse_num(get_cell(row, col, "left_peak")),
            "v_bottom": parse_num(get_cell(row, col, "v_bottom")),
            "trigger_mid": parse_num(get_cell(row, col, "trigger_mid")),
            "v2_confirm": parse_num(get_cell(row, col, "v2_confirm")),
            "recover_50": parse_num(get_cell(row, col, "recover_50")),
            "recover_618": parse_num(get_cell(row, col, "recover_618")),
            "invalid_price": parse_num(get_cell(row, col, "invalid_price")),
            "trigger_date": get_cell(row, col, "trigger_date"),
        })
    return stock


def read_sheet_rows(sh, sheet_name: str, mode: str) -> Tuple[List[Dict], Optional[str]]:
    ws = sh.worksheet(sheet_name)
    values = ws.get_all_values()
    if len(values) < 2:
        return [], sheet_name

    header_row_index, headers, col = find_header_row(values, sheet_name)
    meta = extract_sheet_meta(values, header_row_index)

    stocks = []
    for row in values[header_row_index + 1:]:
        stock = row_to_stock(row, col, mode, meta)
        if stock:
            stocks.append(stock)
    return stocks, sheet_name


def read_optional_first_sheet(sh, sheet_names: List[str], mode: str) -> Tuple[List[Dict], Optional[str]]:
    for sheet_name in sheet_names:
        try:
            return read_sheet_rows(sh, sheet_name, mode)
        except Exception as exc:
            if gspread is not None and isinstance(exc, gspread.exceptions.WorksheetNotFound):
                continue
            if isinstance(exc, RuntimeError):
                print(f"Warning: {exc}")
                return [], sheet_name
            raise
    return [], None


def export_data_json():
    gc = get_client()
    sh = gc.open_by_key(SHEET_ID)
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    main_stocks, main_tab = read_sheet_rows(sh, SHEET_SELECTION, "main")
    contrarian_stocks, contrarian_tab = read_optional_first_sheet(sh, CONTRARIAN_SHEET_CANDIDATES, "contrarian")
    v_reversal_stocks, v_reversal_tab = read_optional_first_sheet(sh, V_REVERSAL_SHEET_CANDIDATES, "v_reversal")

    payload = {
        "generated_at": generated_at,
        "source": "Google Sheet",
        "sheet_id": SHEET_ID,
        "sheet_tab": main_tab,
        "contrarian_sheet_tab": contrarian_tab,
        "v_reversal_sheet_tab": v_reversal_tab,
        "stocks": main_stocks,
        "contrarian_stocks": contrarian_stocks,
        "v_reversal_stocks": v_reversal_stocks,
        "datasets": {
            "main": {
                "label": "主升段",
                "sheet_tab": main_tab,
                "count": len(main_stocks),
            },
            "contrarian": {
                "label": "逆勢抗跌",
                "sheet_tab": contrarian_tab,
                "count": len(contrarian_stocks),
            },
            "v_reversal": {
                "label": "V型反轉",
                "sheet_tab": v_reversal_tab,
                "count": len(v_reversal_stocks),
            },
        },
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"Exported {len(main_stocks)} main stocks to {OUTPUT_PATH}")
    if contrarian_tab:
        print(f"Exported {len(contrarian_stocks)} contrarian stocks from {contrarian_tab}")
    else:
        print("No contrarian sheet found; exported empty contrarian_stocks")
    if v_reversal_tab:
        print(f"Exported {len(v_reversal_stocks)} V-reversal stocks from {v_reversal_tab}")
    else:
        print("No V-reversal sheet found; exported empty v_reversal_stocks")


if __name__ == "__main__":
    export_data_json()
