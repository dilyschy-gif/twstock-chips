#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外資籌碼連續追蹤 — 資料管線
================================

抓取證交所 TWT38U（外資及陸資買賣超彙總表）與 MI_INDEX（每日收盤行情），
逐日落地成 JSON，並彙整成前端用的 summary.json。

用法
----
    # 每日增補（只抓缺的交易日）
    python scripts/foreign_chips.py

    # 首次建置 / 補歷史（往回 90 個日曆日）
    python scripts/foreign_chips.py --backfill 90

    # 只重建 summary，不連網
    python scripts/foreign_chips.py --rebuild-only

    # 指定輸出目錄
    python scripts/foreign_chips.py --out public/data/foreign-chips

設計重點
--------
1. 權證/DR/特別股一律剔除。TWT38U 全市場回傳含數百檔權證（6碼且非 00 開頭，
   例如 078080、05202P、03028Q），那是外資自營商的避險部位，不是方向性籌碼，
   混進排行榜會直接毀掉訊號。
2. 三組欄位全部保留：不含外資自營商 / 外資自營商 / 合計。預設顯示合計
   （與媒體口徑一致），但 dealer 佔比過高時前端會標記。
3. 日期驗證：TWSE 在收盤資料未產出時會回空或回舊資料。每筆回傳都比對日期，
   不符即視為無資料，絕不靜默寫入 —— 錯誤的日期會在連續天數裡累積成假訊號。
