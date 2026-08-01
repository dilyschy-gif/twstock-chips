// functions/api/kline.js
//
// 修正重點（相對於 repo 現行版本）
// ------------------------------------------------------------------
// 1) 【不再靜默丟棄最新一根 K 棒】← 這是圖表停在 7/30 的頭號嫌疑犯
//    舊版：if (![open, high, low, close].every(Number.isFinite)) continue;
//    Yahoo 對台股「當日尚未結算」的 bar 常常只有 close，其餘欄位為 null，
//    這行會把整根 K 棒扔掉，而且完全沒有任何提示。
//    新版：只要 close 有效就保留，缺的欄位用 close 補齊，並標記 partial。
//
// 2) 【日期改用台北時間換算】
//    舊版 toISOString() 取的是 UTC 日期。台股是 UTC+8，目前開盤 09:00 CST
//    = 01:00 UTC 剛好同日，所以「碰巧」正確；但只要 Yahoo 改用收盤時刻或
//    其他基準，日期標籤就會整條退一天。這裡明確加 +8 小時，不再靠運氣。
//
// 3) 【拿掉會餵舊資料的快取】
//    舊版 s-maxage=1800，前端雖然帶了 cache:"no-store"，那只影響瀏覽器快取，
//    Cloudflare 邊緣仍可能回最多 30 分鐘前的版本。這支 API 很輕量，
//    正確性優先，改為 no-store，並對 Yahoo 子請求關閉 Workers fetch cache。
//
// 4) 【新增診斷欄位】
//    回傳 lastDate / lastClose / dropped / fetchedAt / attempts。
//    掃描結果來自 Google Sheet、K 線來自 Yahoo，本來就是兩個資料源，
//    重點不是強求永遠同步，而是要「看得見不同步」。
//
// 5) 【依日期去重並排序】避免 Yahoo 偶發回傳重複的最後一根。
//
// 注意：刻意「不」過濾四價相同、低量的 bar —— 漲停鎖住的當沖冷門股
// 本來就長那樣（例如 2485 兆赫 2026-07-31），過濾掉會把真訊號洗掉。

const TAIPEI_OFFSET_MS = 8 * 60 * 60 * 1000;

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store, max-age=0",
  "x-content-type-options": "nosniff"
};

export async function onRequestGet({ request }) {
  const url = new URL(request.url);
  const code = sanitizeCode(url.searchParams.get("code"));
  const market = (url.searchParams.get("market") || "").trim();
  const days = sanitizeDays(url.searchParams.get("days"));

  if (!code) {
    return jsonResponse({ error: "缺少股票代號" }, 400);
  }

  const symbols = buildCandidateSymbols(code, market);
  const attempts = [];
  let lastError = "查無 K 線資料";

  for (const symbol of symbols) {
    try {
      const parsed = await fetchYahooCandles(symbol);

      attempts.push({
        symbol,
        total: parsed.candles.length,
        dropped: parsed.dropped,
        lastDate: parsed.candles.length ? parsed.candles[parsed.candles.length - 1].date : null
      });

      if (parsed.candles.length >= 20) {
        const sliced = parsed.candles.slice(-days);
        const last = sliced[sliced.length - 1];

        return jsonResponse({
          code,
          symbol,
          days,
          candles: sliced,
          // --- 診斷欄位：前端可比對 Sheet 的訊號日，落後時主動示警 ---
          lastDate: last ? last.date : null,
          lastClose: last ? last.close : null,
          lastIsPartial: last ? Boolean(last.partial) : false,
          dropped: parsed.dropped,
          fetchedAt: taipeiIsoString(new Date()),
          attempts
        });
      }

      lastError = `${symbol} K 線資料不足（僅 ${parsed.candles.length} 筆）`;
    } catch (error) {
      attempts.push({ symbol, error: error.message });
      lastError = error.message;
    }
  }

  return jsonResponse({ error: lastError, code, tried: symbols, attempts }, 404);
}

