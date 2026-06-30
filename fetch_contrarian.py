"""
逆勢抗跌標的掃描模組 (fetch_contrarian.py)
==========================================
觸發條件：大盤跌幅 >= 3% 時自動啟動
核心邏輯：不是找跌最少的股票，而是找最先轉強的股票

評分維度（滿分100）：
  - 法人連買分（35%）：投信/外資連續買超天數
    - N字突破分（45%）：突破頸線 + 創波段新高（從既有掃描結果讀取）
      - 抗跌分（20%）：當日翻紅或跌幅遠小於大盤

      大盤燈號系統：
        紅燈：當日跌幅 >= 3%（門檻提高至70）
          黃燈：前日跌幅 >= 3%，當日止跌或小漲（啟動抗跌掃描）
            綠燈：站回 MA20（門檻降低至40）
              平燈：正常狀態（門檻50）

              作者：R2 for Dilys
              """

import os
import json
import time
import datetime
import requests
import gspread
from google.oauth2.service_account import Credentials

# ──────────────────────────────────────────
# Google Sheets 認證
# ──────────────────────────────────────────

SCOPES = [
      "https://www.googleapis.com/auth/spreadsheets",
      "https://www.googleapis.com/auth/drive",
]

def get_gspread_client():
      creds_json = os.environ.get("GOOGLE_CREDENTIALS", "")
      if not creds_json:
                raise RuntimeError("GOOGLE_CREDENTIALS 環境變數未設定")
            creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

# ──────────────────────────────────────────
# 取得加權指數當日漲跌幅
# ──────────────────────────────────────────