4. 增量優先：已存在的 daily/*.json 不重抓，省流量也避免被證交所擋。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------

TPE = timezone(timedelta(hours=8))

DEFAULT_OUT = "public/data/foreign-chips"

# 證交所端點。舊路徑目前仍可用且較穩定，rwd 路徑作為備援。
TWT38U_URLS = [
    "https://www.twse.com.tw/fund/TWT38U?date={d}&response={fmt}",
    "https://www.twse.com.tw/rwd/zh/fund/TWT38U?date={d}&response={fmt}",
]
MI_INDEX_URLS = [
    "https://www.twse.com.tw/exchangeReport/MI_INDEX?date={d}&type=ALLBUT0999&response=json",
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d}&type=ALLBUT0999&response=json",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/csv, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Referer": "https://www.twse.com.tw/zh/trading/foreign/twt38u.html",
}

REQUEST_GAP = 4.0        # 秒。證交所對密集請求會擋，別調低。
MAX_RETRY = 3
TIMEOUT = 30

SUMMARY_WINDOW = 60      # 計算 5/10/20/60 日累計所需的交易日數
TAPE_DAYS = 40           # 前端籌碼帶顯示的天數（60 格太窄看不清，也讓 JSON 瘦一圈）
MIN_ROWS_OK = 200        # 一天少於這個檔數視為抓取異常


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{datetime.now(TPE):%H:%M:%S}] {msg}", flush=True)


def clean_cell(v: str) -> str:
    """去掉 Excel 防呆用的 ="..." 外殼與全形空白。"""
    if v is None:
        return ""
    s = str(v).strip()
    if s.startswith("="):
        s = s[1:]
    s = s.strip().strip('"').strip()
    return s.replace("\u3000", " ").strip()


def to_int(v: str) -> int:
    s = clean_cell(v).replace(",", "")
    if s in ("", "-", "--", "N/A"):
        return 0
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        n = int(float(s))
    except ValueError:
        return 0
    return -n if neg else n


def to_float(v: str):
    s = clean_cell(v).replace(",", "")
    if s in ("", "-", "--", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def classify_code(code: str) -> str:
    """
    分類證券代號。

    stock : 4 碼純數字普通股（1101 ~ 9958）
    etf   : 00 開頭，4~6 碼（0050 / 0056 / 006208 / 00631L / 00981A）
    other : 權證、DR、特別股、受益證券 —— 全部排除
    """
    c = code.strip().upper()
    if not c:
        return "other"
    # ETF 一律 00 開頭（0050 / 00893 / 006208 / 00631L / 00981A）。
    # 上市權證 6 碼但第二碼為 3~9（078080 / 03028Q / 05202P），不會誤收。
    if 4 <= len(c) <= 6 and re.fullmatch(r"00\d{2,4}[A-Z]?", c):
        return "etf"
    if len(c) == 4 and c.isdigit():
        return "stock"
    return "other"


def roc_to_iso(s: str):
    """把 '111年07月29日' 轉成 '20220729'。認不出來就回 None。"""
    m = re.search(r"(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s or "")
    if not m:
        return None
    y, mo, d = int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3))
    return f"{y:04d}{mo:02d}{d:02d}"


def fetch(url: str) -> requests.Response | None:
    for attempt in range(1, MAX_RETRY + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            log(f"  HTTP {r.status_code} (第 {attempt} 次) {url[:90]}")
        except requests.RequestException as e:
            log(f"  連線失敗 (第 {attempt} 次): {e}")
        time.sleep(REQUEST_GAP * attempt)
    return None


# --------------------------------------------------------------------------
# TWT38U：外資及陸資買賣超
# --------------------------------------------------------------------------

def parse_twt38u_json(payload: dict, want: str):
    if not isinstance(payload, dict):
        return None
    if payload.get("stat") != "OK":
        return None

    rows = payload.get("data")
    if not rows:
        for t in payload.get("tables", []) or []:
            if t.get("data"):
                rows = t["data"]
                break
    if not rows:
        return None

    title = payload.get("title") or ""
    if not title:
        for t in payload.get("tables", []) or []:
            title = t.get("title") or ""
            if title:
                break
    got = roc_to_iso(title) or clean_cell(payload.get("date", ""))
    if got and got != want:
        log(f"  ! JSON 日期不符：期待 {want}，實得 {got}")
        return None

    return _rows_to_records(rows)


def parse_twt38u_csv(text: str, want: str):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None

    got = roc_to_iso(lines[0])
    if got and got != want:
        log(f"  ! CSV 日期不符：期待 {want}，實得 {got}")
        return None

    body = "\n".join(lines[1:])
    rows = list(csv.reader(io.StringIO(body)))
    return _rows_to_records(rows)


def _rows_to_records(rows):
    """
    TWT38U 欄位（去掉首欄標記後）：
      0 證券代號
      1 證券名稱
      2/3/4  外資及陸資(不含外資自營商) 買進/賣出/買賣超 股數
      5/6/7  外資自營商 買進/賣出/買賣超 股數
      8/9/10 外資及陸資 買進/賣出/買賣超 股數
    首欄可能是 ' ' 或 '*' 標記，也可能整欄不存在，所以用長度判斷。
    """
    out = {}
    for raw in rows:
        if not raw or len(raw) < 11:
            continue
        cells = [clean_cell(c) for c in raw]

        # 找出證券代號所在的欄位：第 0 或第 1 欄
        idx = None
        for cand in (0, 1):
            if cand < len(cells) and re.fullmatch(r"[0-9A-Z]{4,6}", cells[cand].upper()):
                idx = cand
                break
        if idx is None:
            continue
        if len(cells) - idx < 11:
            continue

        code = cells[idx].upper()
        kind = classify_code(code)
        if kind == "other":
            continue

        name = cells[idx + 1]
        if not name or name.startswith("證券"):
            continue

        net_ex = to_int(cells[idx + 4])
        net_dl = to_int(cells[idx + 7])
        net_all = to_int(cells[idx + 10])

        # 內部一致性檢查：不含自營 + 自營 應等於合計
        if net_ex + net_dl != net_all:
            net_all = net_ex + net_dl

        out[code] = {
            "code": code,
            "name": name,
            "kind": kind,
            "net_ex_dealer": net_ex,
            "net_dealer": net_dl,
            "net": net_all,
        }
    return out or None


def fetch_twt38u(day: str):
    for url_tpl in TWT38U_URLS:
        for fmt in ("json", "csv"):
            url = url_tpl.format(d=day, fmt=fmt)
            r = fetch(url)
            time.sleep(REQUEST_GAP)
            if r is None:
                continue
            try:
                if fmt == "json":
                    recs = parse_twt38u_json(r.json(), day)
                else:
                    text = r.content.decode("cp950", errors="replace")
                    recs = parse_twt38u_csv(text, day)
            except Exception as e:  # noqa: BLE001
                log(f"  解析失敗（{fmt}）：{e}")
                continue
            if recs:
                return recs
    return None


# --------------------------------------------------------------------------
# MI_INDEX：每日收盤行情（取成交股數與收盤價）
# --------------------------------------------------------------------------

def fetch_mi_index(day: str):
    for url_tpl in MI_INDEX_URLS:
        r = fetch(url_tpl.format(d=day))
        time.sleep(REQUEST_GAP)
        if r is None:
            continue
        try:
            payload = r.json()
        except Exception:  # noqa: BLE001
            continue
        if payload.get("stat") != "OK":
            continue

        tables = payload.get("tables")
        if not tables:
            tables = []
            for i in range(1, 12):
                f, d = payload.get(f"fields{i}"), payload.get(f"data{i}")
                if f and d:
                    tables.append({"fields": f, "data": d})

        for t in tables:
            fields = [clean_cell(f) for f in (t.get("fields") or [])]
            if "證券代號" not in fields or "成交股數" not in fields:
                continue
            i_code = fields.index("證券代號")
            i_vol = fields.index("成交股數")
            i_close = fields.index("收盤價") if "收盤價" in fields else None
            i_diff = fields.index("漲跌價差") if "漲跌價差" in fields else None
            i_sign = fields.index("漲跌(+/-)") if "漲跌(+/-)" in fields else None

            out = {}
            for row in t.get("data") or []:
                if len(row) <= i_vol:
                    continue
                code = clean_cell(row[i_code]).upper()
                if classify_code(code) == "other":
                    continue
                close = to_float(row[i_close]) if i_close is not None else None
                diff = to_float(row[i_diff]) if i_diff is not None else None
                if diff is not None and i_sign is not None:
                    sign = clean_cell(row[i_sign])
                    if "-" in sign:
                        diff = -abs(diff)
                    elif "+" in sign:
                        diff = abs(diff)
                    else:
                        diff = 0.0
                out[code] = {
                    "volume": to_int(row[i_vol]),
                    "close": close,
                    "diff": diff,
                }
            if out:
                return out
    return None


# --------------------------------------------------------------------------
# 逐日落地
# --------------------------------------------------------------------------

def daily_path(out_dir: str, day: str) -> str:
    return os.path.join(out_dir, "daily", f"{day}.json")


def collect_day(out_dir: str, day: str, force: bool = False) -> str:
    """回傳 'ok' / 'cached' / 'nodata'。"""
    path = daily_path(out_dir, day)
    if os.path.exists(path) and not force:
        return "cached"

    log(f"抓取 {day} …")
    chips = fetch_twt38u(day)
    if not chips:
        log(f"  {day} 無買賣超資料（休市或尚未產出）")
        return "nodata"
    if len(chips) < MIN_ROWS_OK:
        log(f"  ! {day} 僅 {len(chips)} 檔，低於門檻 {MIN_ROWS_OK}，視為異常不寫入")
        return "nodata"

    quotes = fetch_mi_index(day) or {}
    if not quotes:
        log(f"  ! {day} 行情資料缺漏，佔量比將留空")

    items = []
    for code, rec in chips.items():
        q = quotes.get(code) or {}
        items.append({
            **rec,
            "volume": q.get("volume"),
            "close": q.get("close"),
            "diff": q.get("diff"),
        })
    items.sort(key=lambda x: x["code"])

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "date": day,
                "fetched_at": datetime.now(TPE).isoformat(timespec="seconds"),
                "count": len(items),
                "has_quotes": bool(quotes),
                "items": items,
            },
            f,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    log(f"  {day} 寫入 {len(items)} 檔"
        f"{'（含行情）' if quotes else '（無行情）'}")
    return "ok"


# --------------------------------------------------------------------------
# 彙整 summary.json
# --------------------------------------------------------------------------

def load_daily(out_dir: str, limit: int):
    d = os.path.join(out_dir, "daily")
    if not os.path.isdir(d):
        return []
    files = sorted(f for f in os.listdir(d) if re.fullmatch(r"\d{8}\.json", f))
    files = files[-limit:]
    days = []
    for fn in files:
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                days.append(json.load(f))
        except Exception as e:  # noqa: BLE001
            log(f"  ! 讀取 {fn} 失敗：{e}")
    return days


def streak_of(series):
    """
    從最新一日往回數同向天數。
    回傳正數＝連續買超天數，負數＝連續賣超天數，0＝最新日持平。
    """
    if not series:
        return 0
    last = series[-1]
    if last == 0:
        return 0
    sign = 1 if last > 0 else -1
    n = 0
    for v in reversed(series):
        if v == 0 or (1 if v > 0 else -1) != sign:
            break
        n += 1
    return n * sign


def build_summary(out_dir: str, window: int = SUMMARY_WINDOW) -> dict:
    days = load_daily(out_dir, window)
    if not days:
        raise SystemExit("沒有任何 daily 資料，先跑一次 --backfill")

    dates = [d["date"] for d in days]
    n = len(dates)

    # code -> 每日資料（缺的日子補 None，之後填 0）
    universe = {}
    for i, day in enumerate(days):
        for it in day["items"]:
            c = it["code"]
            slot = universe.setdefault(c, {
                "code": c,
                "name": it["name"],
                "kind": it["kind"],
                "net": [0] * n,
                "net_ex": [0] * n,
                "net_dl": [0] * n,
                "vol": [None] * n,
                "close": None,
                "diff": None,
            })
            slot["name"] = it["name"]
            slot["net"][i] = it["net"]
            slot["net_ex"][i] = it["net_ex_dealer"]
            slot["net_dl"][i] = it["net_dealer"]
            slot["vol"][i] = it.get("volume")
            if it.get("close") is not None:
                slot["close"] = it["close"]
                slot["diff"] = it.get("diff")

    def lots(shares):
        return int(round(shares / 1000))

    items = []
    for c, s in universe.items():
        net_lots = [lots(v) for v in s["net"]]
        last_shares = s["net"][-1]
        vol_last = s["vol"][-1]

        def tail_sum(k):
            return lots(sum(s["net"][-k:])) if n >= 1 else 0

        amt = None
        if s["close"] is not None:
            amt = round(last_shares * s["close"] / 1e8, 2)   # 億元

        vr = None
        if vol_last:
            vr = round(abs(last_shares) / vol_last * 100, 1)

        dealer_share = None
        tot = abs(s["net"][-1])
        if tot:
            dealer_share = round(abs(s["net_dl"][-1]) / tot * 100, 1)

        items.append({
            "c": c,
            "n": s["name"],
            "k": s["kind"],
            "s": net_lots[-TAPE_DAYS:],
            "last": net_lots[-1],
            "amt": amt,
            "a5": tail_sum(5),
            "a10": tail_sum(10),
            "a20": tail_sum(20),
            "a60": tail_sum(60),
            "st": streak_of(s["net"]),
            "vr": vr,
            "ds": dealer_share,
            "px": s["close"],
            "chg": s["diff"],
            "vol": lots(vol_last) if vol_last else None,
        })

    items.sort(key=lambda x: -abs(x["a20"]))

    # 市場合計（億元）：只有有收盤價的檔位能換算，故同時給張數合計
    market = []
    for i, day in enumerate(days):
        agg = {"d": day["date"], "stock": 0.0, "etf": 0.0}
        for it in day["items"]:
            if it.get("close") is None:
                continue
            v = it["net"] * it["close"] / 1e8
            agg["stock" if it["kind"] == "stock" else "etf"] += v
        agg["stock"] = round(agg["stock"], 1)
        agg["etf"] = round(agg["etf"], 1)
        agg["total"] = round(agg["stock"] + agg["etf"], 1)
        market.append(agg)

    return {
        "generated_at": datetime.now(TPE).isoformat(timespec="seconds"),
        "dates": dates,
        "tape_dates": dates[-TAPE_DAYS:],
        "latest": dates[-1],
        "count": len(items),
        "market": market,
        "items": items,
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="外資籌碼連續追蹤資料管線")
    ap.add_argument("--out", default=DEFAULT_OUT, help="輸出目錄")
    ap.add_argument("--backfill", type=int, default=0,
                    help="往回補幾個日曆日（首次建置建議 90）")
    ap.add_argument("--force", action="store_true", help="已存在的日檔也重抓")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="不連網，只用現有 daily 重建 summary")
    ap.add_argument("--window", type=int, default=SUMMARY_WINDOW,
                    help="summary 保留幾個交易日")
    args = ap.parse_args()

    out_dir = args.out
    os.makedirs(os.path.join(out_dir, "daily"), exist_ok=True)

    if not args.rebuild_only:
        today = datetime.now(TPE).date()
        span = args.backfill if args.backfill > 0 else 5
        targets = []
        for i in range(span + 1):
            d = today - timedelta(days=i)
            if d.weekday() >= 5:          # 六日直接跳過
                continue
            targets.append(d.strftime("%Y%m%d"))
        targets.sort()

        stats = {"ok": 0, "cached": 0, "nodata": 0}
        for day in targets:
            stats[collect_day(out_dir, day, force=args.force)] += 1
        log(f"抓取完成：新增 {stats['ok']}、沿用 {stats['cached']}、無資料 {stats['nodata']}")

    log("重建 summary…")
    summary = build_summary(out_dir, args.window)
    path = os.path.join(out_dir, "summary.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, separators=(",", ":"))
    size_kb = os.path.getsize(path) / 1024
    log(f"summary.json 完成：{summary['count']} 檔 / "
        f"{len(summary['dates'])} 個交易日 / {size_kb:.0f} KB "
        f"（最新 {summary['latest']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
