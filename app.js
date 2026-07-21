const demoStocks = [
  { code: "2330", name: "台積電", signal: "strong", score: 92, note: "法人買超，量價同步轉強", market: "上市" },
  { code: "2317", name: "鴻海", signal: "watch", score: 73, note: "籌碼回溫，等待突破確認", market: "上市" },
  { code: "2454", name: "聯發科", signal: "strong", score: 86, note: "外資連續加碼，趨勢偏多", market: "上市" },
  { code: "2603", name: "長榮", signal: "risk", score: 48, note: "短線賣壓偏高，先控風險", market: "上市" },
  { code: "2881", name: "富邦金", signal: "watch", score: 68, note: "金融股穩定，等待量能放大", market: "上市" }
];

let datasets = { main: [...demoStocks], contrarian: [], brewing: [], v_reversal: [] };
let currentMode = "main";
let dataSource = "Demo";
let datasetMeta = {};
let chartState = null;
let chartRangeDays = 60;
let chartTooltipPinned = false;
let chartTouchStart = null;

const chartColors = {
  up: "#dc2626",
  down: "#047857",
  ma5: "#f97316",
  ma20: "#2563eb",
  neckline: "#8b5cf6",
  support: "#0f766e",
  target: "#b45309"
};

const signalLabel = { strong: "強勢", watch: "觀察", risk: "風險" };

const modeCopy = {
  main: {
    title: "主升段掃描結果",
    eyebrow: "Main Scan",
    sideTitle: "主升段邏輯",
    notes: ["法人買超是門檻。", "N 字突破與波段新高是進場核心。", "技術分、籌碼分、量能分一起排序。"]
  },
  contrarian: {
    title: "逆勢抗跌掃描結果",
    eyebrow: "Contrarian Scan",
    sideTitle: "逆勢抗跌邏輯",
    notes: ["依大盤燈號調整門檻。", "市場弱勢時提高個股篩選標準。", "優先找相對強、籌碼穩、止跌轉強標的。"]
  },
  brewing: {
    title: "右腳醞釀精選",
    eyebrow: "Right-Foot Brewing",
    sideTitle: "右腳醞釀邏輯",
    notes: [
      "N字突破：剛越過局部頸線，右腳啟動。",
      "未創波段新高：還沒噴出，避免追高。",
      "投信連買 ≥ 5 日：法人已先進駐。",
      "★帶寬收斂為加分項：能量壓縮，優先關注。"
    ]
  },
  v_reversal: {
    title: "V型反轉早期掃描",
    eyebrow: "V-Reversal Scan",
    sideTitle: "V型狀態定義",
    notes: [
      "V0：連跌超賣，等待第一根合格紅K。",
      "V1：強紅K收近最高、無長上影，先守紅K中值。",
      "V2：站上第一確認價；V3：已收復跌幅50%，不再視為最早買點。",
      "漲停鎖住可接受較低量比；長上影與跌破V底一律排除。"
    ]
  }
};

const elements = {
  runtimeStatus: document.getElementById("runtimeStatus"),
  runtimeDetail: document.getElementById("runtimeDetail"),
  stockSearch: document.getElementById("stockSearch"),
  signalFilter: document.getElementById("signalFilter"),
  runBtn: document.getElementById("runBtn"),
  loadBtn: document.getElementById("loadBtn"),
  totalCount: document.getElementById("totalCount"),
  strongCount: document.getElementById("strongCount"),
  watchCount: document.getElementById("watchCount"),
  dataSource: document.getElementById("dataSource"),
  lastUpdated: document.getElementById("lastUpdated"),
  stockRows: document.getElementById("stockRows"),
  mainModeBtn: document.getElementById("mainModeBtn"),
  contrarianModeBtn: document.getElementById("contrarianModeBtn"),
  brewingModeBtn: document.getElementById("brewingModeBtn"),
  vReversalModeBtn: document.getElementById("vReversalModeBtn"),
  resultEyebrow: document.getElementById("resultEyebrow"),
  resultTitle: document.getElementById("resultTitle"),
  sideTitle: document.getElementById("sideTitle"),
  sideNotes: document.getElementById("sideNotes"),
  chartModal: document.getElementById("chartModal"),
  chartTitle: document.getElementById("chartTitle"),
  chartSubtitle: document.getElementById("chartSubtitle"),
  chartCloseBtn: document.getElementById("chartCloseBtn"),
  chartStatus: document.getElementById("chartStatus"),
  chartCanvas: document.getElementById("chartCanvas"),
  chartTooltip: document.getElementById("chartTooltip"),
  chartStats: document.getElementById("chartStats"),
  chartRangeButtons: Array.from(document.querySelectorAll(".chart-range-button"))
};

