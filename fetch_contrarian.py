# -*- coding: utf-8 -*-
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
SHEET_SCAN_SOURCES = ["掃描結果", "選股結果"]
SHEET_CHIPS = "籌碼面資料"
SHEET_OUTPUT = "逆勢抗跌掃描"

MIN_VOLUME_LOTS = 300
OBSERVE_THRESHOLD = 30


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


def debug_list_worksheets(sh):
    names = [ws.title for ws in sh.worksheets()]
    print(f"[DEBUG] 目前試算表分頁：{names}")
    return names


def parse_num(v):
    try:
        s = str(v).replace(",", "").replace("%", "").strip()
        return float(s) if s else 0
    except (ValueError, TypeError):
        return 0


def safe_text(v):
    return str(v).strip() if v is not None else ""


def fetch_market_index_change():
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
        return "黃燈", 50
    elif current_price > ma20 and change_pct > 0:
        return "綠燈", 40
    else:
        return "平燈", 50


def calc_institutional_streaks(gc):
    sh = get_sheet(gc)

    try:
        ws = sh.worksheet(SHEET_CHIPS)
    except gspread.exceptions.WorksheetNotFound:
        names = debug_list_worksheets(sh)
        print(f"[WARN] 找不到『{SHEET_CHIPS}』分頁，現有分頁：{names}")
        return {}

    records = ws.get_all_values()
    if len(records) < 2:
        print(f"[WARN] 『{SHEET_CHIPS}』分頁沒有資料")
        return {}

    header = records[0]
    rows = records[1:]

    col_map = {}
    for i, h in enumerate(header):
        h_clean = safe_text(h)
        if "日期" in h_clean:
            col_map["date"] = i
        elif h_clean == "代號":
            col_map["code"] = i
        elif "投信" in h_clean:
            col_map["trust"] = i
        elif "外資" in h_clean:
            col_map["foreign"] = i
        elif "合計" in h_clean or "三法人" in h_clean:
            col_map["total"] = i

    if "code" not in col_map:
        print(f"[WARN] 無法解析『{SHEET_CHIPS}』欄位")
        return {}

    stock_data = {}

    for row in rows:
        try:
            code = safe_text(row[col_map["code"]]) if col_map["code"] < len(row) else ""
            if not code:
                continue

            date_str = safe_text(row[col_map["date"]]) if "date" in col_map and col_map["date"] < len(row) else ""
            trust_val = row[col_map["trust"]] if "trust" in col_map and col_map["trust"] < len(row) else "0"
            foreign_val = row[col_map["foreign"]] if "foreign" in col_map and col_map["foreign"] < len(row) else "0"
            total_val = row[col_map["total"]] if "total" in col_map and col_map["total"] < len(row) else "0"

            if code not in stock_data:
                stock_data[code] = []

            stock_data[code].append({
                "date": date_str,
                "trust": parse_num(trust_val),
                "foreign": parse_num(foreign_val),
                "total": parse_num(total_val),
            })
        except Exception:
            continue

    streaks = {}
    for code, entries in stock_data.items():
        entries.sort(key=lambda x: x["date"], reverse=True)

        trust_streak = 0
        foreign_streak = 0

        for entry in entries:
            if entry["trust"] > 0:
                trust_streak += 1
            else:
                break

        for entry in entries:
            if entry["foreign"] > 0:
                foreign_streak += 1
            else:
                break

        max_streak = max(trust_streak, foreign_streak)
        if max_streak >= 15:
            score = 35
        elif max_streak >= 10:
            score = 28
        elif max_streak >= 5:
            score = 20
        elif max_streak >= 3:
            score = 12
        elif max_streak >= 1:
            score = 5
        else:
            score = 0

        latest_total = entries[0]["total"] if entries else 0

        streaks[code] = {
            "trust_streak": trust_streak,
            "foreign_streak": foreign_streak,
            "institutional_score": score,
            "latest_total": latest_total,
        }

    return streaks


