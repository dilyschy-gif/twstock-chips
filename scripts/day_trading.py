#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
當沖選股雷達 — 資料管線
================================

盤後抓取 TWSE MI_INDEX（上市每日收盤行情）與 TPEX dailyQuotes（上櫃每日收盤行情），
逐日落地成 JSON 快照，再依三大當沖條件掃描，產出前端用的 day-trading.json：

  條件一「成交量大」：日成交量 >= 1,000 張，或日成交金額 >= 5,000 萬元
  條件二「波動度夠」：近 30 個交易日平均每日振幅 >= 3%
                      （每日振幅 = (最高 - 最低) / 前收 × 100）
  條件三「熱門題材」：純數據族群強度 —— 依產業族群當日加權漲幅、成交金額占比、
                      量增幅度排出強勢族群，個股屬於前幾名族群即符合；
                      或個股本身量比 >= 2 且成交金額達焦點門檻（焦點資金股）。

用法
----
    # 每日增補（只抓缺的交易日）
    python scripts/day_trading.py

    # 首次建置 / 補歷史（往回 60 個日曆日，約可湊滿 30+ 個交易日）
    python scripts/day_trading.py --backfill 60

    # 只重建 day-trading.json，不連網
    python scripts/day_trading.py --rebuild-only

    # 指定輸出目錄
    python scripts/day_trading.py --out public/data/day-trading

設計重點
--------
1. 只收 4 碼純數字的普通股。ETF（00 開頭）、權證、DR、特別股全部剔除，
   當沖名單混入 ETF 或權證毫無意義。
2. 快照含 OHLC 與成交金額。前收優先用「收盤 - 漲跌價差」推回，缺漏時
   退回用前一個快照的收盤價，兩者都沒有就不算振幅（寧缺勿錯）。
3. 日期驗證：兩市場各自設最低檔數門檻（上市 700 / 上櫃 500），任一市場
   低於門檻即視為當日資料未產出，不寫快照 —— 半套資料會毀掉族群排名。
