const demoStocks = [
  { code: "2330", name: "台積電", signal: "strong", score: 92, note: "法人買超，量價同步轉強" },
  { code: "2317", name: "鴻海", signal: "watch", score: 73, note: "籌碼回溫，等待突破確認" },
  { code: "2454", name: "聯發科", signal: "strong", score: 86, note: "外資連續加碼，趨勢偏多" },
  { code: "2603", name: "長榮", signal: "risk", score: 48, note: "短線賣壓偏高，先控風險" },
  { code: "2881", name: "富邦金", signal: "watch", score: 68, note: "金融股穩定，等待量能放大" }
];

let stocks = [...demoStocks];
let dataSource = "Demo";

const signalLabel = {
  strong: "強勢",
  watch: "觀察",
  risk: "風險"
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
  stockRows: document.getElementById("stockRows")
};

function normalizeStock(item) {
  return {
    code: String(item.code ?? item.stock_id ?? "").trim(),
    name: String(item.name ?? item.stock_name ?? "未命名").trim(),
    signal: ["strong", "watch", "risk"].includes(item.signal) ? item.signal : "watch",
    score: Number(item.score ?? item.chip_score ?? 0),
    note: String(item.note ?? item.reason ?? "").trim()
  };
}

function setStatus(title, detail) {
  elements.runtimeStatus.textContent = title;
  elements.runtimeDetail.textContent = detail;
}

function getFilteredStocks() {
  const keyword = elements.stockSearch.value.trim().toLowerCase();
  const signal = elements.signalFilter.value;

  return stocks.filter((stock) => {
    const matchesKeyword = !keyword || stock.code.toLowerCase().includes(keyword) || stock.name.toLowerCase().includes(keyword);
    const matchesSignal = signal === "all" || stock.signal === signal;
    return matchesKeyword && matchesSignal;
  });
}

function render() {
  const filteredStocks = getFilteredStocks();
  const strongCount = stocks.filter((stock) => stock.signal === "strong").length;
  const watchCount = stocks.filter((stock) => stock.signal === "watch").length;

  elements.totalCount.textContent = String(stocks.length);
  elements.strongCount.textContent = String(strongCount);
  elements.watchCount.textContent = String(watchCount);
  elements.dataSource.textContent = dataSource;
  elements.lastUpdated.textContent = new Date().toLocaleString("zh-TW", { hour12: false });

  if (filteredStocks.length === 0) {
    elements.stockRows.innerHTML = '<tr><td colspan="5">沒有符合條件的股票。</td></tr>';
    return;
  }

  elements.stockRows.innerHTML = filteredStocks.map((stock) => {
    return '<tr>' +
      '<td><strong>' + escapeHtml(stock.code) + '</strong></td>' +
      '<td>' + escapeHtml(stock.name) + '</td>' +
      '<td><span class="badge ' + stock.signal + '">' + signalLabel[stock.signal] + '</span></td>' +
      '<td>' + (Number.isFinite(stock.score) ? stock.score : 0) + '</td>' +
      '<td>' + escapeHtml(stock.note || "-") + '</td>' +
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
    const rows = Array.isArray(payload) ? payload : payload.stocks;
    if (!Array.isArray(rows)) {
      throw new Error("data.json 格式需要是陣列，或包含 stocks 陣列");
    }

    stocks = rows.map(normalizeStock).filter((stock) => stock.code);
    dataSource = "data.json";
    render();
    setStatus("資料已更新", "已載入 " + stocks.length + " 筆股票資料");
  } catch (error) {
    dataSource = "Demo";
    stocks = [...demoStocks];
    render();
    setStatus("使用示範資料", error.message);
  }
}

function runFrontendProgram() {
  const filteredCount = getFilteredStocks().length;
  render();
  setStatus("前端程式已執行", "目前顯示 " + filteredCount + " 筆符合條件的股票");
}

elements.runBtn.addEventListener("click", runFrontendProgram);
elements.loadBtn.addEventListener("click", loadJsonData);
elements.stockSearch.addEventListener("input", render);
elements.signalFilter.addEventListener("change", render);

render();
setStatus("前端已就緒", "JavaScript 正在瀏覽器中執行");
