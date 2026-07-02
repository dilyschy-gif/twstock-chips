const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "public, max-age=60, s-maxage=300"
};

export async function onRequestGet() {
  try {
    const [usdTwd, twii] = await Promise.all([
      fetchQuote("TWD=X"),
      fetchQuote("^TWII")
    ]);

    return jsonResponse({
      updatedAt: new Date().toISOString(),
      source: "Yahoo Finance",
      usdTwd,
      twii
    });
  } catch (error) {
    return jsonResponse({ error: error.message || "總體資料讀取失敗" }, 502);
  }
}

async function fetchQuote(symbol) {
  const endpoint = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?range=5d&interval=1d`;
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
  if (error) throw new Error(error.description || `${symbol} 查詢失敗`);
  if (!result) throw new Error(`${symbol} 沒有資料`);

  const quote = result.indicators?.quote?.[0] || {};
  const closes = (quote.close || []).filter(Number.isFinite);
  if (closes.length < 1) throw new Error(`${symbol} 收盤資料不足`);

  const value = closes[closes.length - 1];
  const previous = closes.length >= 2 ? closes[closes.length - 2] : value;
  const change = value - previous;
  const changePct = previous ? (change / previous) * 100 : 0;

  return {
    symbol,
    value: round(value, symbol === "TWD=X" ? 4 : 2),
    previous: round(previous, symbol === "TWD=X" ? 4 : 2),
    change: round(change, symbol === "TWD=X" ? 4 : 2),
    changePct: round(changePct, 2),
    currency: result.meta?.currency || "",
    exchangeTimezoneName: result.meta?.exchangeTimezoneName || ""
  };
}

function round(value, decimals) {
  const base = 10 ** decimals;
  return Math.round(Number(value) * base) / base;
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: JSON_HEADERS
  });
}