function normalizeStock(item) {
  const note = String(item.note ?? item.reason ?? item.badges ?? "").trim();
  const badges = String(item.badges ?? "").trim();
  const combined = note + "；" + badges;
  const trustMatch = combined.match(/投信連買(\d+)日/);
  const numberOrNull = (value) => {
    const number = Number(value);
    return value !== "" && value !== null && value !== undefined && Number.isFinite(number) ? number : null;
  };
  return {
    code: String(item.code ?? item.stock_id ?? "").trim(),
    name: String(item.name ?? item.stock_name ?? "未命名").trim(),
    signal: ["strong", "watch", "risk"].includes(item.signal) ? item.signal : "watch",
    score: Number(item.score ?? item.chip_score ?? item.totalScore ?? 0),
    note: note,
    badges: badges,
    bbSignal: note.split("；")[0].trim(),
    trustStreak: trustMatch ? Number(trustMatch[1]) : 0,
    market: String(item.market ?? item.market_type ?? "").trim(),
    marketLight: String(item.market_light ?? item.marketLight ?? "").trim(),
    vState: String(item.v_state ?? item.vState ?? "").trim(),
    leftDropPct: numberOrNull(item.left_drop_pct ?? item.leftDropPct),
    rsi14: numberOrNull(item.rsi14),
    blackCount: numberOrNull(item.black_count ?? item.blackCount),
    leftPeak: numberOrNull(item.left_peak ?? item.leftPeak),
    vBottom: numberOrNull(item.v_bottom ?? item.vBottom),
    triggerMid: numberOrNull(item.trigger_mid ?? item.triggerMid),
    v2Confirm: numberOrNull(item.v2_confirm ?? item.v2Confirm),
    recover50: numberOrNull(item.recover_50 ?? item.recover50),
    recover618: numberOrNull(item.recover_618 ?? item.recover618),
    invalidPrice: numberOrNull(item.invalid_price ?? item.invalidPrice),
    triggerDate: String(item.trigger_date ?? item.triggerDate ?? "").trim()
  };
}

// ══════════ 右腳醞釀精選 ══════════
// 必要條件：N字突破（剛越過局部頸線，右腳啟動）
//        ＋ 未創波段新高（還沒噴出，避免追高）
//        ＋ 投信連買 >= 5 日（法人已進駐）
// 加分項：帶寬收斂（能量壓縮，蓄勢待發）→ 名單中以 ★ 標記並排在最前
// 注意：「未創波段新高」包含「波段新高」子字串，判斷前必須先移除，
//       否則所有醞釀股都會被誤判為已創新高而漏掉。
const BREWING_MIN_TRUST_STREAK = 5;

function isBrewingCandidate(stock) {
  const text = stock.note + "；" + stock.badges;
  const textClean = text.replaceAll("未創波段新高", "");
  const hasBreakout = text.includes("N字突破");
  const notNewHigh = !textClean.includes("波段新高");
  const trustBacked = stock.trustStreak >= BREWING_MIN_TRUST_STREAK;
  return hasBreakout && notNewHigh && trustBacked;
}

function hasSqueeze(stock) {
  return stock.bbSignal.includes("收斂") || (stock.note + stock.badges).includes("帶寬收斂");
}

function buildBrewingList(mainStocks) {
  return mainStocks
    .filter(isBrewingCandidate)
    .map((stock) => Object.assign({}, stock, {
      squeeze: hasSqueeze(stock),
      note: (hasSqueeze(stock) ? "★帶寬收斂｜" : "") + "投信連買" + stock.trustStreak + "日｜" + stock.note
    }))
    .sort((a, b) => (Number(b.squeeze) - Number(a.squeeze)) || (b.score - a.score) || (b.trustStreak - a.trustStreak));
}

function activeStocks() {
  return datasets[currentMode] || [];
}

function setStatus(title, detail) {
  elements.runtimeStatus.textContent = title;
  elements.runtimeDetail.textContent = detail;
}

