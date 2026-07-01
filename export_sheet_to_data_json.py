# -*- coding: utf-8 -*-
"""Export Google Sheet scan results to Cloudflare Pages data.json.

This script reads the sheet written by main_stock_scanner.py and converts it to
frontend-friendly JSON for app.js.
"""

import datetime
import json
import os
from typing import Dict, List

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
SHEET_SELECTION = os.environ.get("EXPORT_SHEET_NAME", "選股結果")
OUTPUT_PATH = os.environ.get("DATA_JSON_PATH", "data.json")


def safe_text(value) -> str:
    return str(value).strip() if value is not None else ""


def parse_num(value) -> float:
    try:
        text = safe_text(value).replace(",", "").replace("%", "")
        return float(text) if text else 0.0
    except (TypeError, ValueError):
        return 0.0


def get_client():
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
        "code": ["代號", "股票代號", "證券代號"],
        "name": ["名稱", "股票名稱", "證券名稱"],
        "market": ["市場", "市場別"],
        "industry": ["產業", "產業別"],
        "price": ["現價", "收盤價", "close"],
        "bb_signal": ["BB訊號"],
        "category": ["命中率", "分類", "category"],
        "score": ["compositeScore", "score", "分數"],
        "tech_score": ["techScore"],
        "chips_score": ["chipsScore"],
        "vol_score": ["volScore"],
        "badges": ["badges", "標籤"],
        "chips_detail": ["chipsDetail", "籌碼細節"],
        "block_reason": ["volDetail", "blockReason", "原因"],
    }

    result = {}
    for i, header in enumerate(headers):
        h = safe_text(header)
        for key, names in aliases.items():
            if h in names or any(name in h for name in names if len(name) >= 3):
                result.setdefault(key, i)
    return result


def get_cell(row: List[str], col: Dict[str, int], key: str, default: str = "") -> str:
    idx = col.get(key)
    if idx is None or idx >= len(row):
        return default
    return safe_text(row[idx])


def frontend_signal(category: str, badges: str, score: float) -> str:
    text = f"{category} {badges}"
    if "正式" in text or "進場" in text:
        return "strong"
    if "淘汰" in text or score < 30:
        return "risk"
    return "watch"


def build_note(row: List[str], col: Dict[str, int]) -> str:
    parts = []
    for key in ("bb_signal", "badges", "chips_detail", "block_reason"):
        value = get_cell(row, col, key)
        if value:
            parts.append(value)
    return "；".join(parts) if parts else "Google Sheet 掃描結果"


def export_data_json():
    gc = get_client()
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(SHEET_SELECTION)
    values = ws.get_all_values()

    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if len(values) < 2:
        payload = {
            "generated_at": generated_at,
            "source": "Google Sheet",
            "sheet_id": SHEET_ID,
            "sheet_tab": SHEET_SELECTION,
            "stocks": [],
        }
    else:
        headers = values[0]
        col = build_col_map(headers)
        if "code" not in col:
            raise RuntimeError(f"無法解析 {SHEET_SELECTION} 表頭：{headers}")

        stocks = []
        for row in values[1:]:
            code = get_cell(row, col, "code")
            if not code:
                continue

            score = parse_num(get_cell(row, col, "score"))
            category = get_cell(row, col, "category")
            badges = get_cell(row, col, "badges")
            stocks.append({
                "code": code,
                "name": get_cell(row, col, "name") or code,
                "signal": frontend_signal(category, badges, score),
                "score": score,
                "note": build_note(row, col),
                "market": get_cell(row, col, "market"),
                "industry": get_cell(row, col, "industry"),
                "price": parse_num(get_cell(row, col, "price")),
                "category": category,
                "badges": badges,
                "tech_score": parse_num(get_cell(row, col, "tech_score")),
                "chips_score": parse_num(get_cell(row, col, "chips_score")),
                "vol_score": parse_num(get_cell(row, col, "vol_score")),
            })

        payload = {
            "generated_at": generated_at,
            "source": "Google Sheet",
            "sheet_id": SHEET_ID,
            "sheet_tab": SHEET_SELECTION,
            "stocks": stocks,
        }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"Exported {len(payload['stocks'])} stocks to {OUTPUT_PATH}")


if __name__ == "__main__":
    export_data_json()
