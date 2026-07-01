const demoStocks = [
  { code: "2330", name: "台積電", signal: "strong", score: 92, note: "法人買超，量價同步轉強" },
  { code: "2317", name: "鴻海", signal: "watch", score: 73, note: "籌碼回溫，等待突破確認" },
  { code: "2454", name: "聯發科", signal: "strong", score: 86, note: "外資連續加碼，趨勢偏多" },
  { code: "2603", name: "長榮", signal: "risk", score: 48, note: "短線賣壓偏高，先控風險" },
  { code: "2881", name: "富邦金", signal: "watch", score: 68, note: "金融股穩定，等待量能放大" }
];

let datasets = {
  main: [...demoStocks],
  contrarian: []
};
let currentMode = "main";
let dataSource = "Demo";
let datasetMeta = {};

const signalLabel = {
  strong: "強勢",
  watch: "觀察",
  risk: "風險"
};

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
  resultEyebrow: document.getElementById("resultEyebrow"),
  resultTitle: document.getElementById("resultTitle"),
  sideTitle: document.getElementById("sideTitle"),
  sideNotes: document.getElementById("sideNotes")
};

function normalizeStock(item) {
  return {
    code: String(item.code ?? item.stock_id ?? "").trim(),
    name: String(item.name ?? item.stock_name ?? "未命名").trim(),
    signal: ["strong", "watch", "risk"].includes(item.signal) ? item.signal : "watch",
    score: Number(item.score ?? item.chip_score ?? item.totalScore ?? 0),
    note: String(item.note ?? item.reason ?? item.badges ?? "").trim(),
    marketLight: String(item.market_light ?? item.marketLight ?? "").trim()
  };
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
    return '<tr>' +
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
    if (!response.ok) {
      throw new Error("找不到 data.json");
    }

    const payload = await response.json();
    const mainRows = Array.isArray(payload) ? payload : payload.stocks;
    const contrarianRows = Array.isArray(payload.contrarian_stocks) ? payload.contrarian_stocks : [];
    if (!Array.isArray(mainRows)) {
      throw new Error("data.json 格式需要包含 stocks 陣列");
    }

    datasets = {
      main: mainRows.map(normalizeStock).filter((stock) => stock.code),
      contrarian: contrarianRows.map(normalizeStock).filter((stock) => stock.code)
    };
    datasetMeta = payload.datasets || {
      main: { sheet_tab: payload.sheet_tab || "選股結果" },
      contrarian: { sheet_tab: payload.contrarian_sheet_tab || "逆勢抗跌" }
    };
    dataSource = payload.source || "data.json";
    render();
    setStatus("資料已更新", "主升段 " + datasets.main.length + " 筆；逆勢抗跌 " + datasets.contrarian.length + " 筆");
  } catch (error) {
    dataSource = "Demo";
    datasets = { main: [...demoStocks], contrarian: [] };
    render();
    setStatus("使用示範資料", error.message);
  }
}

function runFrontendProgram() {
  const filteredCount = getFilteredStocks().length;
  render();
  setStatus("篩選已套用", "目前顯示 " + filteredCount + " 筆符合條件的股票");
}

elements.mainModeBtn.addEventListener("click", () => setMode("main"));
elements.contrarianModeBtn.addEventListener("click", () => setMode("contrarian"));
elements.runBtn.addEventListener("click", runFrontendProgram);
elements.loadBtn.addEventListener("click", loadJsonData);
elements.stockSearch.addEventListener("input", render);
elements.signalFilter.addEventListener("change", render);

render();
loadJsonData();