function setMode(mode) {
  currentMode = mode;
  elements.mainModeBtn.classList.toggle("active", mode === "main");
  elements.contrarianModeBtn.classList.toggle("active", mode === "contrarian");
  if (elements.brewingModeBtn) {
    elements.brewingModeBtn.classList.toggle("active", mode === "brewing");
  }
  if (elements.vReversalModeBtn) {
    elements.vReversalModeBtn.classList.toggle("active", mode === "v_reversal");
  }
  render();
}

function getFilteredStocks() {
  const keyword = elements.stockSearch.value.trim().toLowerCase();
  const signal = elements.signalFilter.value;
  return activeStocks().filter((stock) => {
    const matchesKeyword = !keyword || stock.code.toLowerCase().includes(keyword) || stock.name.toLowerCase().includes(keyword);
    const matchesSignal = signal === "all" || stock.signal === signal;
    return matchesKeyword && matchesSignal;
  });
}

function renderModeText() {
  const copy = modeCopy[currentMode];
  const meta = datasetMeta[currentMode] || {};
  elements.resultEyebrow.textContent = copy.eyebrow;
  elements.resultTitle.textContent = copy.title;
  elements.sideTitle.textContent = copy.sideTitle;
  elements.sideNotes.innerHTML = copy.notes.map((item) => "<li>" + escapeHtml(item) + "</li>").join("");
  elements.dataSource.textContent = meta.sheet_tab || dataSource;
}