function sanitizeCode(value) {
  const text = String(value || "").trim().toUpperCase();
  const match = text.match(/^[0-9A-Z]{2,8}$/);
  return match ? text : "";
}

function sanitizeDays(value) {
  const days = Number(value || 60);
  return [60, 120, 180].includes(days) ? days : 60;
}

function buildCandidateSymbols(code, market) {
  const symbols = [];
  const push = (suffix) => {
    const symbol = `${code}${suffix}`;
    if (!symbols.includes(symbol)) symbols.push(symbol);
  };

  if (market.includes("上櫃") || market.includes("興櫃")) {
    push(".TWO");
    push(".TW");
  } else {
    push(".TW");
    push(".TWO");
  }

  return symbols;
}

async function fetchYahooCandles(symbol) {
  const endpoint =
    `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}` +
    `?range=1y&interval=1d&events=history&includeAdjustedClose=true`;

  const response = await fetch(endpoint, {
    headers: {
      accept: "application/json",
      "user-agent": "twstock-chips-cloudflare-pages"
    },
    // 關閉 Workers 對這個子請求的快取，確保收盤後拿到的是最新結果
    cf: { cacheTtl: 0, cacheEverything: false }
  });

  if (!response.ok) {
    throw new Error(`${symbol} 讀取失敗：${response.status}`);
  }

  const payload = await response.json();
  const result = payload?.chart?.result?.[0];
  const error = payload?.chart?.error;

  if (error) {
    throw new Error(error.description || `${symbol} 查詢失敗`);
  }
  if (!result?.timestamp?.length) {
    throw new Error(`${symbol} 沒有日 K 資料`);
  }

  const quote = result.indicators?.quote?.[0] || {};
  const timestamps = result.timestamp || [];

  // 用 Map 依日期去重，後出現的覆蓋先出現的
  const byDate = new Map();
  let dropped = 0;

  for (let index = 0; index < timestamps.length; index += 1) {
    const close = toNumberOrNull(quote.close?.[index]);

    // 只要收盤價有效就保留這根 K 棒
    if (close === null) {
      dropped += 1;
      continue;
    }

    const open = toNumberOrNull(quote.open?.[index]);
    const high = toNumberOrNull(quote.high?.[index]);
    const low = toNumberOrNull(quote.low?.[index]);
    const volume = toNumberOrNull(quote.volume?.[index]);

    // partial = true 代表這根是用 close 補出來的（當日尚未結算）
    const partial = open === null || high === null || low === null;

    const resolvedOpen = open === null ? close : open;
    const resolvedHigh = high === null ? Math.max(resolvedOpen, close) : high;
    const resolvedLow = low === null ? Math.min(resolvedOpen, close) : low;

    const date = taipeiDateString(timestamps[index]);

    byDate.set(date, {
      date,
      open: roundPrice(resolvedOpen),
      high: roundPrice(resolvedHigh),
      low: roundPrice(resolvedLow),
      close: roundPrice(close),
      volume: volume === null ? 0 : volume,
      partial
    });
  }

  const candles = Array.from(byDate.values()).sort((a, b) =>
    a.date < b.date ? -1 : a.date > b.date ? 1 : 0
  );

  return { candles, dropped };
}

function toNumberOrNull(value) {
  if (value === null || value === undefined) return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

// Yahoo 的日 K timestamp 是交易所當地開盤時刻的 epoch。
// 直接用 toISOString() 得到的是 UTC 日期，這裡明確 +8 小時再取日期字串。
function taipeiDateString(epochSeconds) {
  return new Date(Number(epochSeconds) * 1000 + TAIPEI_OFFSET_MS).toISOString().slice(0, 10);
}

function taipeiIsoString(date) {
  return new Date(date.getTime() + TAIPEI_OFFSET_MS).toISOString().replace("Z", "+08:00");
}

function roundPrice(value) {
  return Math.round(Number(value) * 100) / 100;
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: JSON_HEADERS
  });
}
