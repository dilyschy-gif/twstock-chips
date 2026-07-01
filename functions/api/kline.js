const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "public, max-age=300, s-maxage=1800"
};

export async function onRequestGet({ request }) {
  const url = new URL(request.url);
  const code = sanitizeCode(url.searchParams.get("code"));
  const market = (url.searchParams.get("market") || "").trim();

  if (!code) {
    return jsonResponse({ error: "缺少股票代號" }, 400);
  }

  const symbols = buildCandidateSymbols(code, market);
  let lastError = "查無 K 線資料";

  for (const symbol of symbols) {
    try {
      const candles = await fetchYahooCandles(symbol);
      if (candles.length >= 20) {
        return jsonResponse({ code, symbol, candles: candles.slice(-60) });
      }
      lastError = `${symbol} K 線資料不足`;
    } catch (error) {
      lastError = error.message;
    }
  }

  return jsonResponse({ error: lastError, code, tried: symbols }, 404);
}

function sanitizeCode(value) {
  const text = String(value || "").trim().toUpperCase();
  const match = text.match(/^[0-9A-Z]{2,8}$/);
  return match ? text : "";
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
  const endpoint = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?range=4mo&interval=1d&events=history&includeAdjustedClose=true`;
  const response = await fetch(endpoint, {
    headers: {
      "accept": "application/json",
      "user-agent": "twstock-chips-cloudflare-pages"
    }
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
  const candles = [];

  for (let index = 0; index < timestamps.length; index += 1) {
    const open = quote.open?.[index];
    const high = quote.high?.[index];
    const low = quote.low?.[index];
    const close = quote.close?.[index];
    if (![open, high, low, close].every(Number.isFinite)) continue;

    candles.push({
      date: new Date(timestamps[index] * 1000).toISOString().slice(0, 10),
      open: roundPrice(open),
      high: roundPrice(high),
      low: roundPrice(low),
      close: roundPrice(close),
      volume: quote.volume?.[index] || 0
    });
  }

  return candles;
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