function render() {
  const filteredStocks = getFilteredStocks();
  const stocks = activeStocks();
  const strongCount = stocks.filter((stock) => stock.signal === "strong").length;
  const watchCount = stocks.filter((stock) => stock.signal === "watch").length;

  renderModeText();
  elements.totalCount.textContent = String(stocks.length);
  elements.strongCount.textContent = String(strongCount);
  elements.watchCount.textContent = String(watchCount);
  elements.lastUpdated.textContent = new Date().toLocaleString("zh-TW", { hour12: false });

  if (filteredStocks.length === 0) {
    elements.stockRows.innerHTML = '<tr><td colspan="5">沒有符合條件的股票。</td></tr>';
    return;
  }

  elements.stockRows.innerHTML = filteredStocks.map((stock) => {
    const note = stock.marketLight ? "[" + stock.marketLight + "] " + stock.note : stock.note;
    return '<tr class="stock-row" data-code="' + escapeHtml(stock.code) + '" tabindex="0" title="點擊查看 K 線圖">' +
      '<td><strong>' + escapeHtml(stock.code) + '</strong></td>' +
      '<td>' + escapeHtml(stock.name) + '</td>' +
      '<td><span class="badge ' + stock.signal + '">' + signalLabel[stock.signal] + '</span></td>' +
      '<td>' + (Number.isFinite(stock.score) ? stock.score : 0) + '</td>' +
      '<td>' + escapeHtml(note || "-") + '</td>' +
    '</tr>';
  }).join("");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function loadJsonData() {
  setStatus("讀取資料中", "正在嘗試載入 data.json");
  try {
    const response = await fetch("data.json", { cache: "no-store" });
    if (!response.ok) throw new Error("找不到 data.json");
    const payload = await response.json();
    const mainRows = Array.isArray(payload) ? payload : payload.stocks;
    const contrarianRows = Array.isArray(payload.contrarian_stocks) ? payload.contrarian_stocks : [];
    const vReversalRows = Array.isArray(payload.v_reversal_stocks) ? payload.v_reversal_stocks : [];
    if (!Array.isArray(mainRows)) throw new Error("data.json 格式需要包含 stocks 陣列");

    const mainList = mainRows.map(normalizeStock).filter((stock) => stock.code);
    datasets = {
      main: mainList,
      contrarian: contrarianRows.map(normalizeStock).filter((stock) => stock.code),
      brewing: buildBrewingList(mainList),
      v_reversal: vReversalRows.map(normalizeStock).filter((stock) => stock.code)
    };
    datasetMeta = payload.datasets || {
      main: { sheet_tab: payload.sheet_tab || "選股結果" },
      contrarian: { sheet_tab: payload.contrarian_sheet_tab || "逆勢抗跌" },
      v_reversal: { sheet_tab: payload.v_reversal_sheet_tab || "V型反轉掃描" }
    };
    if (!datasetMeta.brewing) {
      datasetMeta.brewing = { sheet_tab: "選股結果（前端精選）" };
    }
    if (!datasetMeta.v_reversal) {
      datasetMeta.v_reversal = { sheet_tab: payload.v_reversal_sheet_tab || "V型反轉掃描" };
    }
    dataSource = payload.source || "data.json";
    render();
    setStatus(
      "資料已更新",
      "主升段 " + datasets.main.length + " 筆；逆勢抗跌 " + datasets.contrarian.length +
      " 筆；右腳醞釀 " + datasets.brewing.length + " 筆；V型反轉 " + datasets.v_reversal.length + " 筆"
    );
  } catch (error) {
    dataSource = "Demo";
    datasets = { main: [...demoStocks], contrarian: [], brewing: [], v_reversal: [] };
    render();
    setStatus("使用示範資料", error.message);
  }
}

function runFrontendProgram() {
  const filteredCount = getFilteredStocks().length;
  render();
  setStatus("篩選已套用", "目前顯示 " + filteredCount + " 筆符合條件的股票");
}

function findStockByCode(code) {
  return activeStocks().find((stock) => stock.code === code);
}

async function openChart(stock) {
  chartRangeDays = 60;
  setRangeButtons();
  elements.chartModal.classList.add("open");
  elements.chartModal.setAttribute("aria-hidden", "false");
  await loadChart(stock, chartRangeDays);
}

async function loadChart(stock, days) {
  elements.chartTitle.textContent = stock.name + "（" + stock.code + "）";
  const isVReversal = Boolean(stock.vState);
  elements.chartSubtitle.textContent = isVReversal
    ? "近 " + days + " 日 K 線 · MA5 · MA20 · V底 · 紅K中值 · 確認價"
    : "近 " + days + " 日 K 線 · MA5 · MA20 · 頸線 · N 理論價格區間";
  elements.chartStatus.textContent = "讀取 " + days + " 日 K 線資料中...";
  elements.chartStatus.hidden = false;
  elements.chartStats.innerHTML = "";
  elements.chartTooltip.hidden = true;
  chartTooltipPinned = false;
  chartTouchStart = null;
  chartState = null;
  clearChart();

  try {
    const params = new URLSearchParams({ code: stock.code, market: stock.market || "", days: String(days) });
    const response = await fetch("/api/kline?" + params.toString(), { cache: "no-store" });
    if (!response.ok) {
      const errorPayload = await response.json().catch(() => ({}));
      throw new Error(errorPayload.error || "K 線資料讀取失敗");
    }

    const payload = await response.json();
    const candles = payload.candles.map((item) => ({
      date: item.date,
      open: Number(item.open),
      high: Number(item.high),
      low: Number(item.low),
      close: Number(item.close),
      volume: Number(item.volume || 0)
    })).filter((item) => Number.isFinite(item.close));

    if (candles.length < 20) throw new Error("K 線資料不足，暫時無法畫出區間");
    const levels = isVReversal ? buildVReversalLevels(stock, candles) : calculateNTheoryLevels(candles);
    chartState = { stock, candles, levels, hoverIndex: null, symbol: payload.symbol, days, mode: isVReversal ? "v_reversal" : "n" };
    elements.chartStatus.hidden = true;
    renderChartStats(stock, candles, levels, payload.symbol, days);
    drawChart();
    setStatus("K 線已載入", stock.code + " 近 " + candles.length + " 日資料");
  } catch (error) {
    elements.chartStatus.hidden = false;
    elements.chartStatus.textContent = error.message;
    setStatus("K 線讀取失敗", error.message);
  }
}

function closeChart() {
  elements.chartModal.classList.remove("open");
  elements.chartModal.setAttribute("aria-hidden", "true");
  elements.chartTooltip.hidden = true;
  chartTooltipPinned = false;
  chartTouchStart = null;
}

function clearChart() {
  const ctx = elements.chartCanvas.getContext("2d");
  ctx.clearRect(0, 0, elements.chartCanvas.width, elements.chartCanvas.height);
}

function calculateMovingAverage(candles, windowSize) {
  return candles.map((_, index) => {
    if (index + 1 < windowSize) return null;
    const slice = candles.slice(index + 1 - windowSize, index + 1);
    return slice.reduce((total, candle) => total + candle.close, 0) / windowSize;
  });
}

function calculateNTheoryLevels(candles) {
  const usable = candles;
  const firstLowLimit = Math.max(10, Math.floor(usable.length * 0.45));
  const pointAIndex = findExtremeIndex(usable.slice(0, firstLowLimit), "low");
  const afterA = usable.slice(pointAIndex + 1);
  const pointBRelative = findExtremeIndex(afterA, "high");
  const pointBIndex = pointAIndex + 1 + pointBRelative;
  const afterB = usable.slice(pointBIndex + 1);
  const pointCRelative = afterB.length ? findExtremeIndex(afterB, "low") : 0;
  const pointCIndex = afterB.length ? pointBIndex + 1 + pointCRelative : Math.max(pointBIndex - 1, pointAIndex);
  const pointA = usable[pointAIndex];
  const pointB = usable[pointBIndex];
  const pointC = usable[pointCIndex];
  const neckline = pointB.high;
  const support = pointC.low;
  const waveHeight = Math.max(0, pointB.high - pointA.low);
  const target = pointC.low + waveHeight;
  return {
    pointAIndex,
    pointBIndex,
    pointCIndex,
    support,
    neckline,
    target,
    entryLow: neckline * 0.985,
    entryHigh: neckline * 1.015
  };
}

function buildVReversalLevels(stock, candles) {
  const recent = candles.slice(-12);
  const fallbackBottom = Math.min(...recent.map((candle) => candle.low));
  const fallbackPeak = Math.max(...recent.map((candle) => candle.high));
  const support = Number.isFinite(stock.vBottom) ? stock.vBottom : fallbackBottom;
  const neckline = Number.isFinite(stock.v2Confirm) ? stock.v2Confirm : fallbackPeak;
  const target = Number.isFinite(stock.recover50) ? stock.recover50 : support + (fallbackPeak - support) * 0.5;
  const hasTriggerMid = Number.isFinite(stock.triggerMid);
  return {
    type: "v_reversal",
    support,
    neckline,
    target,
    entryLow: hasTriggerMid ? stock.triggerMid : support,
    entryHigh: neckline,
    recover618: Number.isFinite(stock.recover618) ? stock.recover618 : support + (fallbackPeak - support) * 0.618,
    hasTriggerMid
  };
}

function findExtremeIndex(rows, field) {
  let bestIndex = 0;
  let bestValue = rows[0] ? rows[0][field] : 0;
  for (let index = 1; index < rows.length; index += 1) {
    const value = rows[index][field];
    if ((field === "low" && value < bestValue) || (field === "high" && value > bestValue)) {
      bestValue = value;
      bestIndex = index;
    }
  }
  return bestIndex;
}

function renderChartStats(stock, candles, levels, symbol, days) {
  const latest = candles[candles.length - 1];
  const latestDate = formatDate(latest.date);
  if (levels.type === "v_reversal") {
    const midpointText = levels.hasTriggerMid ? formatPrice(levels.entryLow) : "尚未出現";
    elements.chartStats.innerHTML = [
      '<span><strong>狀態 / 期間</strong>' + escapeHtml(stock.vState || "V型") + ' · ' + escapeHtml(String(days)) + ' 日</span>',
      '<span><strong>最新收盤</strong>' + escapeHtml(formatPrice(latest.close)) + '（' + escapeHtml(latestDate) + '）</span>',
      '<span><strong>防守</strong>V底 ' + escapeHtml(formatPrice(levels.support)) + ' · 紅K中值 ' + escapeHtml(midpointText) + '</span>',
      '<span><strong>確認 / 收復</strong>' + escapeHtml(formatPrice(levels.neckline)) + ' · 50% ' + escapeHtml(formatPrice(levels.target)) + ' · 61.8% ' + escapeHtml(formatPrice(levels.recover618)) + '</span>'
    ].join("");
    return;
  }
  const rangeText = "防守區 " + formatPrice(levels.support) + " - " + formatPrice(levels.neckline) + "；突破區 " + formatPrice(levels.entryLow) + " - " + formatPrice(levels.entryHigh) + "；N 目標 " + formatPrice(levels.target);
  elements.chartStats.innerHTML = [
    '<span><strong>期間</strong>' + escapeHtml(String(days)) + ' 日 · ' + escapeHtml(symbol || "") + '</span>',
    '<span><strong>最新收盤</strong>' + escapeHtml(formatPrice(latest.close)) + '（' + escapeHtml(latestDate) + '）</span>',
    '<span><strong>頸線</strong>' + escapeHtml(formatPrice(levels.neckline)) + '</span>',
    '<span><strong>N 理論區間</strong>' + escapeHtml(rangeText) + '</span>'
  ].join("");
}

function drawChart() {
  if (!chartState) return;
  const canvas = elements.chartCanvas;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.floor(rect.width * dpr);
  canvas.height = Math.floor(rect.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const width = rect.width;
  const height = rect.height;
  const padding = { top: 28, right: 72, bottom: 44, left: 52 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;
  const candles = chartState.candles;
  const ma5 = calculateMovingAverage(candles, 5);
  const ma20 = calculateMovingAverage(candles, 20);
  const levels = chartState.levels;
  const valueList = candles.flatMap((candle) => [candle.high, candle.low]).concat([levels.support, levels.neckline, levels.target, levels.entryLow, levels.entryHigh]);
  const minValue = Math.min(...valueList);
  const maxValue = Math.max(...valueList);
  const padValue = Math.max((maxValue - minValue) * 0.12, maxValue * 0.01);
  const yMin = minValue - padValue;
  const yMax = maxValue + padValue;
  const xFor = (index) => padding.left + (plotW / Math.max(candles.length - 1, 1)) * index;
  const yFor = (value) => padding.top + ((yMax - value) / (yMax - yMin)) * plotH;
  const candleW = Math.max(3, Math.min(14, plotW / candles.length * 0.55));

  ctx.clearRect(0, 0, width, height);
  drawGrid(ctx, padding, width, plotW, plotH, yMin, yMax, yFor);
  drawZone(ctx, padding, plotW, yFor(levels.target), yFor(levels.neckline), "rgba(180, 83, 9, 0.10)");
  drawZone(ctx, padding, plotW, yFor(levels.neckline), yFor(levels.support), "rgba(15, 118, 110, 0.10)");
  if (levels.type === "v_reversal") {
    drawLevel(ctx, padding, plotW, yFor(levels.neckline), chartColors.neckline, "V2確認 " + formatPrice(levels.neckline));
    if (levels.hasTriggerMid) {
      drawLevel(ctx, padding, plotW, yFor(levels.entryLow), chartColors.ma5, "紅K中值 " + formatPrice(levels.entryLow));
    }
    drawLevel(ctx, padding, plotW, yFor(levels.support), chartColors.support, "V底 " + formatPrice(levels.support));
    drawLevel(ctx, padding, plotW, yFor(levels.target), chartColors.target, "50%收復 " + formatPrice(levels.target));
  } else {
    drawLevel(ctx, padding, plotW, yFor(levels.neckline), chartColors.neckline, "頸線 " + formatPrice(levels.neckline));
    drawLevel(ctx, padding, plotW, yFor(levels.support), chartColors.support, "支撐 " + formatPrice(levels.support));
    drawLevel(ctx, padding, plotW, yFor(levels.target), chartColors.target, "N目標 " + formatPrice(levels.target));
  }

  candles.forEach((candle, index) => {
    const x = xFor(index);
    const openY = yFor(candle.open);
    const closeY = yFor(candle.close);
    const highY = yFor(candle.high);
    const lowY = yFor(candle.low);
    const isUp = candle.close >= candle.open;
    ctx.strokeStyle = isUp ? chartColors.up : chartColors.down;
    ctx.fillStyle = isUp ? chartColors.up : chartColors.down;
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.moveTo(x, highY);
    ctx.lineTo(x, lowY);
    ctx.stroke();
    ctx.fillRect(x - candleW / 2, Math.min(openY, closeY), candleW, Math.max(Math.abs(openY - closeY), 2));
  });

  drawLine(ctx, ma5, xFor, yFor, chartColors.ma5, 2);
  drawLine(ctx, ma20, xFor, yFor, chartColors.ma20, 2);
  if (levels.type !== "v_reversal") {
    drawSwingPoint(ctx, xFor(levels.pointAIndex), yFor(candles[levels.pointAIndex]?.low), "A");
    drawSwingPoint(ctx, xFor(levels.pointBIndex), yFor(candles[levels.pointBIndex]?.high), "B");
    drawSwingPoint(ctx, xFor(levels.pointCIndex), yFor(candles[levels.pointCIndex]?.low), "C");
  }
  drawXAxis(ctx, candles, xFor, padding, height);
  drawLegend(ctx, padding, height, levels.type);
  if (chartState.hoverIndex !== null) drawHoverGuide(ctx, candles, chartState.hoverIndex, xFor, yFor, padding, plotH);
}

function drawGrid(ctx, padding, width, plotW, plotH, yMin, yMax, yFor) {
  ctx.strokeStyle = "#e5e7eb";
  ctx.fillStyle = "#667085";
  ctx.font = "12px Arial";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const value = yMin + ((yMax - yMin) / 4) * i;
    const y = yFor(value);
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.stroke();
    ctx.fillText(formatPrice(value), width - padding.right + 10, y + 4);
  }
  ctx.strokeStyle = "#cfd8e3";
  ctx.strokeRect(padding.left, padding.top, plotW, plotH);
}

function drawZone(ctx, padding, plotW, y1, y2, fillStyle) {
  ctx.fillStyle = fillStyle;
  ctx.fillRect(padding.left, Math.min(y1, y2), plotW, Math.abs(y2 - y1));
}

function drawLevel(ctx, padding, plotW, y, color, label) {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.setLineDash([6, 5]);
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  ctx.moveTo(padding.left, y);
  ctx.lineTo(padding.left + plotW, y);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.font = "12px Arial";
  ctx.fillText(label, padding.left + plotW - 108, y - 6);
  ctx.restore();
}

function drawLine(ctx, values, xFor, yFor, color, lineWidth) {
  const firstValidIndex = values.findIndex((item) => item !== null);
  ctx.strokeStyle = color;
  ctx.lineWidth = lineWidth;
  ctx.beginPath();
  values.forEach((value, index) => {
    if (value === null) return;
    const x = xFor(index);
    const y = yFor(value);
    if (index === firstValidIndex) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawSwingPoint(ctx, x, y, label) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) return;
  ctx.fillStyle = "#111827";
  ctx.beginPath();
  ctx.arc(x, y, 4, 0, Math.PI * 2);
  ctx.fill();
  ctx.font = "700 12px Arial";
  ctx.fillText(label, x + 7, y - 7);
}

function drawXAxis(ctx, candles, xFor, padding, height) {
  ctx.fillStyle = "#667085";
  ctx.font = "12px Arial";
  [0, Math.floor(candles.length / 2), candles.length - 1].forEach((index) => {
    ctx.fillText(formatDate(candles[index].date).slice(5), xFor(index) - 18, height - padding.bottom + 26);
  });
}

function drawLegend(ctx, padding, height, levelType) {
  const items = [
    ["漲K", chartColors.up],
    ["跌K", chartColors.down],
    ["MA5", chartColors.ma5],
    ["MA20", chartColors.ma20],
    [levelType === "v_reversal" ? "V確認" : "頸線", chartColors.neckline]
  ];
  let x = padding.left;
  const y = height - 12;
  ctx.font = "12px Arial";
  items.forEach(([label, color]) => {
    ctx.fillStyle = color;
    ctx.fillRect(x, y - 8, 18, 4);
    ctx.fillStyle = "#374151";
    ctx.fillText(label, x + 24, y - 4);
    x += 76;
  });
}

function drawHoverGuide(ctx, candles, index, xFor, yFor, padding, plotH) {
  const candle = candles[index];
  const x = xFor(index);
  ctx.strokeStyle = "rgba(17, 24, 39, 0.35)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(x, padding.top);
  ctx.lineTo(x, padding.top + plotH);
  ctx.stroke();
  ctx.fillStyle = "#111827";
  ctx.beginPath();
  ctx.arc(x, yFor(candle.close), 4, 0, Math.PI * 2);
  ctx.fill();
}

function showChartTooltip(event) {
  if (!chartState) return;
  const canvas = elements.chartCanvas;
  const rect = canvas.getBoundingClientRect();
  const candles = chartState.candles;
  const padding = { left: 52, right: 72 };
  const plotW = rect.width - padding.left - padding.right;
  const x = event.clientX - rect.left;
  const rawIndex = Math.round(((x - padding.left) / plotW) * (candles.length - 1));
  const index = Math.max(0, Math.min(candles.length - 1, rawIndex));
  const candle = candles[index];
  chartState.hoverIndex = index;
  drawChart();
  elements.chartTooltip.hidden = false;
  elements.chartTooltip.style.left = Math.min(rect.width - 180, Math.max(12, event.clientX - rect.left + 14)) + "px";
  elements.chartTooltip.style.top = Math.max(12, event.clientY - rect.top - 16) + "px";
  elements.chartTooltip.innerHTML = "<strong>" + escapeHtml(formatDate(candle.date)) + "</strong><span>收盤 " + escapeHtml(formatPrice(candle.close)) + "</span><span>開 " + escapeHtml(formatPrice(candle.open)) + " 高 " + escapeHtml(formatPrice(candle.high)) + " 低 " + escapeHtml(formatPrice(candle.low)) + "</span>";
}

function hideChartTooltip() {
  if (!chartState || chartTooltipPinned) return;
  clearChartTooltip();
}

function clearChartTooltip() {
  if (!chartState) return;
  chartTooltipPinned = false;
  chartState.hoverIndex = null;
  elements.chartTooltip.hidden = true;
  drawChart();
}

function handleChartPointerMove(event) {
  if (event.pointerType && event.pointerType !== "mouse") return;
  chartTooltipPinned = false;
  showChartTooltip(event);
}

function handleChartPointerDown(event) {
  if (event.pointerType === "mouse") return;
  chartTouchStart = {
    pointerId: event.pointerId,
    clientX: event.clientX,
    clientY: event.clientY
  };
}

function handleChartPointerUp(event) {
  if (!chartTouchStart || chartTouchStart.pointerId !== event.pointerId) return;
  const movedX = event.clientX - chartTouchStart.clientX;
  const movedY = event.clientY - chartTouchStart.clientY;
  chartTouchStart = null;
  if (Math.hypot(movedX, movedY) > 10) return;
  chartTooltipPinned = true;
  showChartTooltip(event);
}

function handleChartPointerCancel() {
  chartTouchStart = null;
}

function setRangeButtons() {
  elements.chartRangeButtons.forEach((button) => {
    button.classList.toggle("active", Number(button.dataset.days) === chartRangeDays);
  });
}

function formatPrice(value) {
  return Number(value).toLocaleString("zh-TW", { maximumFractionDigits: 2 });
}

function formatDate(value) {
  const date = new Date(value + "T00:00:00");
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString("zh-TW", { year: "numeric", month: "2-digit", day: "2-digit" });
}

elements.mainModeBtn.addEventListener("click", () => setMode("main"));
elements.contrarianModeBtn.addEventListener("click", () => setMode("contrarian"));
if (elements.brewingModeBtn) {
  elements.brewingModeBtn.addEventListener("click", () => setMode("brewing"));
}
if (elements.vReversalModeBtn) {
  elements.vReversalModeBtn.addEventListener("click", () => setMode("v_reversal"));
}
elements.runBtn.addEventListener("click", runFrontendProgram);
elements.loadBtn.addEventListener("click", loadJsonData);
elements.stockSearch.addEventListener("input", render);
elements.signalFilter.addEventListener("change", render);
elements.stockRows.addEventListener("click", (event) => {
  const row = event.target.closest(".stock-row");
  if (!row) return;
  const stock = findStockByCode(row.dataset.code);
  if (stock) openChart(stock);
});
elements.stockRows.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const row = event.target.closest(".stock-row");
  if (!row) return;
  event.preventDefault();
  const stock = findStockByCode(row.dataset.code);
  if (stock) openChart(stock);
});
elements.chartRangeButtons.forEach((button) => {
  button.addEventListener("click", async () => {
    if (!chartState) return;
    chartRangeDays = Number(button.dataset.days);
    setRangeButtons();
    await loadChart(chartState.stock, chartRangeDays);
  });
});
elements.chartCloseBtn.addEventListener("click", closeChart);
elements.chartModal.addEventListener("click", (event) => {
  if (event.target === elements.chartModal) closeChart();
});
elements.chartModal.addEventListener("pointerdown", (event) => {
  if (event.target !== elements.chartCanvas) clearChartTooltip();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && elements.chartModal.classList.contains("open")) closeChart();
});
elements.chartCanvas.addEventListener("pointermove", handleChartPointerMove);
elements.chartCanvas.addEventListener("pointerdown", handleChartPointerDown);
elements.chartCanvas.addEventListener("pointerup", handleChartPointerUp);
elements.chartCanvas.addEventListener("pointercancel", handleChartPointerCancel);
elements.chartCanvas.addEventListener("pointerleave", hideChartTooltip);
window.addEventListener("resize", () => {
  if (chartState && elements.chartModal.classList.contains("open")) drawChart();
});

render();
loadJsonData();