4. 增量優先：已存在的 daily/*.json 不重抓；--force 才重抓。
5. TWSE 需要帶 cookie 的 session（WAF 會擋裸 requests，同 fetch_chips.py 經驗）；
   TPEX 新版 /www/zh-tw/afterTrading/dailyQuotes 先試 GET 西元年，再退民國年與 POST。
6. TWSE TWTB4U 當沖統計為「加分資訊」（顯示當沖率），抓不到不影響名單本身。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------

TPE = timezone(timedelta(hours=8))

DEFAULT_OUT = "public/data/day-trading"

# ── 三大條件門檻（前端另可再收緊，這裡是入庫門檻）──
VOL_MIN_LOTS = 1000          # 條件一：日成交量（張）
TURNOVER_MIN = 50_000_000    # 條件一：或日成交金額（元）
AMP_WINDOW = 30              # 條件二：振幅回顧交易日數
AMP_MIN_SAMPLES = 20         # 條件二：至少要有幾天樣本才算數
AMP30_MIN = 3.0              # 條件二：30 日平均每日振幅（%）
HOT_SECTOR_TOP = 5           # 條件三：熱門族群取前幾名
HOT_SECTOR_MIN_STOCKS = 3    # 族群至少要有幾檔（太小的族群不排名）
FOCUS_VOL_RATIO = 2.0        # 條件三：焦點資金股 —— 量比門檻
FOCUS_TURNOVER = 100_000_000 # 條件三：焦點資金股 —— 成交金額門檻（元）

VOL_AVG_DAYS = 5             # 量比 = 今日量 / 近 5 日均量（不含今日）
MIN_ROWS_TWSE = 700          # 上市當日最低檔數，低於視為資料未產出
MIN_ROWS_TPEX = 500          # 上櫃當日最低檔數
KEEP_SNAPSHOT_FILES = 50     # daily/ 最多保留幾個快照檔
OUTPUT_MAX_STOCKS = 300      # day-trading.json 名單上限（含兩條件觀察股）
SPARK_DAYS = 30              # 前端 sparkline 天數

REQUEST_GAP = 4.0            # 秒。證交所對密集請求會擋，別調低。
MAX_RETRY = 3
TIMEOUT = 30

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

TWSE_MI_INDEX_URLS = [
    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d}&type=ALLBUT0999&response=json",
    "https://www.twse.com.tw/exchangeReport/MI_INDEX?date={d}&type=ALLBUT0999&response=json",
]
TWSE_MI_REFERER = "https://www.twse.com.tw/zh/trading/historical/mi-index.html"
TWSE_WARMUP = "https://www.twse.com.tw/zh/trading/historical/mi-index.html"

TWSE_TWTB4U_URLS = [
    "https://www.twse.com.tw/rwd/zh/afterTrading/TWTB4U?date={d}&selectType=All&response=json",
    "https://www.twse.com.tw/exchangeReport/TWTB4U?date={d}&selectType=All&response=json",
]

TPEX_DAILY_QUOTES = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
TPEX_REFERER = "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/pricing.html"

# 產業別（上市 t187ap03_L / 上櫃 mopsfin_t187ap03_O 共用同一套代碼）
INDUSTRY_URL_TWSE = "https://mopsfin.twse.com.tw/opendata/t187ap03_L.csv"
INDUSTRY_URL_TPEX = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

INDUSTRY_NAMES = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙",
    "10": "鋼鐵", "11": "橡膠", "12": "汽車", "14": "建材營造",
    "15": "航運", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨",
    "19": "綜合", "20": "其他", "21": "化學", "22": "生技醫療",
    "23": "油電燃氣", "24": "半導體", "25": "電腦及週邊", "26": "光電",
    "27": "通信網路", "28": "電子零組件", "29": "電子通路", "30": "資訊服務",
    "31": "其他電子", "32": "文化創意", "33": "農業科技", "34": "電子商務",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
    "80": "管理股票",
}


def log(msg: str) -> None:
    print(f"[{datetime.now(TPE):%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def clean_cell(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.startswith("="):
        s = s[1:]
    s = s.strip().strip('"').strip()
    return s.replace("　", " ").strip()


def to_float(v):
    s = clean_cell(v).replace(",", "")
    if s in ("", "-", "--", "N/A", "---", "----", "除息", "除權", "除權息", "X", "＋", "－"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if neg else n


def to_int(v) -> int:
    f = to_float(v)
    return int(f) if f is not None else 0


def is_common_stock(code: str) -> bool:
    """只收 4 碼純數字且非 0 開頭的普通股。ETF/權證/DR/特別股剔除。"""
    return bool(re.fullmatch(r"[1-9]\d{3}", code))


def pct_rank(sorted_vals: list, x) -> float:
    """x 在 sorted_vals 中的百分位（0~100）。"""
    if not sorted_vals or x is None:
        return 0.0
    import bisect
    i = bisect.bisect_right(sorted_vals, x)
    return round(100.0 * i / len(sorted_vals), 1)


# --------------------------------------------------------------------------
# TWSE：帶 cookie 的 session（WAF 對裸 requests 回 307）
# --------------------------------------------------------------------------

_twse_session = None


def _get_twse_session():
    global _twse_session
    if _twse_session is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-TW,zh;q=0.9",
        })
        try:
            s.get(TWSE_WARMUP, timeout=TIMEOUT)
        except requests.RequestException as e:
            log(f"TWSE session 預熱失敗（繼續嘗試）: {e}")
        _twse_session = s
    return _twse_session


def _twse_get_json(url: str, referer: str):
    """GET TWSE JSON；被 WAF 打發（307/403/429 或非 JSON）就重建 session 重試。"""
    global _twse_session
    for attempt in range(1, MAX_RETRY + 1):
        try:
            s = _get_twse_session()
            r = s.get(url, headers={"Referer": referer}, timeout=TIMEOUT,
                      allow_redirects=False)
            if r.status_code == 200 and r.text.strip().startswith("{"):
                return r.json()
            log(f"  TWSE HTTP {r.status_code}（第 {attempt} 次），重建 session")
            _twse_session = None
        except requests.RequestException as e:
            log(f"  TWSE 連線失敗（第 {attempt} 次）: {e}")
            _twse_session = None
        time.sleep(REQUEST_GAP * attempt)
    return None


def fetch_twse_quotes(day: str):
    """回傳 {code: {name, open, high, low, close, prev, volume, turnover}}。"""
    for url_tpl in TWSE_MI_INDEX_URLS:
        payload = _twse_get_json(url_tpl.format(d=day), TWSE_MI_REFERER)
        time.sleep(REQUEST_GAP)
        if not payload or payload.get("stat") != "OK":
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
            if "證券代號" not in fields or "最高價" not in fields:
                continue
            idx = {name: fields.index(name) for name in fields}
            i_code = idx.get("證券代號")
            i_name = idx.get("證券名稱")
            i_vol = idx.get("成交股數")
            i_amt = idx.get("成交金額")
            i_open = idx.get("開盤價")
            i_high = idx.get("最高價")
            i_low = idx.get("最低價")
            i_close = idx.get("收盤價")
            i_sign = idx.get("漲跌(+/-)")
            i_diff = idx.get("漲跌價差")
            if None in (i_code, i_vol, i_high, i_low, i_close):
                continue

            out = {}
            for row in t.get("data") or []:
                if len(row) <= max(i_vol, i_high, i_low, i_close):
                    continue
                code = clean_cell(row[i_code]).upper()
                if not is_common_stock(code):
                    continue
                close = to_float(row[i_close])
                high = to_float(row[i_high])
                low = to_float(row[i_low])
                if close is None or high is None or low is None:
                    continue  # 全日無成交
                diff = to_float(row[i_diff]) if i_diff is not None else None
                if diff is not None and i_sign is not None:
                    sign = clean_cell(row[i_sign])
                    if "-" in sign:
                        diff = -abs(diff)
                    elif "+" in sign:
                        diff = abs(diff)
                    else:
                        diff = 0.0
                prev = round(close - diff, 4) if diff is not None else None
                out[code] = {
                    "name": clean_cell(row[i_name]) if i_name is not None else code,
                    "open": to_float(row[i_open]) if i_open is not None else None,
                    "high": high, "low": low, "close": close, "prev": prev,
                    "volume": to_int(row[i_vol]),
                    "turnover": to_int(row[i_amt]) if i_amt is not None else 0,
                }
            if out:
                log(f"  TWSE：取得 {len(out)} 檔")
                return out
    return None


def fetch_twse_daytrade_stat(day: str):
    """TWTB4U 當日沖銷交易統計（加分資訊）。回傳 {code: 當沖成交股數}；失敗回 {}。"""
    try:
        for url_tpl in TWSE_TWTB4U_URLS:
            payload = _twse_get_json(url_tpl.format(d=day), TWSE_MI_REFERER)
            time.sleep(REQUEST_GAP)
            if not payload or payload.get("stat") != "OK":
                continue
            tables = payload.get("tables") or []
            for t in tables:
                fields = [clean_cell(f) for f in (t.get("fields") or [])]
                i_code = None
                i_dtvol = None
                for i, f in enumerate(fields):
                    if f in ("證券代號", "股票代號"):
                        i_code = i
                    if "沖銷" in f and "股數" in f and "買進" not in f and "賣出" not in f:
                        i_dtvol = i
                if i_code is None or i_dtvol is None:
                    continue
                out = {}
                for row in t.get("data") or []:
                    if len(row) <= max(i_code, i_dtvol):
                        continue
                    code = clean_cell(row[i_code]).upper()
                    if is_common_stock(code):
                        out[code] = to_int(row[i_dtvol])
                if out:
                    log(f"  TWSE 當沖統計：{len(out)} 檔")
                    return out
    except Exception as e:  # noqa: BLE001 — 加分資訊，任何失敗都不阻斷
        log(f"  TWSE 當沖統計略過: {e}")
    return {}


# --------------------------------------------------------------------------
# TPEX：上櫃每日收盤行情（新版 dailyQuotes）
# --------------------------------------------------------------------------

def _tpex_field_index(fields, candidates):
    for i, f in enumerate(fields):
        for c in candidates:
            if f == c or f.startswith(c):
                return i
    return None


def fetch_tpex_quotes(day: str):
    """回傳 {code: {...同上}}。day 為 YYYYMMDD。"""
    y, m, d = day[:4], day[4:6], day[6:8]
    ad_date = f"{y}/{m}/{d}"
    roc_date = f"{int(y) - 1911}/{m}/{d}"
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": TPEX_REFERER,
    }
    attempts = [
        ("GET", {"date": ad_date, "id": "", "response": "json"}),
        ("GET", {"date": roc_date, "id": "", "response": "json"}),
        ("POST", {"date": ad_date, "id": "", "response": "json"}),
        ("POST", {"date": roc_date, "id": "", "response": "json"}),
    ]
    for method, params in attempts:
        try:
            if method == "GET":
                r = requests.get(TPEX_DAILY_QUOTES, headers=headers,
                                 params=params, timeout=TIMEOUT)
            else:
                r = requests.post(TPEX_DAILY_QUOTES, headers=headers,
                                  data=params, timeout=TIMEOUT)
            time.sleep(REQUEST_GAP)
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                log(f"  TPEX {method} {params['date']} 非 JSON（HTTP {r.status_code}）")
                continue
            payload = r.json()
        except requests.RequestException as e:
            log(f"  TPEX 連線失敗: {e}")
            time.sleep(REQUEST_GAP)
            continue

        for t in (payload.get("tables") or []):
            fields = [clean_cell(f) for f in (t.get("fields") or [])]
            rows = t.get("data") or []
            if not fields or not rows:
                continue
            i_code = _tpex_field_index(fields, ["代號", "股票代號", "證券代號"])
            i_name = _tpex_field_index(fields, ["名稱", "股票名稱", "證券名稱"])
            i_close = _tpex_field_index(fields, ["收盤"])
            i_diff = _tpex_field_index(fields, ["漲跌"])
            i_open = _tpex_field_index(fields, ["開盤"])
            i_high = _tpex_field_index(fields, ["最高"])
            i_low = _tpex_field_index(fields, ["最低"])
            i_vol = _tpex_field_index(fields, ["成交股數", "成交量"])
            i_amt = _tpex_field_index(fields, ["成交金額"])
            if None in (i_code, i_close, i_high, i_low, i_vol):
                continue

            out = {}
            for row in rows:
                if len(row) <= max(i_code, i_close, i_high, i_low, i_vol):
                    continue
                code = clean_cell(row[i_code]).upper()
                if not is_common_stock(code):
                    continue
                close = to_float(row[i_close])
                high = to_float(row[i_high])
                low = to_float(row[i_low])
                if close is None or high is None or low is None:
                    continue
                diff = to_float(row[i_diff]) if i_diff is not None else None
                prev = round(close - diff, 4) if diff is not None else None
                out[code] = {
                    "name": clean_cell(row[i_name]) if i_name is not None else code,
                    "open": to_float(row[i_open]) if i_open is not None else None,
                    "high": high, "low": low, "close": close, "prev": prev,
                    "volume": to_int(row[i_vol]),
                    "turnover": to_int(row[i_amt]) if i_amt is not None else 0,
                }
            if out:
                log(f"  TPEX：取得 {len(out)} 檔")
                return out
        log("  TPEX 回應無可解析的表格（可能休市）")
        # JSON 拿到了但沒有資料 → 不再嘗試其他日期格式
        return None
    return None


# --------------------------------------------------------------------------
# 產業別
# --------------------------------------------------------------------------

def fetch_industry_map(out_dir: str) -> dict:
    """抓上市/上櫃公司清單的產業別。失敗時沿用既有 industry.json。"""
    path = os.path.join(out_dir, "industry.json")
    mapping = {}

    # 上市：mopsfin CSV（openapi.twse.com.tw 會擋資料中心 IP，勿改回）
    try:
        r = requests.get(INDUSTRY_URL_TWSE,
                         headers={"User-Agent": UA, "Accept": "text/csv,*/*"},
                         timeout=TIMEOUT)
        if r.status_code == 200 and not r.text.strip().startswith("<"):
            import csv as _csv
            import io as _io
            text = r.content.decode("utf-8-sig", errors="replace")
            reader = _csv.DictReader(_io.StringIO(text))
            for row in reader:
                code = clean_cell(row.get("公司代號") or row.get("SecuritiesCompanyCode"))
                ind = clean_cell(row.get("產業別") or row.get("SecuritiesIndustryCode"))
                if is_common_stock(code) and ind:
                    mapping[code] = INDUSTRY_NAMES.get(ind.zfill(2), ind)
            log(f"  產業別（上市）：{len(mapping)} 檔")
    except Exception as e:  # noqa: BLE001
        log(f"  產業別（上市）抓取失敗: {e}")
    time.sleep(REQUEST_GAP)

    # 上櫃：TPEX OpenAPI
    try:
        r = requests.get(INDUSTRY_URL_TPEX,
                         headers={"User-Agent": UA, "Accept": "application/json"},
                         timeout=TIMEOUT)
        if r.status_code == 200 and r.text.strip().startswith("["):
            n0 = len(mapping)
            for row in r.json():
                code = clean_cell(row.get("SecuritiesCompanyCode") or row.get("公司代號"))
                ind = clean_cell(row.get("SecuritiesIndustryCode") or row.get("產業別"))
                if is_common_stock(code) and ind:
                    mapping[code] = INDUSTRY_NAMES.get(ind.zfill(2), ind)
            log(f"  產業別（上櫃）：{len(mapping) - n0} 檔")
    except Exception as e:  # noqa: BLE001
        log(f"  產業別（上櫃）抓取失敗: {e}")

    if len(mapping) >= 1200:
        os.makedirs(out_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.now(TPE).isoformat(timespec="seconds"),
                       "map": mapping}, f, ensure_ascii=False)
        return mapping

    # 抓不全 → 沿用舊檔
    if os.path.exists(path):
        log("  產業別抓取不全，沿用既有 industry.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("map", {})
    log("  ! 無產業別資料，族群條件將全部標為「未分類」")
    return mapping


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
    twse = fetch_twse_quotes(day)
    if not twse or len(twse) < MIN_ROWS_TWSE:
        log(f"  {day} 上市僅 {0 if not twse else len(twse)} 檔（休市或尚未產出），不寫入")
        return "nodata"
    tpex = fetch_tpex_quotes(day)
    if not tpex or len(tpex) < MIN_ROWS_TPEX:
        log(f"  {day} 上櫃僅 {0 if not tpex else len(tpex)} 檔（尚未產出），不寫入")
        return "nodata"

    items = []
    for market, data in (("上市", twse), ("上櫃", tpex)):
        for code, q in data.items():
            items.append({"code": code, "market": market, **q})
    items.sort(key=lambda x: x["code"])

    os.makedirs(os.path.dirname(path), exist_ok=True)
    snap = {
        "date": day,
        "fetched_at": datetime.now(TPE).isoformat(timespec="seconds"),
        "count": len(items),
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    log(f"  ✅ {day} 寫入 {len(items)} 檔（上市 {len(twse)}／上櫃 {len(tpex)}）")
    return "ok"


def list_snapshot_days(out_dir: str) -> list:
    ddir = os.path.join(out_dir, "daily")
    if not os.path.isdir(ddir):
        return []
    days = [f[:-5] for f in os.listdir(ddir)
            if re.fullmatch(r"\d{8}\.json", f)]
    return sorted(days)


def prune_snapshots(out_dir: str) -> None:
    days = list_snapshot_days(out_dir)
    for day in days[:-KEEP_SNAPSHOT_FILES]:
        os.remove(daily_path(out_dir, day))
        log(f"  清理舊快照 {day}")


def load_snapshot(out_dir: str, day: str) -> dict:
    with open(daily_path(out_dir, day), encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# 掃描：三大條件 + 族群強度
# --------------------------------------------------------------------------

def build_history(out_dir: str, days: list) -> dict:
    """把最近的快照整成 {code: [(day, item), ...]}（依日期舊→新）。"""
    hist = {}
    for day in days:
        snap = load_snapshot(out_dir, day)
        for it in snap.get("items", []):
            hist.setdefault(it["code"], []).append((day, it))
    return hist


def daily_amplitude(item: dict, prev_close) -> float | None:
    """單日振幅 %。優先用 item 內建 prev，否則用傳入的前一日收盤。"""
    base = item.get("prev") or prev_close
    if not base or base <= 0:
        return None
    return (item["high"] - item["low"]) / base * 100.0


def scan(out_dir: str, industry: dict, dt_stat: dict) -> dict | None:
    days = list_snapshot_days(out_dir)
    if not days:
        log("沒有任何快照，無法掃描")
        return None
    latest = days[-1]
    window_days = days[-(AMP_WINDOW + 1):]  # 多取一天供 prev 使用
    hist = build_history(out_dir, window_days)
    log(f"掃描 {latest}（可用交易日 {len(days)}，振幅視窗 {len(window_days)}）")

    universe = []
    for code, series in hist.items():
        if series[-1][0] != latest:
            continue  # 今日沒成交
        today = series[-1][1]
        if today["close"] is None or today["volume"] <= 0:
            continue

        # ── 條件二：近 30 個交易日平均每日振幅 ──
        amps = []
        for i, (day, it) in enumerate(series):
            prev_close = series[i - 1][1]["close"] if i > 0 else None
            a = daily_amplitude(it, prev_close)
            if a is not None:
                amps.append(a)
        amps = amps[-AMP_WINDOW:]
        amp30 = round(statistics.mean(amps), 2) if len(amps) >= AMP_MIN_SAMPLES else None

        prev_close = series[-2][1]["close"] if len(series) >= 2 else None
        amp_today = daily_amplitude(today, prev_close)
        base = today.get("prev") or prev_close
        change_pct = (round((today["close"] - base) / base * 100.0, 2)
                      if base and base > 0 else None)

        # ── 量比：今日量 / 近 5 日均量（不含今日）──
        prior_vols = [it["volume"] for day, it in series[:-1]][-VOL_AVG_DAYS:]
        vol5 = statistics.mean(prior_vols) if len(prior_vols) >= 3 else None
        vol_ratio = round(today["volume"] / vol5, 2) if vol5 and vol5 > 0 else None

        dt_vol = dt_stat.get(code)
        dt_ratio = (round(dt_vol / today["volume"] * 100.0, 1)
                    if dt_vol and today["volume"] > 0 else None)

        universe.append({
            "code": code,
            "name": today["name"],
            "market": today["market"],
            "industry": industry.get(code, "未分類"),
            "open": today.get("open"),
            "high": today["high"],
            "low": today["low"],
            "close": today["close"],
            "change_pct": change_pct,
            "volume_lots": round(today["volume"] / 1000),
            "turnover": today["turnover"],
            "amp_today": round(amp_today, 2) if amp_today is not None else None,
            "amp30": amp30,
            "amp_days": len(amps),
            "vol_ratio": vol_ratio,
            "dt_ratio": dt_ratio,
            "closes": [round(it["close"], 2) for day, it in series[-SPARK_DAYS:]],
        })

    if not universe:
        log("今日 universe 為空，異常")
        return None

    # ── 條件三：族群強度排名 ──
    sectors = {}
    for s in universe:
        sectors.setdefault(s["industry"], []).append(s)

    total_turnover = sum(s["turnover"] for s in universe) or 1
    sector_rows = []
    for name, members in sectors.items():
        if name == "未分類" or len(members) < HOT_SECTOR_MIN_STOCKS:
            continue
        turnover = sum(m["turnover"] for m in members)
        weights = [(m["change_pct"], m["turnover"]) for m in members
                   if m["change_pct"] is not None]
        w_sum = sum(w for _, w in weights) or 1
        w_change = round(sum(c * w for c, w in weights) / w_sum, 2)
        ratios = [m["vol_ratio"] for m in members if m["vol_ratio"] is not None]
        med_vr = round(statistics.median(ratios), 2) if ratios else None
        ups = sum(1 for m in members if (m["change_pct"] or 0) > 0)
        sector_rows.append({
            "name": name,
            "stocks": len(members),
            "turnover": turnover,
            "turnover_share": round(turnover / total_turnover * 100.0, 2),
            "w_change": w_change,
            "med_vol_ratio": med_vr,
            "gainers_pct": round(ups / len(members) * 100.0, 1),
        })

    ch_sorted = sorted(r["w_change"] for r in sector_rows)
    vr_sorted = sorted(r["med_vol_ratio"] for r in sector_rows
                       if r["med_vol_ratio"] is not None)
    ts_sorted = sorted(r["turnover_share"] for r in sector_rows)
    for r in sector_rows:
        heat = (0.45 * pct_rank(ch_sorted, r["w_change"])
                + 0.30 * pct_rank(vr_sorted, r["med_vol_ratio"])
                + 0.25 * pct_rank(ts_sorted, r["turnover_share"]))
        r["heat"] = round(heat, 1)
    sector_rows.sort(key=lambda r: r["heat"], reverse=True)
    for i, r in enumerate(sector_rows, start=1):
        r["rank"] = i
        r["hot"] = i <= HOT_SECTOR_TOP and r["w_change"] > 0
    hot_names = {r["name"] for r in sector_rows if r["hot"]}
    sector_rank = {r["name"]: r["rank"] for r in sector_rows}
    log(f"族群 {len(sector_rows)} 個，熱門：{'、'.join(sorted(hot_names)) or '（無）'}")

    # ── 三條件判定與計分 ──
    to_sorted = sorted(s["turnover"] for s in universe)
    amp_sorted = sorted(s["amp30"] for s in universe if s["amp30"] is not None)
    results = []
    for s in universe:
        cond_vol = (s["volume_lots"] >= VOL_MIN_LOTS
                    or s["turnover"] >= TURNOVER_MIN)
        cond_amp = s["amp30"] is not None and s["amp30"] >= AMP30_MIN
        is_focus = (s["vol_ratio"] is not None
                    and s["vol_ratio"] >= FOCUS_VOL_RATIO
                    and s["turnover"] >= FOCUS_TURNOVER)
        in_hot_sector = s["industry"] in hot_names
        cond_hot = in_hot_sector or is_focus
        passed = int(cond_vol) + int(cond_amp) + int(cond_hot)
        if passed < 2:
            continue

        hot_pct = 0.0
        if in_hot_sector:
            rank = sector_rank.get(s["industry"], HOT_SECTOR_TOP)
            hot_pct = 100.0 - (rank - 1) * (40.0 / HOT_SECTOR_TOP)
        if is_focus:
            hot_pct = max(hot_pct, 90.0)
        score = round(0.35 * pct_rank(to_sorted, s["turnover"])
                      + 0.35 * pct_rank(amp_sorted, s["amp30"])
                      + 0.30 * hot_pct)

        s2 = dict(s)
        s2.update({
            "cond": {"vol": cond_vol, "amp": cond_amp, "hot": cond_hot},
            "hot_reason": ("熱門族群" if in_hot_sector else "") +
                          ("＋" if in_hot_sector and is_focus else "") +
                          ("焦點資金股" if is_focus else ""),
            "sector_rank": sector_rank.get(s["industry"]),
            "passed": passed,
            "score": score,
        })
        results.append(s2)

    results.sort(key=lambda x: (-x["passed"], -x["score"], -x["turnover"]))
    results = results[:OUTPUT_MAX_STOCKS]
    n3 = sum(1 for r in results if r["passed"] == 3)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_at_taipei": datetime.now(TPE).strftime("%Y-%m-%d %H:%M"),
        "data_date": f"{latest[:4]}-{latest[4:6]}-{latest[6:8]}",
        "trading_days": len(days),
        "params": {
            "vol_min_lots": VOL_MIN_LOTS,
            "turnover_min": TURNOVER_MIN,
            "amp_window": AMP_WINDOW,
            "amp_min_samples": AMP_MIN_SAMPLES,
            "amp30_min": AMP30_MIN,
            "hot_sector_top": HOT_SECTOR_TOP,
            "focus_vol_ratio": FOCUS_VOL_RATIO,
            "focus_turnover": FOCUS_TURNOVER,
        },
        "counts": {
            "universe": len(universe),
            "pass3": n3,
            "pass2": len(results) - n3,
        },
        "sectors": sector_rows,
        "stocks": results,
    }
    log(f"名單：三條件全中 {n3} 檔、兩條件觀察 {len(results) - n3} 檔"
        f"（universe {len(universe)}）")
    return out


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="當沖選股雷達資料管線")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--backfill", type=int, default=0,
                    help="往回補幾個日曆日（首次建置填 60）")
    ap.add_argument("--force", action="store_true", help="已存在的日檔也重抓")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="只重建 day-trading.json，不連網")
    args = ap.parse_args()

    out_dir = args.out
    os.makedirs(os.path.join(out_dir, "daily"), exist_ok=True)

    industry = {}
    dt_stat = {}

    if not args.rebuild_only:
        today = datetime.now(TPE).date()
        start = today - timedelta(days=max(args.backfill, 0))
        day = start
        fetched = 0
        while day <= today:
            if day.weekday() < 5:  # 週末必休市，不打 API
                status = collect_day(out_dir, f"{day:%Y%m%d}", force=args.force)
                if status == "ok":
                    fetched += 1
            day += timedelta(days=1)
        log(f"本次新抓 {fetched} 個交易日")
        prune_snapshots(out_dir)

        industry = fetch_industry_map(out_dir)

        days = list_snapshot_days(out_dir)
        if days:
            dt_stat = fetch_twse_daytrade_stat(days[-1])
    else:
        path = os.path.join(out_dir, "industry.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                industry = json.load(f).get("map", {})

    result = scan(out_dir, industry, dt_stat)
    if result is None:
        log("掃描無結果，不更新 day-trading.json")
        return 1

    out_path = os.path.join(out_dir, "day-trading.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    log(f"✅ 已寫入 {out_path}"
        f"（{result['data_date']}／{len(result['stocks'])} 檔）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
