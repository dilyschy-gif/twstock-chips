# -*- coding: utf-8 -*-
"""
fetch_chips.py v4 局部修補 — 用下面兩個函式取代 v3 裡的同名函式
════════════════════════════════════════════════════
診斷結論（2026-07-06，經瀏覽器實測驗證）：
  A. TWSE 307：資料其實存在（瀏覽器查 7/1 回 stat:OK），
     是 TWSE WAF 對「無 cookie 的裸 requests」回 307 打發。
     修法：requests.Session 先訪問 t86 網頁拿 cookie，
     再打 API；被打發時重建 session 重試，最多 3 次、間隔 10 秒。
  B. TPEX 200 但永遠無資料：舊版 3itrade_hedge_result.php 已隨網站改版退役。
     新版端點實測確認：
       POST https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade
       body: type=Daily&sect=EW&date=115/07/03&response=json（民國年）
       回應: {"tables":[{"date","totalCount","fields"(24欄),"data":[[...]]}], ...}
     欄位順序與 v3 註解一致（[10]外資合計 [13]投信 [22]自營合計 [23]三法人），
     已用 2026-07-03 全部 827 檔四碼股驗算 外資+投信+自營=合計，0 檔不合。
     關鍵差異：必須用 POST，用 GET 會拿到空回應。
其他建議：backfill_main 裡的 time.sleep(4) 改成 time.sleep(6)。
"""
import requests
import time

# ══════════ 抓 TWSE 三大法人（上市）v4 ══════════
_twse_session = None

def _get_twse_session():
    """建立帶 cookie 的 TWSE session（模擬先開網頁再查資料）。
    TWSE WAF 會對「沒有 cookie 的 API 請求」回 307，
    先 GET 一次 t86 網頁頁面拿 cookie，後續 API 就走得通。
    """
    global _twse_session
    if _twse_session is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        })
        try:
            s.get("https://www.twse.com.tw/zh/trading/foreign/t86.html", timeout=30)
        except Exception as e:
            print(f"TWSE session 預熱失敗（繼續嘗試）: {e}")
        _twse_session = s
    return _twse_session

def fetch_twse_chips(date_str):
    """
    date_str: "20260615" 格式，回傳 list of dict（單位：張）
    T86 欄位對照同 v3（[4]+[7]外資 / [10]投信 / [11]自營 / [18]合計）。
    v4：Session + cookie 預熱 + 被 WAF 打發時最多重試 3 次（間隔 10 秒）。
    """
    global _twse_session
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={date_str}&selectType=ALLBUT0999&response=json"
    headers = {"Referer": "https://www.twse.com.tw/zh/trading/foreign/t86.html"}
    for attempt in range(1, 4):
        try:
            s = _get_twse_session()
            res = s.get(url, headers=headers, timeout=30, allow_redirects=True)
            print(f"TWSE T86 回應碼: {res.status_code}（第 {attempt} 次）")
            if res.status_code in (301, 302, 307, 403, 429):
                _twse_session = None  # 被 WAF 打發：重建 session 再試
                time.sleep(10)
                continue
            if res.status_code != 200:
                return []
            data = res.json()
            if data.get("stat") != "OK" or not data.get("data"):
                print(f"TWSE 無資料: {data.get('stat')}")
                return []
            date_label = str(data.get("date", date_str))
            if "/" in date_label:
                parts = date_label.split("/")
                if len(parts) == 3 and int(parts[0]) < 1000:
                    date_label = f"{int(parts[0])+1911}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
            elif len(date_label) == 8 and date_label.isdigit():
                date_label = f"{date_label[:4]}-{date_label[4:6]}-{date_label[6:8]}"
            results = []
            for row in data["data"]:
                code = str(row[0]).strip()
                if not code.isdigit() or len(code) != 4:
                    continue
                if len(row) < 19:
                    continue  # 欄位數不足，跳過並依靠 sanity_check 察覺異常
                results.append({
                    "date":    date_label,
                    "code":    code,
                    "name":    str(row[1]).strip(),
                    "market":  "上市",
                    "foreign": to_lots(parse_num(row[4]) + parse_num(row[7])),
                    "sitc":    to_lots(parse_num(row[10])),
                    "dealer":  to_lots(parse_num(row[11])),
                    "total":   to_lots(parse_num(row[18]))
                })
            print(f"TWSE：取得 {len(results)} 檔")
            sanity_check(results, "TWSE")
            return results
        except Exception as e:
            print(f"TWSE 抓取失敗（第 {attempt} 次）: {e}")
            time.sleep(10)
    print("TWSE：3 次嘗試均失敗，放棄此日期")
    return []

# ══════════ 抓 TPEX 三大法人（上櫃）v4 ══════════
def fetch_tpex_chips(date_str):
    """
    date_str: "20260615" 格式
    v4：改打新版端點（舊版 .php 已退役，回 200+空 aaData 不可救）。
    實測確認：必須 POST + form body，GET 會回空白。
    回應 tables[0].data 為 24 欄制，欄位對照：
      [0]代號 [1]名稱
      [2-4]  外資及陸資(不含外資自營商) 買/賣/淨
      [5-7]  外資自營商 買/賣/淨
      [8-10] 外資及陸資合計 買/賣/淨
      [11-13]投信 買/賣/淨
      [14-16]自營商(自行買賣) 買/賣/淨
      [17-19]自營商(避險) 買/賣/淨
      [20-22]自營商合計 買/賣/淨
      [23]   三大法人買賣超合計
    （已用 2026-07-03 全 827 檔驗算 [10]+[13]+[22]=[23]，0 檔不合）
    """
    year  = int(date_str[:4]) - 1911
    mm    = date_str[4:6]
    dd    = date_str[6:8]
    tw_date    = f"{year}/{mm}/{dd}"
    date_label = f"{date_str[:4]}-{mm}-{dd}"
    url = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.tpex.org.tw/zh-tw/mainboard/trading/major-institutional/detail/day.html",
    }
    payload = {"type": "Daily", "sect": "EW", "date": tw_date, "response": "json"}
    try:
        res = requests.post(url, headers=headers, data=payload, timeout=30)  # ← 必須 POST
        print(f"TPEX 回應碼: {res.status_code}")
        if res.status_code != 200 or not res.text.strip().startswith("{"):
            print(f"TPEX 非 JSON 回應: {res.text[:120]}")
            return []
        data = res.json()
        rows = []
        for t in (data.get("tables") or []):
            if t.get("data"):
                rows = t["data"]
                break
        if not rows:
            print("TPEX 無資料（假日或未公布）")
            return []
        results = []
        for row in rows:
            code = str(row[0]).strip()
            if not code.isdigit() or len(code) != 4:
                continue
            if len(row) < 24:
                continue  # 欄位數不足，跳過並依靠 sanity_check 察覺異常
            results.append({
                "date":    date_label,
                "code":    code,
                "name":    str(row[1]).strip(),
                "market":  "上櫃",
                "foreign": to_lots(parse_num(row[10])),   # 外資及陸資合計買賣超（張）
                "sitc":    to_lots(parse_num(row[13])),   # 投信買賣超（張）
                "dealer":  to_lots(parse_num(row[22])),   # 自營商合計買賣超（張）
                "total":   to_lots(parse_num(row[23]))    # 三大法人合計（張）
            })
        print(f"TPEX：取得 {len(results)} 檔")
        sanity_check(results, "TPEX")
        return results
    except Exception as e:
        print(f"TPEX 抓取失敗: {e}")
        return []
