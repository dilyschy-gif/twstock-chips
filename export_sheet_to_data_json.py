# -*- coding: utf-8 -*-
"""Export Google Sheet scan results to Cloudflare Pages data.json.

This script reads the sheet tabs written by the scanners and converts them to
frontend-friendly JSON for app.js.

2026-08 修正（BUG-1 資料時間戳）
────────────────────────────────
問題：app.js 的「最後更新」顯示 `new Date()`，那是使用者開網頁的時間，
      不是資料的時間。data.json 就算三天沒更新，畫面也永遠顯示「剛剛」，
      等於把資料管線中斷這件事藏起來。

本檔負責產出時間戳的「資料端」，共三個欄位：

  generated_at         匯出當下的 UTC ISO 時間（原本就有，保留不動，向後相容）
  generated_at_taipei  同一時刻的台北時間可讀字串，例如 2026/08/01 09:12
  data_date            **資料實際對應的交易日**，例如 2026-07-31

前兩者回答「這份 JSON 什麼時候產的」，第三個回答「這份 JSON 講的是哪一天的盤」。
兩者會不一樣：週六早上重跑匯出，generated_at 是週六，data_date 仍是週五。
前端要判斷新鮮度，看的是 data_date。

data_date 從哪來：
  主升段／逆勢：chipsDetail 欄位開頭的日期，格式如
                「2026-07-31 三法人合計1667張；...」
  V型反轉：     轉折日欄位
取該分頁所有列的最大值。若整頁都解析不到日期，回傳空字串，前端會顯示「未知」
而不是假裝知道。
"""

import datetime
import json
import os
import re
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
TAIPEI_TZ = datetime.timezone(datetime.timedelta(hours=8))

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

# 接受 2026-07-31 / 2026/07/31 / 2026.07.31 三種寫法，取第一個出現的日期。
DATE_PATTERN = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")


def safe_text(value) -> str:
    return str(value).strip() if value is not None else ""


def parse_num(value) -> float:
    try:
        text = safe_text(value).replace(",", "").replace("%", "")
        return float(text) if text else 0.0
    except (TypeError, ValueError):
        return 0.0


