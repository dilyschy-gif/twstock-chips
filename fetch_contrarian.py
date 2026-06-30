"""
逆勢抗跌標的掃描模組 (fetch_contrarian.py)
功能：
1. 從 Google Sheet 的「選股結果」「籌碼面」讀資料
2. 從 Yahoo Finance 取得加權指數與個股日資料
3. 產生正式名單 + 觀察名單
4. 輸出淘汰原因統計，方便除錯
作者：BB-8 for Dilys
"""

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
USER_AGENT = {"User-Agent": "Mozilla/5.0"}
REQUEST_SLEEP = 0.15
MIN_VOLUME_LOTS = 300
WATCHLIST_MIN_SCORE = 30


def get_gspread_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS 環境變數未設定")
    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def parse_num(v):
    try:
        text = str(v).replace(",", "").replace("%", "").strip()
        return float(text) if text else 0.0
    except (ValueError, TypeError):
        return 0.0


def safe_div(a, b):
    return a / b if b else 0


def fetch_market_index_change():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
    params = {"range": "5d", "interval": "1d"}
    try:
        r = requests.get(url, params=params, headers=USER_AGENT, timeout=15)
        r.raise_for_status()
        data = r.json()
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        valid_closes = [c for c in closes if c is not None]
        if len(valid_closes) < 2:
            return 0, 0, 0
        current = valid_closes[-1]
        previous = valid_closes[-2]
        change_pct = safe_div(current - previous, previous) * 100
        change_points = current - previous
        return round(change_pct, 2), round(change_points, 2), round(current, 2)
    except Exception as e:
        print(f"[WARN] 無法取得加權指數: {e}")
        return 0, 0, 0


def fetch_market_index_ma20():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
    params = {"range": "3mo", "interval": "1d"}
    try:
        r = requests.get(url, params=params, headers=USER_AGENT, timeout=15)
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
        print(f"[WARN] 無法取得加權 MA20: {e}")
        return 0


def determine_market_light(change_pct, current_price, ma20):
    if change_pct <= -3:
        return "紅燈", 70
    elif change_pct <= -1.5:
        return "黃燈", 60
    elif current_price > ma20 and change_pct > 0:
        return "綠燈", 40
    else:
        return "平燈", 50


