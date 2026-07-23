#!/usr/bin/env python3
"""Fetch the latest two disclosed portfolios and report constituent changes."""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ENDPOINT = "https://www.cmoney.tw/MobileService/ashx/GetDtnoData.ashx"
OUTPUT_PATH = Path(__file__).with_name("active-etf.json")
TAIPEI = ZoneInfo("Asia/Taipei")

ETF_CONFIG = [
    {
        "code": "00981A",
        "name": "主動統一台股增長",
        "official_url": "https://www.ezmoney.com.tw/ETF/Transaction/PCF",
    },
    {
        "code": "00991A",
        "name": "主動復華未來50",
        "official_url": "https://www.fhtrust.com.tw/ETF/trade_list",
    },
    {
        "code": "00992A",
        "name": "主動群益科技創新",
        "official_url": "https://www.capitalfund.com.tw/etf/product/detail/500/portfolio",
    },
    {
        "code": "00988A",
        "name": "主動統一全球創新",
        "official_url": "https://www.ezmoney.com.tw/ETF/Transaction/PCF",
    },
    {
        "code": "00980A",
        "name": "主動野村臺灣優選",
        "official_url": "https://www.nomurafunds.com.tw/ETFWEB/pcf",
    },
]

MARKET_LABELS = {
    "TW": "台灣",
    "US": "美國",
    "JP": "日本",
    "CH": "中國",
    "HK": "香港",
    "KS": "韓國",
    "KQ": "韓國",
    "GY": "德國",
    "LN": "英國",
    "FP": "法國",
    "NA": "荷蘭",
    "SW": "瑞士",
    "AU": "澳洲",
    "SP": "新加坡",
}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _to_int(value: Any) -> int:
    return int(float(str(value).replace(",", "").strip()))


def market_from_symbol(symbol: str) -> tuple[str, str]:
    parts = symbol.upper().split()
    market_code = parts[-1] if len(parts) > 1 and parts[-1] in MARKET_LABELS else "TW"
    return market_code, MARKET_LABELS[market_code]


def lots_for(market_code: str, shares: int) -> int | float | None:
    if market_code != "TW":
        return None
    lots = round(shares / 1000, 3)
    return int(lots) if lots.is_integer() else lots


def _holding_from_row(row: list[Any], indexes: dict[str, int]) -> dict[str, Any] | None:
    unit = _clean_text(row[indexes["單位"]])
    if unit != "股":
        return None

    shares = _to_int(row[indexes["持有數"]])
    if shares <= 0:
        return None

    symbol = _clean_text(row[indexes["標的代號"]]).upper()
    if not symbol:
        return None

    market_code, market = market_from_symbol(symbol)
    return {
        "symbol": symbol,
        "name": _clean_text(row[indexes["標的名稱"]]) or symbol,
        "market_code": market_code,
        "market": market,
        "shares": shares,
        "lots": lots_for(market_code, shares),
        "unit": unit,
    }


def build_report(
    etf_code: str,
    etf_name: str,
    official_url: str,
    title: list[str],
    rows: list[list[Any]],
) -> dict[str, Any]:
    required = {"日期", "標的代號", "標的名稱", "持有數", "單位"}
    indexes = {name: index for index, name in enumerate(title)}
    missing = required - indexes.keys()
    if missing:
        raise ValueError(f"{etf_code} 資料缺少欄位：{', '.join(sorted(missing))}")

    portfolios: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        if len(row) < len(title):
            continue
        holding = _holding_from_row(row, indexes)
        if not holding:
            continue
        date = _clean_text(row[indexes["日期"]])
        portfolios.setdefault(date, {})[holding["symbol"]] = holding

    dates = sorted(portfolios)
    if len(dates) < 2:
        raise ValueError(f"{etf_code} 未取得最近兩個交易日的完整持股")

    previous_date, data_date = dates[-2], dates[-1]
    previous = portfolios[previous_date]
    current = portfolios[data_date]
    added_symbols = sorted(current.keys() - previous.keys())
    removed_symbols = sorted(previous.keys() - current.keys())

    return {
        "code": etf_code,
        "name": etf_name,
        "official_url": official_url,
        "data_date": data_date,
        "previous_date": previous_date,
        "current_holdings_count": len(current),
        "added": [current[symbol] for symbol in added_symbols],
        "removed": [previous[symbol] for symbol in removed_symbols],
    }


