#!/usr/bin/env python3
"""Build an active-ETF manager radar from daily disclosed portfolios."""

from __future__ import annotations

import concurrent.futures
import gzip
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


TWSE_ACTIVE_URL = "https://www.twse.com.tw/rwd/zh/ETF/activeList?response=json"
CMONEY_ENDPOINT = "https://www.cmoney.tw/MobileService/ashx/GetDtnoData.ashx"
OUTPUT_PATH = Path(__file__).with_name("active-etf.json")
SNAPSHOT_PATH = Path(__file__).with_name("active-etf-snapshots.json.gz")
TAIPEI = ZoneInfo("Asia/Taipei")

FETCH_RANGE = 6
MAX_SNAPSHOT_DATES = 30
MAX_HISTORY_BATCHES = 90
MIN_WEIGHT_CHANGE_PP = 0.10
STRONG_WEIGHT_CHANGE_PP = 0.30
STREAK_NOISE_PP = 0.02

PRIORITY_CODES = {"00981A", "00991A", "00992A", "00988A", "00980A"}

MANAGER_MARKERS = (
    "第一金",
    "國泰",
    "野村",
    "群益",
    "統一",
    "中信",
    "安聯",
    "台新",
    "富邦",
    "摩根",
    "元大",
    "復華",
    "兆豐",
    "聯博",
    "凱基",
)

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


def _to_float(value: Any) -> float:
    text = str(value or "0").replace(",", "").replace("%", "").strip()
    return round(float(text or 0), 4)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                value = json.load(stream)
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, EOFError, json.JSONDecodeError):
        return {}


def _request_json(url: str, retries: int = 3) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "twstock-chips active-etf manager radar/2.0",
                },
            )
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise ValueError("回傳內容不是JSON物件")
            return payload
        except Exception as error:  # pragma: no cover - network retry path
            last_error = error
            if attempt < retries:
                time.sleep(attempt * 2)
    raise RuntimeError(f"資料抓取失敗：{last_error}") from last_error


def manager_from_name(name: str) -> str:
    for marker in MANAGER_MARKERS:
        if marker in name:
            return marker
    return _clean_text(name).removeprefix("主動")[:3] or "其他"


def fetch_universe(cached: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    try:
        payload = _request_json(TWSE_ACTIVE_URL)
        fields = payload.get("fields")
        rows = payload.get("data")
        if not isinstance(fields, list) or not isinstance(rows, list):
            raise ValueError("TWSE主動ETF清單格式不正確")
        indexes = {name: index for index, name in enumerate(fields)}
        required = {"證券代號", "證券簡稱", "ETF分類"}
        if required - indexes.keys():
            raise ValueError("TWSE主動ETF清單缺少必要欄位")

        funds = []
        for row in rows:
            if len(row) < len(fields):
                continue
            code = _clean_text(row[indexes["證券代號"]]).upper()
            if not code.endswith("A"):
                continue
            name = _clean_text(row[indexes["證券簡稱"]])
            category = _clean_text(row[indexes["ETF分類"]])
            funds.append(
                {
                    "code": code,
                    "name": name,
                    "manager": manager_from_name(name),
                    "category": category,
                    "region": "台股型" if category == "domestic" else "海外型",
                    "priority": code in PRIORITY_CODES,
                    "official_url": (
                        "https://www.twse.com.tw/zh/products/securities/etf/products/"
                        f"active-list/content.html?{code}#{category}"
                    ),
                }
            )
        if not funds:
            raise ValueError("TWSE未回傳股票型主動ETF")
        return sorted(funds, key=lambda fund: (not fund["priority"], fund["code"]))
    except Exception:
        if cached:
            return cached
        raise


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
        "weight": _to_float(row[indexes["權重(%)"]]),
        "unit": unit,
    }


def parse_portfolios(
    title: list[str],
    rows: list[list[Any]],
    etf_code: str = "ETF",
) -> dict[str, dict[str, dict[str, Any]]]:
    required = {"日期", "標的代號", "標的名稱", "權重(%)", "持有數", "單位"}
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
        if date:
            portfolios.setdefault(date, {})[holding["symbol"]] = holding
    return portfolios