def calc_institutional_streaks(gc):
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("籌碼面")
    except gspread.exceptions.WorksheetNotFound:
        print("[WARN] 找不到籌碼面分頁")
        return {}

    records = ws.get_all_values()
    if len(records) < 2:
        print("[WARN] 籌碼面分頁沒有資料")
        return {}

    header = records[0]
    rows = records[1:]

    col_map = {}
    for i, h in enumerate(header):
        h_clean = str(h).strip()
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
        print("[WARN] 無法解析籌碼面欄位")
        return {}

    stock_data = {}
    for row in rows:
        try:
            code = str(row[col_map["code"]]).strip()
            if not code:
                continue
            date_str = str(row[col_map.get("date", 0)]).strip()
            trust_val = row[col_map["trust"]] if "trust" in col_map and col_map["trust"] < len(row) else "0"
            foreign_val = row[col_map["foreign"]] if "foreign" in col_map and col_map["foreign"] < len(row) else "0"
            total_val = row[col_map["total"]] if "total" in col_map and col_map["total"] < len(row) else "0"
            stock_data.setdefault(code, []).append({
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
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("選股結果")
    except gspread.exceptions.WorksheetNotFound:
        print("[WARN] 找不到選股結果分頁")
        return {}

    records = ws.get_all_values()
    if len(records) < 2:
        print("[WARN] 選股結果分頁沒有資料")
        return {}

    header = records[0]
    rows = records[1:]

    col_map = {}
    for i, h in enumerate(header):
        h_clean = str(h).strip()
        if h_clean == "代號":
            col_map["code"] = i
        elif h_clean == "名稱":
            col_map["name"] = i
        elif h_clean == "現價":
            col_map["price"] = i
        elif h_clean in ("BB訊號", "訊號"):
            col_map["signal"] = i
        elif "N字目標" in h_clean:
            col_map["n_target"] = i
        elif "起漲點" in h_clean:
            col_map["start_point"] = i
        elif "帶寬" in h_clean:
            col_map["bandwidth"] = i
        elif "量比" in h_clean:
            col_map["vol_ratio"] = i
        elif "市場" in h_clean:
            col_map["market"] = i
        elif h_clean == "badges":
            col_map["badges"] = i

    if "code" not in col_map:
        print("[WARN] 選股結果缺少代號欄位")
        return {}

    results = {}
    for row in rows:
        try:
            code = str(row[col_map["code"]]).strip()
            if not code:
                continue

            def safe_get(key, default=""):
                idx = col_map.get(key)
                if idx is None or idx >= len(row):
                    return default
                return row[idx]

            def safe_float(key):
                return parse_num(safe_get(key, "0"))

            results[code] = {
                "name": str(safe_get("name")).strip(),
                "price": safe_float("price"),
                "signal": str(safe_get("signal")).strip(),
                "n_target": safe_float("n_target"),
                "start_point": safe_float("start_point"),
                "bandwidth": safe_float("bandwidth"),
                "vol_ratio": safe_float("vol_ratio"),
                "market": str(safe_get("market", "上市")).strip() or "上市",
                "badges": str(safe_get("badges")).strip(),
            }
        except Exception:
            continue

    return results


def fetch_stock_daily_change(code, market="上市"):
    suffix = ".TW" if market == "上市" else ".TWO"
    symbol = f"{code}{suffix}"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "5d", "interval": "1d"}
    try:
        r = requests.get(url, params=params, headers=USER_AGENT, timeout=12)
        r.raise_for_status()
        data = r.json()
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        volumes = result["indicators"]["quote"][0]["volume"]
        valid_pairs = [(c, v) for c, v in zip(closes, volumes) if c is not None]
        if len(valid_pairs) < 2:
            return None
        current, current_vol = valid_pairs[-1]
        previous, _ = valid_pairs[-2]
        change_pct = safe_div(current - previous, previous) * 100
        return {
            "price": round(current, 2),
            "change_pct": round(change_pct, 2),
            "volume": int(current_vol or 0),
        }
    except Exception:
        return None


def calc_contrarian_score(stock_change_pct, market_change_pct):
    score_a = 0
    if stock_change_pct > 0:
        score_a = 15
    if stock_change_pct > 2:
        score_a = 20

    if stock_change_pct > 0:
        rel_strength = abs(market_change_pct) + stock_change_pct
    else:
        rel_strength = abs(market_change_pct) - abs(stock_change_pct)

    score_b = 0
    if rel_strength >= 3:
        score_b = 20
    elif rel_strength >= 2:
        score_b = 15
    elif rel_strength >= 1:
        score_b = 10
    elif rel_strength >= 0.5:
        score_b = 5

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


def classify_reject_reason(has_daily, volume_lots, latest_total, total, threshold):
    if not has_daily:
        return "取不到日資料"
    if volume_lots < MIN_VOLUME_LOTS:
        return f"成交量不足<{MIN_VOLUME_LOTS}張"
    if latest_total <= 0:
        return "三法人非買超"
    if total < WATCHLIST_MIN_SCORE:
        return f"總分低於觀察門檻{WATCHLIST_MIN_SCORE}"
    if total < threshold:
        return f"介於觀察與正式門檻({WATCHLIST_MIN_SCORE}-{threshold-0.1:.1f})"
    return "其他"


def write_results(gc, candidates, watchlist, stats, light, change_pct, change_pts, threshold):
    sh = gc.open_by_key(SHEET_ID)
    tab_name = "逆勢抗跌掃描"
    try:
        ws = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=500, cols=20)

    ws.clear()
    now = datetime.datetime.now().strftime("%Y/%m/%d %H:%M")

    ws.update("A1", [[
        f"掃描時間：{now}",
        f"大盤燈號：{light}",
        f"漲跌幅：{change_pct}%（{change_pts}點）",
        f"正式門檻：{threshold}",
        f"正式名單：{len(candidates)} 檔",
    ]])

    ws.update("A3", [[
        "處理檔數", stats["processed"],
        "取不到日資料", stats["skip_no_daily"],
        f"量不足(<{MIN_VOLUME_LOTS}張)", stats["skip_low_volume"],
        "三法人非買超", stats["skip_negative_inst"],
        f"觀察名單({WATCHLIST_MIN_SCORE}+)", len(watchlist),
        "分數未達觀察", stats["skip_below_watchlist"],
    ]])

    headers = [
        "代號", "名稱", "收盤價", "漲跌%", "成交量(張)",
        "相對抗跌度", "投信連買(日)", "外資連買(日)", "三法人合計",
        "N字目標", "BB訊號", "帶寬%",
        "法人分", "N字分", "抗跌分", "總分", "名單類型", "淘汰/保留原因"
    ]
    ws.update("A5", [headers])

    rows = []
    for c in candidates:
        rows.append([
            c["code"], c["name"], c["price"], c["change_pct"], c["volume_lots"],
            c["rel_strength"], c["trust_streak"], c["foreign_streak"], c["latest_total"],
            c["n_target"], c["signal"], c["bandwidth"],
            c["institutional_score"], c["ntheory_score"], c["contrarian_score"], c["total_score"],
            "正式", c["reason"]
        ])

    for c in watchlist:
        rows.append([
            c["code"], c["name"], c["price"], c["change_pct"], c["volume_lots"],
            c["rel_strength"], c["trust_streak"], c["foreign_streak"], c["latest_total"],
            c["n_target"], c["signal"], c["bandwidth"],
            c["institutional_score"], c["ntheory_score"], c["contrarian_score"], c["total_score"],
            "觀察", c["reason"]
        ])

    if rows:
        ws.update("A6", rows)

    print(f"\n已寫入「{tab_name}」分頁，正式 {len(candidates)} 檔，觀察 {len(watchlist)} 檔")


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
    print(f" 燈號：{light}（正式門檻：{threshold}；觀察門檻：{WATCHLIST_MIN_SCORE}）")

    gc = get_gspread_client()

    print("\n計算法人連買天數...")
    streaks = calc_institutional_streaks(gc)
    print(f" 已計算 {len(streaks)} 檔股票的法人連買數據")

    print("\n讀取既有掃描結果...")
    scan_results = read_existing_scan_results(gc)
    print(f" 已讀取 {len(scan_results)} 檔掃描結果")

    candidates = []
    watchlist = []
    stats = {
        "processed": 0,
        "skip_no_daily": 0,
        "skip_low_volume": 0,
        "skip_negative_inst": 0,
        "skip_below_watchlist": 0,
    }

    for code, scan_data in scan_results.items():
        stats["processed"] += 1
        if stats["processed"] % 50 == 0:
            print(f" 處理中... {stats['processed']}/{len(scan_results)}")

        market = scan_data.get("market", "上市")
        daily = fetch_stock_daily_change(code, market)
        if not daily:
            stats["skip_no_daily"] += 1
            continue

        volume_lots = round((daily["volume"] or 0) / 1000, 0)
        streak_data = streaks.get(code, {})
        institutional_score = streak_data.get("institutional_score", 0)
        latest_total = streak_data.get("latest_total", 0)

        contrarian_score, rel_strength = calc_contrarian_score(daily["change_pct"], change_pct)
        ntheory_score = calc_ntheory_score(scan_data)
        total = institutional_score + ntheory_score + contrarian_score

        reason = classify_reject_reason(True, volume_lots, latest_total, total, threshold)

        if volume_lots < MIN_VOLUME_LOTS:
            stats["skip_low_volume"] += 1
            continue

        if total < WATCHLIST_MIN_SCORE:
            stats["skip_below_watchlist"] += 1
            continue

        stock_row = {
            "code": code,
            "name": scan_data.get("name", ""),
            "price": daily["price"],
            "change_pct": daily["change_pct"],
            "volume_lots": int(volume_lots),
            "rel_strength": rel_strength,
            "trust_streak": streak_data.get("trust_streak", 0),
            "foreign_streak": streak_data.get("foreign_streak", 0),
            "latest_total": latest_total,
            "n_target": scan_data.get("n_target", 0),
            "signal": scan_data.get("signal", ""),
            "bandwidth": scan_data.get("bandwidth", 0),
            "institutional_score": institutional_score,
            "ntheory_score": ntheory_score,
            "contrarian_score": contrarian_score,
            "total_score": round(total, 1),
            "reason": reason,
        }

        if latest_total <= 0:
            stats["skip_negative_inst"] += 1
            stock_row["reason"] = "觀察保留：三法人未同步，但技術面/抗跌達標"
            watchlist.append(stock_row)
        elif total >= threshold:
            stock_row["reason"] = f"正式入選：總分達正式門檻{threshold}"
            candidates.append(stock_row)
        else:
            stock_row["reason"] = f"觀察保留：總分{round(total,1)}介於{WATCHLIST_MIN_SCORE}-{threshold-0.1:.1f}"
            watchlist.append(stock_row)

        time.sleep(REQUEST_SLEEP)

    candidates.sort(key=lambda x: x["total_score"], reverse=True)
    watchlist.sort(key=lambda x: x["total_score"], reverse=True)

    print("\n淘汰/保留統計：")
    print(f" 處理總數：{stats['processed']}")
    print(f" 取不到日資料：{stats['skip_no_daily']}")
    print(f" 成交量不足(<{MIN_VOLUME_LOTS}張)：{stats['skip_low_volume']}")
    print(f" 三法人非買超但保留至觀察：{stats['skip_negative_inst']}")
    print(f" 分數低於觀察門檻{WATCHLIST_MIN_SCORE}：{stats['skip_below_watchlist']}")
    print(f" 正式名單：{len(candidates)}")
    print(f" 觀察名單：{len(watchlist)}")

    write_results(gc, candidates, watchlist, stats, light, change_pct, change_pts, threshold)
    return candidates, watchlist, stats


if __name__ == "__main__":
    results, watchlist, stats = run_contrarian_scan()

    if results:
        print("\nTop 5 正式候選股：")
        for i, r in enumerate(results[:5], 1):
            print(
                f" {i}. {r['code']} {r['name']} "
                f"| 總分{r['total_score']} "
                f"| 漲跌{r['change_pct']}% "
                f"| 投信連買{r['trust_streak']}日 "
                f"| N目標{r['n_target']}"
            )
    else:
        print("\n今日無正式達標候選股")

    if watchlist:
        print("\nTop 5 觀察名單：")
        for i, r in enumerate(watchlist[:5], 1):
            print(
                f" {i}. {r['code']} {r['name']} "
                f"| 總分{r['total_score']} "
                f"| 原因{r['reason']}"
            )