def _request_url(etf_code: str) -> str:
    params = {
        "action": "getdtnodata",
        "DtNo": "59449513",
        "ParamStr": (
            f"AssignID={etf_code};MTPeriod=0;DTMode=0;"
            "DTRange=2;DTOrder=1;MajorTable=M722;"
        ),
        "FilterNo": "0",
    }
    return ENDPOINT + "?" + urllib.parse.urlencode(params)


def fetch_etf(config: dict[str, str], retries: int = 3) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                _request_url(config["code"]),
                headers={
                    "Accept": "application/json",
                    "User-Agent": "twstock-chips active-etf tracker/1.0",
                },
            )
            with urllib.request.urlopen(request, timeout=35) as response:
                payload = json.load(response)
            title = payload.get("Title")
            rows = payload.get("Data")
            if not isinstance(title, list) or not isinstance(rows, list):
                raise ValueError("回傳資料格式不正確")
            return build_report(
                config["code"],
                config["name"],
                config["official_url"],
                title,
                rows,
            )
        except Exception as error:  # pragma: no cover - network retry path
            last_error = error
            if attempt < retries:
                time.sleep(attempt * 2)
    raise RuntimeError(f"{config['code']} 抓取失敗：{last_error}") from last_error


def build_payload() -> dict[str, Any]:
    reports = []
    errors = []
    for config in ETF_CONFIG:
        try:
            reports.append(fetch_etf(config))
        except Exception as error:
            errors.append(str(error))

    if errors:
        raise RuntimeError("；".join(errors))

    return {
        "generated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "source": {
            "name": "CMoney 公開ETF持股資料",
            "url": ENDPOINT,
            "note": "重要變動請以各投信官方每日投資組合公告為準。",
        },
        "etfs": reports,
    }


def _same_data(existing: dict[str, Any], new_payload: dict[str, Any]) -> bool:
    return (
        isinstance(existing.get("history"), list)
        and bool(existing["history"])
        and existing.get("source") == new_payload.get("source")
        and existing.get("etfs") == new_payload.get("etfs")
    )


def merge_history(payload: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    history = existing.get("history")
    if not isinstance(history, list):
        history = []

    batch_date = max(report["data_date"] for report in payload["etfs"])
    entry = {
        "batch_date": batch_date,
        "generated_at": payload["generated_at"],
        "etfs": payload["etfs"],
    }
    history = [
        item for item in history
        if isinstance(item, dict) and item.get("batch_date") != batch_date
    ]
    history.append(entry)
    history.sort(key=lambda item: item.get("batch_date", ""))
    merged["history"] = history[-90:]
    return merged


def write_payload(payload: dict[str, Any], output_path: Path = OUTPUT_PATH) -> bool:
    existing: dict[str, Any] = {}
    if output_path.exists():
        try:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
            if _same_data(existing, payload):
                print("ETF持股資料日期與內容未變，略過寫入。")
                return False
        except (OSError, json.JSONDecodeError):
            existing = {}

    payload = merge_history(payload, existing)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def main() -> int:
    try:
        payload = build_payload()
        changed = write_payload(payload)
        for report in payload["etfs"]:
            print(
                f"{report['code']} {report['previous_date']}→{report['data_date']}："
                f"新增 {len(report['added'])}、剔除 {len(report['removed'])}"
            )
        print("已更新 active-etf.json" if changed else "active-etf.json 無需更新")
        return 0
    except Exception as error:
        print(f"主動ETF持股更新失敗：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
