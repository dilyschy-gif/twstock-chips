# CLAUDE.md — twstock-chips 程式檢驗手冊

> 依據 2026-07-06 全鏈路除錯實戰整理。放在 repo 根目錄。
> 用途：Claude（或任何人）對本系統做檢查、除錯、驗證名單時，照本手冊執行。

## 交付規矩（最優先）

任何程式碼更動，一律交付**整份可直接覆蓋的完整檔案**，不給局部片段。
局部片段曾導致整檔被誤覆蓋（只剩兩個函式、main 全滅）。唯一例外：單行設定值修改可用文字說明。

## 系統架構（資料流）

```
TWSE T86 API ─┐
TPEX dailyTrade ─┴→ fetch_chips.py（16:35）→ Google Sheet「籌碼面資料」（滾動 20 交易日）
                                                    ↓
mopsfin 月營收 CSV → monthly_revenue_fetch.py → 「月營收」分頁    ↓
                                        main_stock_scanner.py（17:00 主掃描）
                                         ┌──────────┴──────────┐
                                         ↓                     ↓
                     「選股結果」分頁（主升段）       「V型反轉掃描」分頁（V0～V3）
                                         └──────────┬──────────┘
                                                    ↓
                          export_sheet_to_data_json.py → data.json → Cloudflare Pages
                                                    ↓
                              app.js 前端純過濾出「右腳醞釀精選」
另一條線：
「選股結果」→ GAS syncRightFootSignals()（右腳正式訊號 → BB-8 signals 分頁）
           → BB-8 morningReport/eveningReport（Telegram 推播）
```

兩份試算表：
- BB-8 表 `16SNd2Tsi...`：config / signals / tg_log 等
- StockRadar 表 `1lxp1HcYf...`：選股結果 / 籌碼面資料 / 月營收 / 股票資料庫 等
  （注意：**沒有**叫「右腳醞釀精選」的分頁，右腳資料就在「選股結果」）

## 右腳醞釀篩選邏輯（app.js）

必要條件（全部要成立）：
1. 文字含「N字突破」（注意：「未突破N字頸線」不含此子字串，天然不誤判）
2. 移除「未創波段新高」後，不含「波段新高」（**必須先 replaceAll 再判斷**，
   否則「未創波段新高」會被誤判成已創新高，全名單歸零——這是埋伏 bug，勿刪那行）
3. 投信連買 ≥ 5 日（regex `投信連買(\d+)日` 取**第一個** match，badge 的階梯值 5/10/15
   優先於 chipsDetail 的實際值，屬已知顯示特性，非 bug）

加分：帶寬收斂 → ★ 排最前。收緊名單改 `BREWING_MIN_TRUST_STREAK`。

## V型反轉掃描邏輯（v_reversal.py）

目標是找「跌深後剛轉折」，不是找已完成一大段漲幅的股票。法人籌碼只加分，不是硬門檻。

- V0 醞釀：10 日左臂跌幅至少 10%、近 3 根至少 2 根黑 K、RSI ≤ 38、接近 V 底、
  20 日均量至少 300 張，等待第一根轉折紅 K。
- V1 轉折：漲幅至少 4% 或紅 K 實體至少 0.8 ATR、收盤位於當日振幅上方 20%、
  上影占比不超過 20%、收復前一根壓力，量比至少 1.2。
- 漲停例外：若漲幅至少 9.5%、收近最高且幾乎無上影，量比可放寬至 0.45，
  用來保留「低量鎖漲停」而非誤刪。
- V2 確認：V1 後守住紅 K 中值，站上 `max(轉折高點、MA5、轉折前兩日高點)`。
- V3 成形：自 V 底收復左臂跌幅 50%，站上 MA10 且 MA5 上彎；已不屬最早買點。
- VX 失敗：跌破 V 底、連兩日收盤跌破紅 K 中值，或第三日仍未站回 MA5。

預設不把 VX 寫入前端名單；除錯時可設定 `V_INCLUDE_FAILED=1`。前端 K 線會顯示 V 底、
紅 K 中值、V2 確認價、50% 與 61.8% 收復價。零成交量補值與疑似除權息造成的 35% 跳空會排除。

規則測試：`python -m unittest discover -v`。測試包含 3037 類型、V0、V2、VX、長上影排除、
低量漲停例外與零量補值。

## 標準檢驗流程（驗證任何一份右腳名單）