def read_existing_scan_results(gc):
    sh = get_sheet(gc)

    for sheet_name in SHEET_SCAN_SOURCES:
        try:
            ws = sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            names = debug_list_worksheets(sh)
            print(f"[WARN] 找不到『{sheet_name}』分頁，現有分頁：{names}")
            continue

        records = ws.get_all_values()
        if len(records) < 2:
            print(f"[WARN] 『{sheet_name}』分頁沒有資料列")
            continue

        header = records[0]
        rows = records[1:]
        col_map = build_scan_column_map(header)

        if "code" not in col_map:
            print(f"[WARN] 無法解析『{sheet_name}』欄位。表頭：{header}")
            continue

        results = rows_to_scan_results(rows, col_map)
        print(f"[INFO] 已從『{sheet_name}』讀取 {len(results)} 檔掃描結果")
        if results:
            return results

    print("[WARN] 所有掃描來源分頁都沒有可用股票資料。請先確認主掃描已寫入『掃描結果』或『選股結果』。")
    return {}


def build_scan_column_map(header):
    aliases = {
        "code": ["代號", "股票代號", "證券代號", "stockCode", "code"],
        "name": ["名稱", "股票名稱", "證券名稱", "stockName", "name"],
        "price": ["現價", "收盤價", "成交價", "close", "price"],
        "signal": ["BB訊號", "訊號", "布林訊號", "BB信號"],
        "n_target": ["N字目標", "N目標價", "N字目標價"],
        "start_point": ["起漲點", "起漲價"],
        "bandwidth": ["帶寬", "帶寬%", "BB帶寬"],
        "vol_ratio": ["量比", "成交量比", "volumeRatio"],
        "market": ["市場", "市場別"],
        "badges": ["badges", "徽章", "標籤"],
    }

    col_map = {}
    for i, h in enumerate(header):
        h_clean = safe_text(h)
        for key, names in aliases.items():
            if h_clean in names or any(name in h_clean for name in names if len(name) >= 3):
                col_map.setdefault(key, i)
    return col_map


def rows_to_scan_results(rows, col_map):
    results = {}

    for row in rows:
        try:
            code_idx = col_map.get("code")
            if code_idx is None or code_idx >= len(row):
                continue

            code = safe_text(row[code_idx]).replace(".TW", "").replace(".TWO", "")
            if not code:
                continue

            def safe_get(key, default=""):
                idx = col_map.get(key)
                if idx is None or idx >= len(row):
                    return default
                return row[idx]

            results[code] = {
                "name": safe_text(safe_get("name")),
                "price": parse_num(safe_get("price")),
                "signal": safe_text(safe_get("signal")),
                "n_target": parse_num(safe_get("n_target")),
                "start_point": parse_num(safe_get("start_point")),
                "bandwidth": parse_num(safe_get("bandwidth")),
                "vol_ratio": parse_num(safe_get("vol_ratio")),
                "market": safe_text(safe_get("market")) or "上市",
                "badges": safe_text(safe_get("badges")),
            }
        except Exception as e:
            print(f"[WARN] 略過一列掃描資料：{e}")
            continue

    return results

def fetch_stock_daily_change(code, market="上市"):
    suffix = ".TW" if market == "上市" else ".TWO"
    symbol = f"{code}{suffix}"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "2d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        volumes = result["indicators"]["quote"][0]["volume"]

        valid_closes = [c for c in closes if c is not None]
        valid_volumes = [v for v in volumes if v is not None]

        if len(valid_closes) < 2:
            return None

        current = valid_closes[-1]
        previous = valid_closes[-2]
        change_pct = ((current - previous) / previous) * 100
        volume = valid_volumes[-1] if valid_volumes else 0

        return {
            "price": round(current, 2),
            "change_pct": round(change_pct, 2),
            "volume": volume,
        }
    except Exception:
        return None


def calc_contrarian_score(stock_change_pct, market_change_pct):
    score_a = 20 if stock_change_pct > 2 else 15 if stock_change_pct > 0 else 0

    if stock_change_pct > 0:
        rel_strength = abs(market_change_pct) + stock_change_pct
    else:
        rel_strength = abs(market_change_pct) - abs(stock_change_pct)

    if rel_strength >= 3:
        score_b = 20
    elif rel_strength >= 2:
        score_b = 15
    elif rel_strength >= 1:
        score_b = 10
    elif rel_strength >= 0.5:
        score_b = 5
    else:
        score_b = 0

    return max(score_a, score_b), round(rel_strength, 2)