def _request_url(etf_code: str, date_range: int = FETCH_RANGE) -> str:
    params = {
        "action": "getdtnodata",
        "DtNo": "59449513",
        "ParamStr": (
            f"AssignID={etf_code};MTPeriod=0;DTMode=0;"
            f"DTRange={date_range};DTOrder=1;MajorTable=M722;"
        ),
        "FilterNo": "0",
    }
    return CMONEY_ENDPOINT + "?" + urllib.parse.urlencode(params)


def fetch_fund_portfolios(fund: dict[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    payload = _request_json(_request_url(fund["code"]))
    title = payload.get("Title")
    rows = payload.get("Data")
    if not isinstance(title, list) or not isinstance(rows, list):
        raise ValueError(f"{fund['code']} 回傳資料格式不正確")
    portfolios = parse_portfolios(title, rows, fund["code"])
    if not portfolios:
        raise ValueError(f"{fund['code']} 未取得股票持股")
    return portfolios


def _snapshots_to_maps(
    fund_state: dict[str, Any] | None,
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    if not isinstance(fund_state, dict):
        return result
    for snapshot in fund_state.get("snapshots", []):
        if not isinstance(snapshot, dict) or not snapshot.get("date"):
            continue
        holdings = snapshot.get("holdings")
        if not isinstance(holdings, list):
            continue
        result[snapshot["date"]] = {
            item["symbol"]: item
            for item in holdings
            if isinstance(item, dict) and item.get("symbol")
        }
    return result


def _maps_to_snapshots(
    portfolios: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    snapshots = []
    for date in sorted(portfolios)[-MAX_SNAPSHOT_DATES:]:
        holdings = sorted(
            portfolios[date].values(),
            key=lambda item: (-item.get("weight", 0), item["symbol"]),
        )
        snapshots.append({"date": date, "holdings": holdings})
    return snapshots


def merge_snapshot_fund(
    fund: dict[str, Any],
    previous_state: dict[str, Any] | None,
    fetched: dict[str, dict[str, dict[str, Any]]] | None,
) -> dict[str, Any]:
    portfolios = _snapshots_to_maps(previous_state)
    if fetched:
        portfolios.update(fetched)
    return {
        **fund,
        "snapshots": _maps_to_snapshots(portfolios),
    }


def _rank_map(portfolio: dict[str, dict[str, Any]]) -> dict[str, int]:
    ordered = sorted(
        portfolio.values(),
        key=lambda item: (-item.get("weight", 0), item["symbol"]),
    )
    return {item["symbol"]: index + 1 for index, item in enumerate(ordered)}


def _with_weight_change(
    holding: dict[str, Any],
    previous_weight: float,
    current_weight: float,
    rank: int | None = None,
    previous_rank: int | None = None,
) -> dict[str, Any]:
    item = dict(holding)
    item.update(
        {
            "previous_weight": round(previous_weight, 4),
            "current_weight": round(current_weight, 4),
            "delta_pp": round(current_weight - previous_weight, 4),
            "rank": rank,
            "previous_rank": previous_rank,
        }
    )
    return item


def _weight_streak(
    portfolios: list[dict[str, dict[str, Any]]],
    symbol: str,
) -> int:
    if len(portfolios) < 2:
        return 0
    streak = 0
    direction = 0
    for index in range(len(portfolios) - 1, 0, -1):
        current = portfolios[index].get(symbol, {}).get("weight", 0)
        previous = portfolios[index - 1].get(symbol, {}).get("weight", 0)
        delta = current - previous
        step = 1 if delta >= STREAK_NOISE_PP else -1 if delta <= -STREAK_NOISE_PP else 0
        if step == 0:
            break
        if direction == 0:
            direction = step
        if step != direction:
            break
        streak += step
    return streak


def _trend_items(
    dates: list[str],
    portfolios: dict[str, dict[str, dict[str, Any]]],
    window: int,
) -> list[dict[str, Any]]:
    if len(dates) < window:
        return []
    old = portfolios[dates[-window]]
    current = portfolios[dates[-1]]
    current_ranks = _rank_map(current)
    old_ranks = _rank_map(old)
    items = []
    for symbol in current.keys() | old.keys():
        current_item = current.get(symbol)
        old_item = old.get(symbol)
        current_weight = current_item.get("weight", 0) if current_item else 0
        old_weight = old_item.get("weight", 0) if old_item else 0
        if abs(current_weight - old_weight) + 1e-9 < MIN_WEIGHT_CHANGE_PP:
            continue
        holding = current_item or old_item
        item = _with_weight_change(
            holding,
            old_weight,
            current_weight,
            current_ranks.get(symbol),
            old_ranks.get(symbol),
        )
        item["change_type"] = (
            "added" if old_item is None else "removed" if current_item is None else "adjusted"
        )
        items.append(item)
    return sorted(items, key=lambda item: (-abs(item["delta_pp"]), item["symbol"]))


def build_report_from_snapshots(
    fund: dict[str, Any],
    snapshots: list[dict[str, Any]],
    fetch_error: str | None = None,
) -> dict[str, Any]:
    portfolios = {
        snapshot["date"]: {
            item["symbol"]: item
            for item in snapshot.get("holdings", [])
            if item.get("symbol")
        }
        for snapshot in snapshots
        if snapshot.get("date")
    }
    dates = sorted(portfolios)
    base = {
        key: fund[key]
        for key in (
            "code",
            "name",
            "manager",
            "category",
            "region",
            "priority",
            "official_url",
        )
    }
    if not dates:
        return {
            **base,
            "status": "unavailable",
            "fetch_error": fetch_error,
            "data_date": "",
            "previous_date": "",
            "current_holdings_count": 0,
            "added": [],
            "removed": [],
            "increased": [],
            "decreased": [],
            "top10_entered": [],
            "top10_exited": [],
            "trend_3d": [],
            "trend_5d": [],
        }

    data_date = dates[-1]
    current = portfolios[data_date]
    if len(dates) < 2:
        return {
            **base,
            "status": "baseline",
            "fetch_error": fetch_error,
            "data_date": data_date,
            "previous_date": "",
            "current_holdings_count": len(current),
            "added": [],
            "removed": [],
            "increased": [],
            "decreased": [],
            "top10_entered": [],
            "top10_exited": [],
            "trend_3d": [],
            "trend_5d": [],
        }

    previous_date = dates[-2]
    previous = portfolios[previous_date]
    previous_ranks = _rank_map(previous)
    current_ranks = _rank_map(current)
    portfolio_series = [portfolios[date] for date in dates]

    added_symbols = sorted(current.keys() - previous.keys())
    removed_symbols = sorted(previous.keys() - current.keys())
    common_symbols = current.keys() & previous.keys()

    added = [
        _with_weight_change(
            current[symbol],
            0,
            current[symbol]["weight"],
            current_ranks.get(symbol),
            None,
        )
        for symbol in added_symbols
    ]
    removed = [
        _with_weight_change(
            previous[symbol],
            previous[symbol]["weight"],
            0,
            None,
            previous_ranks.get(symbol),
        )
        for symbol in removed_symbols
    ]

    weight_changes = []
    for symbol in common_symbols:
        delta = current[symbol]["weight"] - previous[symbol]["weight"]
        if abs(delta) + 1e-9 < MIN_WEIGHT_CHANGE_PP:
            continue
        item = _with_weight_change(
            current[symbol],
            previous[symbol]["weight"],
            current[symbol]["weight"],
            current_ranks.get(symbol),
            previous_ranks.get(symbol),
        )
        item["streak"] = _weight_streak(portfolio_series, symbol)
        weight_changes.append(item)

    increased = sorted(
        (item for item in weight_changes if item["delta_pp"] > 0),
        key=lambda item: (-item["delta_pp"], item["symbol"]),
    )
    decreased = sorted(
        (item for item in weight_changes if item["delta_pp"] < 0),
        key=lambda item: (item["delta_pp"], item["symbol"]),
    )

    previous_top10 = {symbol for symbol, rank in previous_ranks.items() if rank <= 10}
    current_top10 = {symbol for symbol, rank in current_ranks.items() if rank <= 10}
    top10_entered = [
        _with_weight_change(
            current[symbol],
            previous.get(symbol, {}).get("weight", 0),
            current[symbol]["weight"],
            current_ranks.get(symbol),
            previous_ranks.get(symbol),
        )
        for symbol in sorted(current_top10 - previous_top10)
    ]
    top10_exited = [
        _with_weight_change(
            (current.get(symbol) or previous[symbol]),
            previous[symbol]["weight"],
            current.get(symbol, {}).get("weight", 0),
            current_ranks.get(symbol),
            previous_ranks.get(symbol),
        )
        for symbol in sorted(previous_top10 - current_top10)
    ]

    return {
        **base,
        "status": "error" if fetch_error else "ok",
        "fetch_error": fetch_error,
        "data_date": data_date,
        "previous_date": previous_date,
        "current_holdings_count": len(current),
        "added": added,
        "removed": removed,
        "increased": increased,
        "decreased": decreased,
        "top10_entered": top10_entered,
        "top10_exited": top10_exited,
        "trend_3d": _trend_items(dates, portfolios, 3),
        "trend_5d": _trend_items(dates, portfolios, 5),
    }


def build_report(
    etf_code: str,
    etf_name: str,
    official_url: str,
    title: list[str],
    rows: list[list[Any]],
) -> dict[str, Any]:
    """Compatibility helper used by focused unit tests."""
    fund = {
        "code": etf_code,
        "name": etf_name,
        "manager": manager_from_name(etf_name),
        "category": "foreign" if etf_code == "00988A" else "domestic",
        "region": "海外型" if etf_code == "00988A" else "台股型",
        "priority": etf_code in PRIORITY_CODES,
        "official_url": official_url,
    }
    snapshots = _maps_to_snapshots(parse_portfolios(title, rows, etf_code))
    return build_report_from_snapshots(fund, snapshots)


def _score_report_actions(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    actions: dict[str, dict[str, Any]] = {}

    def add_action(item: dict[str, Any], score: int, label: str) -> None:
        symbol = item["symbol"]
        record = actions.setdefault(
            symbol,
            {
                "symbol": symbol,
                "name": item["name"],
                "market": item["market"],
                "market_code": item["market_code"],
                "score": 0,
                "labels": [],
                "delta_pp": 0.0,
            },
        )
        record["score"] += score
        record["labels"].append(label)
        record["delta_pp"] = round(record["delta_pp"] + item.get("delta_pp", 0), 4)

    for item in report["added"]:
        add_action(item, 3, "新建倉")
    for item in report["removed"]:
        add_action(item, -3, "完全出清")
    for item in report["increased"]:
        score = 2 if item["delta_pp"] >= STRONG_WEIGHT_CHANGE_PP else 1
        add_action(item, score, "明顯加碼" if score == 2 else "小幅加碼")
        if item.get("streak", 0) >= 3:
            add_action(item, 1, "連續加碼")
    for item in report["decreased"]:
        score = -2 if item["delta_pp"] <= -STRONG_WEIGHT_CHANGE_PP else -1
        add_action(item, score, "明顯減碼" if score == -2 else "小幅減碼")
        if item.get("streak", 0) <= -3:
            add_action(item, -1, "連續減碼")
    for item in report["top10_entered"]:
        add_action(item, 1, "進入前十大")
    for item in report["top10_exited"]:
        add_action(item, -1, "退出前十大")
    return actions


def build_consensus(
    reports: Iterable[dict[str, Any]],
    category: str,
) -> dict[str, list[dict[str, Any]]]:
    stock_records: dict[str, dict[str, Any]] = {}
    for report in reports:
        if report.get("category") != category or report.get("status") == "unavailable":
            continue
        for action in _score_report_actions(report).values():
            record = stock_records.setdefault(
                action["symbol"],
                {
                    "symbol": action["symbol"],
                    "name": action["name"],
                    "market": action["market"],
                    "market_code": action["market_code"],
                    "manager_scores": {},
                    "funds": [],
                    "delta_pp_sum": 0.0,
                },
            )
            manager = report["manager"]
            record["manager_scores"][manager] = (
                record["manager_scores"].get(manager, 0) + action["score"]
            )
            record["funds"].append(
                {
                    "code": report["code"],
                    "name": report["name"],
                    "manager": manager,
                    "score": action["score"],
                    "labels": action["labels"],
                    "delta_pp": action["delta_pp"],
                }
            )
            record["delta_pp_sum"] = round(
                record["delta_pp_sum"] + action["delta_pp"],
                4,
            )

    output = []
    for record in stock_records.values():
        manager_scores = {
            manager: max(-4, min(4, score))
            for manager, score in record.pop("manager_scores").items()
            if score
        }
        score = sum(manager_scores.values())
        positive_managers = sum(value > 0 for value in manager_scores.values())
        negative_managers = sum(value < 0 for value in manager_scores.values())
        if score >= 6 and positive_managers >= 2:
            signal = "強烈共識加碼"
        elif score >= 3:
            signal = "經理人開始布局"
        elif score > 0:
            signal = "偏多觀察"
        elif score <= -6 and negative_managers >= 2:
            signal = "共識撤退"
        elif score <= -3:
            signal = "經理人明顯減碼"
        else:
            signal = "偏空觀察"
        output.append(
            {
                **record,
                "score": score,
                "signal": signal,
                "manager_count": len(manager_scores),
                "positive_managers": positive_managers,
                "negative_managers": negative_managers,
                "etf_count": len(record["funds"]),
                "managers": sorted(manager_scores),
            }
        )

    bullish = sorted(
        (item for item in output if item["score"] > 0),
        key=lambda item: (
            -item["positive_managers"],
            -item["score"],
            -item["etf_count"],
            item["symbol"],
        ),
    )
    bearish = sorted(
        (item for item in output if item["score"] < 0),
        key=lambda item: (
            -item["negative_managers"],
            item["score"],
            -item["etf_count"],
            item["symbol"],
        ),
    )
    return {"bullish": bullish[:20], "bearish": bearish[:20]}


def _history_report(report: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "code",
        "name",
        "manager",
        "category",
        "region",
        "priority",
        "official_url",
        "status",
        "fetch_error",
        "data_date",
        "previous_date",
        "current_holdings_count",
        "added",
        "removed",
        "increased",
        "decreased",
        "top10_entered",
        "top10_exited",
    )
    return {key: report.get(key) for key in keys}


def _summary(reports: list[dict[str, Any]], batch_date: str) -> dict[str, Any]:
    return {
        "tracked_etfs": len(reports),
        "priority_etfs": sum(report.get("priority", False) for report in reports),
        "domestic_etfs": sum(report.get("category") == "domestic" for report in reports),
        "foreign_etfs": sum(report.get("category") == "foreign" for report in reports),
        "updated_etfs": sum(report.get("data_date") == batch_date for report in reports),
        "delayed_etfs": sum(
            bool(report.get("data_date")) and report.get("data_date") < batch_date
            for report in reports
        ),
        "unavailable_etfs": sum(report.get("status") == "unavailable" for report in reports),
        "added": sum(len(report.get("added", [])) for report in reports),
        "removed": sum(len(report.get("removed", [])) for report in reports),
        "increased": sum(len(report.get("increased", [])) for report in reports),
        "decreased": sum(len(report.get("decreased", [])) for report in reports),
    }


def _payload_signature(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload.get(key)
        for key in ("universe", "batch_date", "summary", "consensus", "etfs", "errors")
    }


def merge_history(payload: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    history = existing.get("history")
    if not isinstance(history, list):
        history = []
    entry = {
        "batch_date": payload["batch_date"],
        "generated_at": payload["generated_at"],
        "summary": payload["summary"],
        "consensus": payload["consensus"],
        "etfs": [_history_report(report) for report in payload["etfs"]],
    }
    history = [
        item
        for item in history
        if isinstance(item, dict) and item.get("batch_date") != payload["batch_date"]
    ]
    history.append(entry)
    history.sort(key=lambda item: item.get("batch_date", ""))
    merged["history"] = history[-MAX_HISTORY_BATCHES:]
    return merged


def build_outputs(
    existing_payload: dict[str, Any] | None = None,
    existing_snapshots: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    existing_payload = existing_payload or {}
    existing_snapshots = existing_snapshots or {}
    cached_universe = existing_payload.get("universe")
    universe = fetch_universe(cached_universe if isinstance(cached_universe, list) else None)
    old_funds = existing_snapshots.get("funds")
    if not isinstance(old_funds, dict):
        old_funds = {}

    fetched: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    errors: dict[str, str] = {}

    def fetch_one(fund: dict[str, Any]) -> tuple[str, Any, str | None]:
        try:
            return fund["code"], fetch_fund_portfolios(fund), None
        except Exception as error:  # pragma: no cover - exercised by integration
            return fund["code"], None, str(error)

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        for code, portfolios, error in executor.map(fetch_one, universe):
            if error:
                errors[code] = error
            else:
                fetched[code] = portfolios

    snapshot_funds = {}
    reports = []
    for fund in universe:
        code = fund["code"]
        fund_state = merge_snapshot_fund(
            fund,
            old_funds.get(code),
            fetched.get(code),
        )
        snapshot_funds[code] = fund_state
        reports.append(
            build_report_from_snapshots(
                fund,
                fund_state["snapshots"],
                errors.get(code),
            )
        )

    data_dates = [report["data_date"] for report in reports if report["data_date"]]
    if not data_dates:
        raise RuntimeError("所有主動ETF均無可用持股資料")
    batch_date = max(data_dates)
    for report in reports:
        if (
            report["status"] == "ok"
            and report["data_date"]
            and report["data_date"] < batch_date
        ):
            report["status"] = "delayed"

    generated_at = datetime.now(TAIPEI).isoformat(timespec="seconds")
    payload = {
        "generated_at": generated_at,
        "batch_date": batch_date,
        "source": {
            "universe_name": "TWSE主動式ETF商品清單",
            "universe_url": TWSE_ACTIVE_URL,
            "portfolio_name": "CMoney公開ETF持股資料",
            "portfolio_url": CMONEY_ENDPOINT,
            "note": "重要變動請以各投信及證交所每日投資組合公告為準。",
        },
        "settings": {
            "minimum_weight_change_pp": MIN_WEIGHT_CHANGE_PP,
            "strong_weight_change_pp": STRONG_WEIGHT_CHANGE_PP,
            "snapshot_dates": MAX_SNAPSHOT_DATES,
            "history_batches": MAX_HISTORY_BATCHES,
        },
        "universe": universe,
        "summary": _summary(reports, batch_date),
        "consensus": {
            "domestic": build_consensus(reports, "domestic"),
            "foreign": build_consensus(reports, "foreign"),
        },
        "etfs": reports,
        "errors": errors,
    }
    snapshot_payload = {
        "generated_at": generated_at,
        "source": payload["source"],
        "funds": snapshot_funds,
    }
    return payload, snapshot_payload


def write_outputs(
    payload: dict[str, Any],
    snapshots: dict[str, Any],
    output_path: Path = OUTPUT_PATH,
    snapshot_path: Path = SNAPSHOT_PATH,
) -> tuple[bool, bool]:
    existing_payload = _read_json(output_path)
    existing_snapshots = _read_json(snapshot_path)

    payload_changed = _payload_signature(existing_payload) != _payload_signature(payload)
    snapshot_changed = existing_snapshots.get("funds") != snapshots.get("funds")
    payload_needs_compaction = False
    if output_path.exists():
        try:
            payload_needs_compaction = output_path.read_text(
                encoding="utf-8",
                errors="ignore",
            ).startswith("{\n  ")
        except OSError:
            pass

    if payload_changed or payload_needs_compaction:
        payload = merge_history(payload, existing_payload)
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    if snapshot_changed:
        with gzip.open(snapshot_path, "wt", encoding="utf-8", compresslevel=9) as stream:
            json.dump(snapshots, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
    return payload_changed or payload_needs_compaction, snapshot_changed


def main() -> int:
    try:
        existing_payload = _read_json(OUTPUT_PATH)
        existing_snapshots = _read_json(SNAPSHOT_PATH)
        payload, snapshots = build_outputs(existing_payload, existing_snapshots)
        payload_changed, snapshot_changed = write_outputs(payload, snapshots)
        summary = payload["summary"]
        print(
            f"{payload['batch_date']}：追蹤 {summary['tracked_etfs']} 檔，"
            f"新增 {summary['added']}、剔除 {summary['removed']}、"
            f"加碼 {summary['increased']}、減碼 {summary['decreased']}"
        )
        if payload["errors"]:
            print(f"沿用快照的抓取異常：{', '.join(sorted(payload['errors']))}")
        print(
            "已更新經理人雷達"
            if payload_changed or snapshot_changed
            else "資料日期與內容未變，無需更新"
        )
        return 0
    except Exception as error:
        print(f"主動ETF經理人雷達更新失敗：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