def fetch_market_index_change():
      """從 Yahoo Finance 取得台灣加權指數 (^TWII) 當日漲跌幅"""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
    params = {"range": "5d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
              r = requests.get(url, params=params, headers=headers, timeout=15)
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
      """取得加權指數近20日均線"""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII"
    params = {"range": "1mo", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
              r = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        valid = [c for c in closes if c is not None]
        if len(valid) >= 20:
                      return round(sum(valid[-20:]) / 20, 2)
                  return round(sum(valid) / len(valid), 2)
except Exception:
        return 0

# ──────────────────────────────────────────
# 大盤燈號判定
# ──────────────────────────────────────────

def determine_market_light(change_pct, current_price, ma20):
      """
          紅燈：跌幅 >= 3%
              黃燈：跌幅 1.5%~3%，或前日紅燈今日止跌
                  綠燈：站回 MA20 且漲幅 > 0
                      平燈：其他
                          """
    if change_pct <= -3:
              return "紅燈", 70
elif change_pct <= -1.5:
        return "黃燈", 60
elif current_price > ma20 and change_pct > 0:
        return "綠燈", 40
else:
        return "平燈", 50

# ──────────────────────────────────────────
# 從既有 Sheets 讀取籌碼數據，計算法人連買天數
# ──────────────────────────────────────────

def calc_institutional_streaks(gc):
      """讀取籌碼面分頁，計算每支股票的投信/外資連續買超天數"""
    sh = gc.open_by_key(SHEET_ID)
    try:
              ws = sh.worksheet("籌碼面")
except gspread.exceptions.WorksheetNotFound:
        print("[WARN] 找不到「籌碼面」分頁")
        return {}

    records = ws.get_all_values()
    if len(records) < 2:
              return {}

    header = records[0]
    rows = records[1:]

    col_map = {}
    for i, h in enumerate(header):
              h_clean = h.strip()
        if "日期" in h_clean:
                      col_map["date"] = i
elif "代號" in h_clean:
            col_map["code"] = i
elif "投信" in h_clean:
            col_map["trust"] = i
elif "外資" in h_clean:
            col_map["foreign"] = i
elif "三法人" in h_clean:
            col_map["total"] = i

    if "code" not in col_map:
              print("[WARN] 無法解析籌碼面欄位")
        return {}

    stock_data = {}
    for row in rows:
              try:
                            code = str(row[col_map["code"]]).strip()
                            date_str = str(row[col_map.get("date", 0)]).strip()
                            trust_val = row[col_map.get("trust", -1)] if "trust" in col_map else "0"
                            foreign_val = row[col_map.get("foreign", -1)] if "foreign" in col_map else "0"
                            total_val = row[col_map.get("total", -1)] if "total" in col_map else "0"

            def parse_num(v):
                              try:
                                                    return float(str(v).replace(",", "").strip())
except (ValueError, TypeError):
                    return 0

            if code not in stock_data:
                              stock_data[code] = []
                          stock_data[code].append({
                                            "date": date_str,
                                            "trust": parse_num(trust_val),
                                            "foreign": parse_num(foreign_val),
                                            "total": parse_num(total_val),
                          })
except (IndexError, ValueError):
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

        score = 0
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

        latest_total = entries[0]["total"] if entries else 0
        streaks[code] = {
                      "trust_streak": trust_streak,
                      "foreign_streak": foreign_streak,
                      "institutional_score": score,
                      "latest_total": latest_total,
        }

    return streaks

# ──────────────────────────────────────────
# 從既有掃描結果讀取 N字理論數據
# ──────────────────────────────────────────

def read_existing_scan_results(gc):
      """讀取選股結果分頁，取得每支股票的 N字目標、起漲點、帶寬等"""
    sh = gc.open_by_key(SHEET_ID)
    try:
              ws = sh.worksheet("選股結果")
except gspread.exceptions.WorksheetNotFound:
        ws = sh.sheet1

    records = ws.get_all_values()
    if len(records) < 2:
              return {}

    header = records[0]
    rows = records[1:]

    col_map = {}
    for i, h in enumerate(header):
              h_clean = h.strip()
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
elif "compositeScore" in h_clean:
            col_map["composite"] = i
elif "市場" in h_clean:
            col_map["market"] = i
elif "產業" in h_clean:
            col_map["industry"] = i
elif h_clean == "badges":
            col_map["badges"] = i

    results = {}
    for row in rows:
              try:
                            code = str(row[col_map.get("code", 0)]).strip()
                            if not code:
                                              continue

                            def safe_float(idx_key):
                                              try:
                                                                    return float(str(row[col_map.get(idx_key, -1)]).replace(",", "").strip())
    except (ValueError, IndexError, KeyError):
                    return 0

            results[code] = {
                              "name": str(row[col_map.get("name", 1)]).strip() if "name" in col_map else "",
                              "price": safe_float("price"),
                              "signal": str(row[col_map.get("signal", -1)]).strip() if "signal" in col_map else "",
                              "n_target": safe_float("n_target"),
                              "start_point": safe_float("start_point"),
                              "bandwidth": safe_float("bandwidth"),
                              "vol_ratio": safe_float("vol_ratio"),
                              "composite": safe_float("composite"),
                              "market": str(row[col_map.get("market", -1)]).strip() if "market" in col_map else "",
                              "industry": str(row[col_map.get("industry", -1)]).strip() if "industry" in col_map else "",
                              "badges": str(row[col_map.get("badges", -1)]).strip() if "badges" in col_map else "",
            }
except (IndexError, ValueError):
            continue

    return results

# ──────────────────────────────────────────
# 從 Yahoo Finance 取得個股當日漲跌幅
# ──────────────────────────────────────────

def fetch_stock_daily_change(code, market="上市"):
      """取得個股當日漲跌幅和成交量"""
    suffix = ".TW" if market == "上市" else ".TWO"
    symbol = f"{code}{suffix}"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": "2d", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
              r = requests.get(url, params=params, headers=headers, timeout=10)
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

# ──────────────────────────────────────────
# 抗跌分數計算
# ──────────────────────────────────────────

def calc_contrarian_score(stock_change_pct, market_change_pct):
      """
          軌道A：當日翻紅（max 20分）
              軌道B：相對抗跌度（max 20分）
                  取兩者較高者（滿分20）
                      """
    score_a = 0
    if stock_change_pct > 0:
              score_a = 15
        if stock_change_pct > 2:
                      score_a = 20

    rel_strength = abs(market_change_pct) - abs(min(stock_change_pct, 0))
    score_b = 0
    if stock_change_pct > 0:
              rel_strength = abs(market_change_pct) + stock_change_pct

    if rel_strength >= 3:
              score_b = 20
elif rel_strength >= 2:
        score_b = 15
elif rel_strength >= 1:
        score_b = 10
elif rel_strength >= 0.5:
        score_b = 5

    return max(score_a, score_b), round(rel_strength, 2)

# ──────────────────────────────────────────
# N字突破分數（從既有掃描結果推算）
# ──────────────────────────────────────────

def calc_ntheory_score(scan_data):
      """
          根據既有掃描結果中的 BB訊號、N字目標判斷突破狀態
              滿分45分
                  """
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

# ──────────────────────────────────────────
# 主掃描邏輯
# ──────────────────────────────────────────

def run_contrarian_scan():
      print("=" * 60)
    print("逆勢抗跌標的掃描模組 啟動")
    print("=" * 60)

    change_pct, change_pts, current_price = fetch_market_index_change()
    ma20 = fetch_market_index_ma20()
    light, threshold = determine_market_light(change_pct, current_price, ma20)

    print(f"\n大盤狀態：")
    print(f"   加權指數：{current_price}")
    print(f"   漲跌幅：{change_pct}%（{change_pts}點）")
    print(f"   MA20：{ma20}")
    print(f"   燈號：{light}（篩選門檻：{threshold}）")

    gc = get_gspread_client()

    print("\n計算法人連買天數...")
    streaks = calc_institutional_streaks(gc)
    print(f"   已計算 {len(streaks)} 檔股票的法人連買數據")

    print("\n讀取既有掃描結果...")
    scan_results = read_existing_scan_results(gc)
    print(f"   已讀取 {len(scan_results)} 檔掃描結果")

    candidates = []
    processed = 0

    for code, scan_data in scan_results.items():
              processed += 1
        if processed % 50 == 0:
                      print(f"   處理中... {processed}/{len(scan_results)}")

        market = scan_data.get("market", "上市")
        daily = fetch_stock_daily_change(code, market)
        if not daily:
                      continue

        volume_lots = daily["volume"] / 1000
        if volume_lots < 1000:
                      continue

        streak_data = streaks.get(code, {})
        institutional_score = streak_data.get("institutional_score", 0)
        latest_total = streak_data.get("latest_total", 0)

        if latest_total <= 0:
                      continue

        contrarian_score, rel_strength = calc_contrarian_score(
                      daily["change_pct"], change_pct
        )

        ntheory_score = calc_ntheory_score(scan_data)

        total = (
                      institutional_score * (35 / 35) +
                      ntheory_score * (45 / 45) +
                      contrarian_score * (20 / 20)
        )

        if total >= threshold:
                      candidates.append({
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
                                        "total_score": round(total, 1),
                      })

        time.sleep(0.3)

    candidates.sort(key=lambda x: x["total_score"], reverse=True)
    print(f"\n篩選完成：{len(candidates)} 檔達標")

    write_results(gc, candidates, light, change_pct, change_pts, threshold)
    return candidates

# ──────────────────────────────────────────
# 寫入結果到 Google Sheets
# ──────────────────────────────────────────

def write_results(gc, candidates, light, change_pct, change_pts, threshold):
      sh = gc.open_by_key(SHEET_ID)
    tab_name = "逆勢抗跌掃描"
    try:
              ws = sh.worksheet(tab_name)
except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=200, cols=20)

    ws.clear()
    now = datetime.datetime.now().strftime("%Y/%m/%d %H:%M")

    ws.update("A1", [[
              f"掃描時間：{now}",
              f"大盤燈號：{light}",
              f"漲跌幅：{change_pct}%（{change_pts}點）",
              f"篩選門檻：{threshold}",
              f"達標：{len(candidates)} 檔"
    ]])

    headers = [
              "代號", "名稱", "收盤價", "漲跌%", "成交量(張)",
              "相對抗跌度", "投信連買(日)", "外資連買(日)",
              "N字目標", "BB訊號", "帶寬%",
              "法人分", "N字分", "抗跌分", "總分"
    ]
    ws.update("A3", [headers])

    if candidates:
              data_rows = []
        for c in candidates[:50]:
                      data_rows.append([
                                        c["code"],
                                        c["name"],
                                        c["price"],
                                        c["change_pct"],
                                        c["volume_lots"],
                                        c["rel_strength"],
                                        c["trust_streak"],
                                        c["foreign_streak"],
                                        c["n_target"],
                                        c["signal"],
                                        c["bandwidth"],
                                        c["institutional_score"],
                                        c["ntheory_score"],
                                        c["contrarian_score"],
                                        c["total_score"],
                      ])
                  ws.update("A4", data_rows)

    print(f"\n已寫入「{tab_name}」分頁，共 {len(candidates)} 筆")

# ──────────────────────────────────────────
# 入口
# ──────────────────────────────────────────

if __name__ == "__main__":
      results = run_contrarian_scan()

    if results:
              print("\nTop 5 候選股：")
        for i, r in enumerate(results[:5], 1):
                      print(
                                        f"  {i}. {r['code']} {r['name']} "
                                        f"| 總分{r['total_score']} "
                                        f"| 漲跌{r['change_pct']}% "
                                        f"| 投信連買{r['trust_streak']}日 "
                                        f"| N目標{r['n_target']}"
                      )
else:
        print("\n今日無達標候選股")