def calc_ntheory_score(scan_data):
    score = 0
    signal = scan_data.get("signal", "")
    n_target = scan_data.get("n_target", 0)
    bandwidth = scan_data.get("bandwidth", 100)
    badges = scan_data.get("badges", "")

    if n_target > 0:
        score += 15
    if "起漲" in signal:
        score += 15
    elif "多頭" in signal:
        score += 10
    elif "收斂" in signal:
        score += 5

    if 0 < bandwidth < 8:
        score += 10
    elif 0 < bandwidth < 10:
        score += 5

    if "放量起漲" in badges:
        score += 5

    return min(score, 45)


def write_results(gc, formal_list, watch_list, light, change_pct, change_pts, threshold):
    sh = get_sheet(gc)
    try:
        ws = sh.worksheet(SHEET_OUTPUT)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_OUTPUT, rows=500, cols=20)

    ws.clear()
    now = datetime.datetime.now().strftime("%Y/%m/%d %H:%M")

    ws.update(
        range_name="A1",
        values=[[
            f"掃描時間：{now}",
            f"大盤燈號：{light}",
            f"漲跌幅：{change_pct}%（{change_pts}點）",
            f"正式門檻：{threshold}",
            f"觀察門檻：{OBSERVE_THRESHOLD}",
            f"正式：{len(formal_list)} 檔",
            f"觀察：{len(watch_list)} 檔",
        ]]
    )

    headers = [
        "類別", "代號", "名稱", "收盤價", "漲跌%", "成交量(張)",
        "相對抗跌度", "投信連買(日)", "外資連買(日)",
        "N字目標", "BB訊號", "帶寬%", "法人分", "N字分", "抗跌分", "總分"
    ]
    ws.update(range_name="A3", values=[headers])

    data_rows = []

    for c in formal_list:
        data_rows.append([
            "正式", c["code"], c["name"], c["price"], c["change_pct"], c["volume_lots"],
            c["rel_strength"], c["trust_streak"], c["foreign_streak"], c["n_target"],
            c["signal"], c["bandwidth"], c["institutional_score"], c["ntheory_score"],
            c["contrarian_score"], c["total_score"]
        ])

    for c in watch_list:
        data_rows.append([
            "觀察", c["code"], c["name"], c["price"], c["change_pct"], c["volume_lots"],
            c["rel_strength"], c["trust_streak"], c["foreign_streak"], c["n_target"],
            c["signal"], c["bandwidth"], c["institutional_score"], c["ntheory_score"],
            c["contrarian_score"], c["total_score"]
        ])

    if data_rows:
        ws.update(range_name="A4", values=data_rows)
    else:
        ws.update(range_name="A4", values=[["無資料", "主掃描來源沒有可用股票資料，或全部低於觀察門檻", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]])

    print(f"\n已寫入「{SHEET_OUTPUT}」分頁，正式 {len(formal_list)} 檔，觀察 {len(watch_list)} 檔")