def parse_any_date(value) -> str:
    """從任意字串抽出第一個日期，正規化成 YYYY-MM-DD；找不到回傳空字串。

    用於 chipsDetail（開頭是日期）與轉折日（整格就是日期）。
    會驗證日期真實存在，避免把 2026-13-45 這種雜訊當成日期。
    """
    text = safe_text(value)
    if not text:
        return ""

    match = DATE_PATTERN.search(text)
    if not match:
        return ""

    year, month, day = (int(part) for part in match.groups())
    try:
        return datetime.date(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


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
        # 退場參考價位（2026-08 新增）。這兩個值主掃描一直都有算、也一直寫在
        # 試算表裡，只是從來沒被帶到前端。n_target 是停利位、start_price 是停損位。
        "n_target": ["N字目標", "nTarget", "n_target"],
        "start_price": ["起漲點", "startPrice", "start_price"],
        # 「分類」是 2026-08 起 main_stock_scanner.py 使用的正式標題。
        # 「命中率」是修正前的誤植標題，保留在別名中，讓尚未重跑主掃描的
        # 舊資料仍可正確解析（重跑後標題會自動換成「分類」）。
        "category": ["分類", "命中率", "category", "結果", "狀態"],
        "score": ["compositeScore", "totalScore", "score", "分數", "總分", "抗跌分數", "V分數"],
        "tech_score": ["techScore", "技術分"],
        "chips_score": ["chipsScore", "籌碼分"],
        "vol_score": ["volScore", "量能分"],
        "relative_score": ["relativeStrength", "相對強度", "抗跌", "抗跌分"],
        "market_light": ["大盤燈號", "marketLight", "燈號"],
        "badges": ["badges", "標籤", "條件"],
        "chips_detail": ["chipsDetail", "籌碼細節", "法人", "投信", "外資"],
        # 同上：「備註」為正式標題，「volDetail」為修正前誤植標題，保留相容。
        "block_reason": ["備註", "volDetail", "blockReason", "原因", "note"],
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


def row_data_date(row: List[str], col: Dict[str, int], mode: str) -> str:
    """單列的資料日。主升段／逆勢看 chipsDetail 開頭日期，V型看轉折日。"""
    if mode == "v_reversal":
        return parse_any_date(get_cell(row, col, "trigger_date")) or \
            parse_any_date(get_cell(row, col, "chips_detail"))
    return parse_any_date(get_cell(row, col, "chips_detail")) or \
        parse_any_date(get_cell(row, col, "trigger_date"))


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
        # 逐檔資料日：讓前端可以標出「這一檔的籌碼掛的是舊日期」，
        # 對應 README 檢驗流程第 4 步「新鮮度」。
        "data_date": row_data_date(row, col, mode),
        # 退場參考價位。注意這是「價位」不是「訊號」——程式不會叫你賣，
        # 只是把一直存在試算表裡的兩個數字帶到看得見的地方。
        "n_target": parse_num(get_cell(row, col, "n_target")),
        "start_price": parse_num(get_cell(row, col, "start_price")),
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


def read_sheet_rows(sh, sheet_name: str, mode: str) -> Tuple[List[Dict], Optional[str], str]:
    """回傳 (股票清單, 分頁名稱, 該分頁資料日)。"""
    ws = sh.worksheet(sheet_name)
    values = ws.get_all_values()
    if len(values) < 2:
        return [], sheet_name, ""

    header_row_index, headers, col = find_header_row(values, sheet_name)
    meta = extract_sheet_meta(values, header_row_index)

    stocks = []
    for row in values[header_row_index + 1:]:
        stock = row_to_stock(row, col, mode, meta)
        if stock:
            stocks.append(stock)

    dates = [stock.get("data_date", "") for stock in stocks if stock.get("data_date")]
    data_date = max(dates) if dates else ""
    return stocks, sheet_name, data_date


def read_optional_first_sheet(sh, sheet_names: List[str], mode: str) -> Tuple[List[Dict], Optional[str], str]:
    for sheet_name in sheet_names:
        try:
            return read_sheet_rows(sh, sheet_name, mode)
        except Exception as exc:
            if gspread is not None and isinstance(exc, gspread.exceptions.WorksheetNotFound):
                continue
            if isinstance(exc, RuntimeError):
                print(f"Warning: {exc}")
                return [], sheet_name, ""
            raise
    return [], None, ""


def export_data_json():
    gc = get_client()
    sh = gc.open_by_key(SHEET_ID)

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    generated_at = now_utc.isoformat()
    generated_at_taipei = now_utc.astimezone(TAIPEI_TZ).strftime("%Y/%m/%d %H:%M")

    main_stocks, main_tab, main_date = read_sheet_rows(sh, SHEET_SELECTION, "main")
    contrarian_stocks, contrarian_tab, contrarian_date = read_optional_first_sheet(
        sh, CONTRARIAN_SHEET_CANDIDATES, "contrarian"
    )
    v_reversal_stocks, v_reversal_tab, v_reversal_date = read_optional_first_sheet(
        sh, V_REVERSAL_SHEET_CANDIDATES, "v_reversal"
    )

    # 全域 data_date 以主升段為準；主升段沒抓到才退回其他分頁。
    data_date = main_date or v_reversal_date or contrarian_date or ""

    payload = {
        "generated_at": generated_at,
        "generated_at_taipei": generated_at_taipei,
        "data_date": data_date,
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
                "data_date": main_date,
            },
            "contrarian": {
                "label": "逆勢抗跌",
                "sheet_tab": contrarian_tab,
                "count": len(contrarian_stocks),
                "data_date": contrarian_date,
            },
            "v_reversal": {
                "label": "V型反轉",
                "sheet_tab": v_reversal_tab,
                "count": len(v_reversal_stocks),
                "data_date": v_reversal_date,
            },
        },
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"Exported {len(main_stocks)} main stocks to {OUTPUT_PATH}")
    print(f"資料日 data_date：{data_date or '（未解析到，前端會顯示未知）'}")
    print(f"匯出時間 generated_at_taipei：{generated_at_taipei}")
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