用 gviz 唯讀查詢，不動生產資料：
`https://docs.google.com/spreadsheets/d/{ID}/gviz/tq?tqx=out:csv&v={隨機數}&sheet={分頁}&tq={查詢}`

**第 0 步：先驗地基，再驗名單。**

1. **資料完整性**：`select A,D,count(B) group by A,D` 對「籌碼面資料」
   - 必須恰好 20 個交易日 × 每天「上市」（~1080 檔）＋「上櫃」（~810 檔）兩列都在
   - 有任何日期缺市場或缺日 → 名單全部存疑，先修資料再談名單
2. **名單完整性重算**：對「選股結果」`select A,B,F,T,W where T contains '投信連買'`
   逐列套三條件，結果必須與面板名單一檔不差（多了漏了都是 bug）
3. **連買天數獨立驗算**：抽 2~3 檔 `select A,F where B='{代號}' order by A desc`
   手算連續買超天數（>0 連續），必須與 chipsDetail 的「投信連買N日」一致
4. **新鮮度**：每檔 chipsDetail 的日期必須是最新交易日。掛舊日期＝該股所屬市場資料缺漏
   （例：上櫃股掛 07-02 表示 07-03、07-06 上櫃資料沒進來）

注意 gviz 快取：同 URL 會回舊結果，每次查詢換 `v=` 參數。網址過長會被拒，一檔一查。

## 已知陷阱（今日全數實戰踩過）

### 資料源
| 症狀 | 真因 | 解法 |
|---|---|---|
| TWSE 回 307 | WAF 擋無 cookie 的裸 requests（資料其實存在） | Session 先訪 t86 網頁拿 cookie；307/403/429 重建 session 重試 3 次 |
| TPEX 回 200 但永遠無資料 | 舊版 3itrade_hedge_result.php 已退役 | 新版 `POST /www/zh-tw/insti/dailyTrade`（民國年、**必須 POST**，GET 回空白） |
| TPEX 24 欄對照 | — | [10]外資合計 [13]投信 [22]自營合計 [23]三法人；驗算 10+13+22≈23 |
| 月營收 InvalidJSONError | 無去年基期 → yoy 為 NaN/inf，JSON 不容 | 寫入前 `_json_safe()` 清洗成空字串 |

### 寫入與去重
- 去重必須用**「日期×市場」**，不能只用日期——否則半套日期（只有上市）永遠補不進上櫃
- 回補（`--backfill 20`）可安全重跑：已存在的（日期,市場）自動跳過
- **改完程式必須 commit 後才重跑 workflow**——workflow 跑的是 repo 裡的碼，不是你螢幕上的碼

### GAS / signals
- `getLatestSignals` 日期一律 `normSignalDate()` 正規化＋`isValidSignalDate()` 過濾——
  中文雜訊列排序會大於日期字串，被誤抓成「最新訊號」
- 同天多筆訊號：日期若為 Date 物件，`===` 是物件比對只會中一筆，必須轉字串再比

### 判讀
- **連買天數在資料有缺口時會灌水或低估**（統一超缺口期虛胖成 20 日，補完為真實 5 日；
  原相舊資料 6 日，補完為真實 11 日）。缺口未補齊前，任何連買數字不可信
- badge 階梯值（5/10/15）≠ 實際連買天數（chipsDetail 才是實際值）
- 歷史清零重建後，前 5 個交易日右腳名單必然偏短，屬正常現象非 bug

### CI/CD
- push 被 reject（fetch first）＝撞車：workflow 期間 main 有新 commit。Re-run 即癒；
  永久解為 push 前 `git pull --rebase`
- Node.js 20 deprecation 是警告非錯誤，不影響執行

## 標準操作順序（改碼後重建資料）

1. commit 程式碼（整檔覆蓋）
2. **Backfill Chips**（驗收：log 出現「已有 上市 資料，該部分跳過」＋「✅ 寫入 8xx 筆」）
3. **Daily Taiwan Stock Scanner**（~8 分鐘）
4. **Update Cloudflare Pages data**（~16 秒）
5. 依「標準檢驗流程」四步驗收

一步跑完再跑下一步。每日自動排程順序：16:35 籌碼 → 17:00 主掃描 → data.json 更新。

## 紀律條款（R2 條款）

名單驗證通過 ≠ 買進訊號。新邏輯或資料重建後的名單，觀察三個交易日，
穩定留在名單上的才進入深入研究；一天進一天出的是雜訊。工具越準，越不需要急。