def run_contrarian_scan():
    print("=" * 60)
    print("逆勢抗跌標的掃描模組 啟動")
    print("=" * 60)

    change_pct, change_pts, current_price = fetch_market_index_change()
    ma20 = fetch_market_index_ma20()
    light, threshold = determine_market_light(change_pct, current_price, ma20)

    print("\n大盤狀態：")
    print(f" 加權指數：{current_price}")
    print(f" 漲跌幅：{change_pct}%（{change_pts}點）")
    print(f" MA20：{ma20}")
    print(f" 燈號：{light}（正式門檻：{threshold}；觀察門檻：{OBSERVE_THRESHOLD}）")

    gc = get_gspread_client()
    sh = get_sheet(gc)
    debug_list_worksheets(sh)

    print("\n計算法人連買天數...")
    streaks = calc_institutional_streaks(gc)
    print(f" 已計算 {len(streaks)} 檔股票的法人連買數據")

    print("\n讀取既有掃描結果...")
    scan_results = read_existing_scan_results(gc)
    print(f" 已讀取 {len(scan_results)} 檔掃描結果")

    formal_list = []
    watch_list = []
    stats = {
        "processed": 0,
        "no_daily": 0,
        "low_volume": 0,
        "watch_due_to_no_net_buy": 0,
        "below_observe": 0,
    }

    for code, scan_data in scan_results.items():
        stats["processed"] += 1

        market = scan_data.get("market", "上市")
        daily = fetch_stock_daily_change(code, market)
        if not daily:
            stats["no_daily"] += 1
            continue

        volume_lots = daily["volume"] / 1000
        if volume_lots < MIN_VOLUME_LOTS:
            stats["low_volume"] += 1
            continue

        streak_data = streaks.get(code, {})
        institutional_score = streak_data.get("institutional_score", 0)
        latest_total = streak_data.get("latest_total", 0)

        contrarian_score, rel_strength = calc_contrarian_score(daily["change_pct"], change_pct)
        ntheory_score = calc_ntheory_score(scan_data)
        total_score = round(institutional_score + ntheory_score + contrarian_score, 1)

        item = {
            "code": code,
            "name": scan_data.get("name", ""),
            "price": daily["price"],
            "change_pct": daily["change_pct"],
            "volume_lots": round(volume_lots),
            "rel_strength": rel_strength,
            "trust_streak": streak_data.get("trust_streak", 0),
            "foreign_streak": streak_data.get("foreign_streak", 0),
            "n_target": scan_data.get("n_target", 0),
            "signal": scan_data.get("signal", ""),
            "bandwidth": scan_data.get("bandwidth", 0),
            "institutional_score": institutional_score,
            "ntheory_score": ntheory_score,
            "contrarian_score": contrarian_score,
            "total_score": total_score,
        }

        if latest_total <= 0:
            if total_score >= OBSERVE_THRESHOLD:
                watch_list.append(item)
                stats["watch_due_to_no_net_buy"] += 1
            else:
                stats["below_observe"] += 1
            time.sleep(0.1)
            continue

        if total_score >= threshold:
            formal_list.append(item)
        elif total_score >= OBSERVE_THRESHOLD:
            watch_list.append(item)
        else:
            stats["below_observe"] += 1

        time.sleep(0.1)

    formal_list.sort(key=lambda x: x["total_score"], reverse=True)
    watch_list.sort(key=lambda x: x["total_score"], reverse=True)

    print("\n淘汰/保留統計：")
    print(f" 處理總數：{stats['processed']}")
    print(f" 取不到日資料：{stats['no_daily']}")
    print(f" 成交量不足(<{MIN_VOLUME_LOTS}張)：{stats['low_volume']}")
    print(f" 三法人非買超但保留至觀察：{stats['watch_due_to_no_net_buy']}")
    print(f" 分數低於觀察門檻{OBSERVE_THRESHOLD}：{stats['below_observe']}")
    print(f" 正式名單：{len(formal_list)}")
    print(f" 觀察名單：{len(watch_list)}")

    write_results(gc, formal_list, watch_list, light, change_pct, change_pts, threshold)
    return formal_list, watch_list


if __name__ == "__main__":
    formal, watch = run_contrarian_scan()

    if formal:
        print("\nTop 5 正式候選股：")
        for i, r in enumerate(formal[:5], 1):
            print(
                f" {i}. {r['code']} {r['name']} "
                f"| 總分{r['total_score']} "
                f"| 漲跌{r['change_pct']}% "
                f"| 投信連買{r['trust_streak']}日 "
                f"| N目標{r['n_target']}"
            )
    else:
        print("\n今日無正式達標候選股")

    if watch:
        print("\nTop 5 觀察名單：")
        for i, r in enumerate(watch[:5], 1):
            print(
                f" {i}. {r['code']} {r['name']} "
                f"| 總分{r['total_score']} "
                f"| 漲跌{r['change_pct']}% "
                f"| 投信連買{r['trust_streak']}日 "
                f"| N目標{r['n_target']}"
            )
