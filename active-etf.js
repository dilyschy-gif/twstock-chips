let activeEtfPayload = null;

const activeEtfElements = {
  runtimeStatus: document.getElementById("etfRuntimeStatus"),
  runtimeDetail: document.getElementById("etfRuntimeDetail"),
  batchFilter: document.getElementById("etfBatchFilter"),
  filter: document.getElementById("etfFilter"),
  search: document.getElementById("etfSearch"),
  reloadBtn: document.getElementById("reloadEtfBtn"),
  trackedCount: document.getElementById("trackedEtfCount"),
  totalAdded: document.getElementById("totalAdded"),
  totalRemoved: document.getElementById("totalRemoved"),
  generatedAt: document.getElementById("etfGeneratedAt"),
  cards: document.getElementById("etfCards"),
  sourceNote: document.getElementById("etfSourceNote")
};

function etfEscapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatEtfDate(value) {
  const text = String(value || "");
  if (!/^\d{8}$/.test(text)) return text || "--";
  return text.slice(0, 4) + "/" + text.slice(4, 6) + "/" + text.slice(6, 8);
}

function formatGeneratedAt(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "--";
  return date.toLocaleString("zh-TW", {
    timeZone: "Asia/Taipei",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  });
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString("zh-TW", { maximumFractionDigits: 3 });
}

function positionText(item) {
  const shares = formatNumber(item.shares) + " 股";
  if (item.market_code === "TW" && item.lots !== null && item.lots !== undefined) {
    return shares + "／" + formatNumber(item.lots) + " 張";
  }
  return shares;
}

function matchesSearch(item, keyword) {
  if (!keyword) return true;
  const text = [item.symbol, item.name, item.market].join(" ").toLowerCase();
  return text.includes(keyword);
}

function renderChangeTable(items, type, keyword) {
  const filtered = items.filter((item) => matchesSearch(item, keyword));
  const label = type === "added" ? "新增" : "剔除";
  const emptyText = keyword ? "沒有符合搜尋條件的股票" : "本次無" + label + "成分股";

  if (filtered.length === 0) {
    return '<div class="etf-empty">' + etfEscapeHtml(emptyText) + "</div>";
  }

  return '<div class="table-wrap etf-table-wrap"><table class="etf-change-table">' +
    "<thead><tr><th>市場／代號</th><th>股票名稱</th><th>" + label + "股數／張數</th></tr></thead>" +
    "<tbody>" +
    filtered.map((item) =>
      "<tr>" +
        '<td><span class="market-chip">' + etfEscapeHtml(item.market) + "</span><strong>" + etfEscapeHtml(item.symbol) + "</strong></td>" +
        "<td>" + etfEscapeHtml(item.name) + "</td>" +
        '<td class="position-value">' + etfEscapeHtml(positionText(item)) + "</td>" +
      "</tr>"
    ).join("") +
    "</tbody></table></div>";
}

function renderEtfCard(etf, keyword) {
  const hasChanges = etf.added.length + etf.removed.length > 0;
  return '<article class="panel etf-card">' +
    '<div class="etf-card-header">' +
      "<div>" +
        '<p class="eyebrow">' + etfEscapeHtml(etf.code) + "</p>" +
        "<h2>" + etfEscapeHtml(etf.name) + "</h2>" +
      "</div>" +
      '<span class="change-state ' + (hasChanges ? "changed" : "unchanged") + '">' +
        (hasChanges ? "名單有變動" : "名單無變動") +
      "</span>" +
    "</div>" +
    '<div class="etf-date-row">' +
      "<span>比較期間：" + etfEscapeHtml(formatEtfDate(etf.previous_date)) + " → " + etfEscapeHtml(formatEtfDate(etf.data_date)) + "</span>" +
      "<span>目前持股 " + etfEscapeHtml(etf.current_holdings_count) + " 檔</span>" +
      '<a href="' + etfEscapeHtml(etf.official_url) + '" target="_blank" rel="noopener noreferrer">官方持股核對</a>' +
    "</div>" +
    '<div class="etf-change-grid">' +
      '<section class="etf-change-block added-block">' +
        '<div class="etf-change-title"><h3>新增成分股</h3><strong>' + etf.added.length + "</strong></div>" +
        renderChangeTable(etf.added, "added", keyword) +
      "</section>" +
      '<section class="etf-change-block removed-block">' +
        '<div class="etf-change-title"><h3>剔除成分股</h3><strong>' + etf.removed.length + "</strong></div>" +
        renderChangeTable(etf.removed, "removed", keyword) +
      "</section>" +
    "</div>" +
  "</article>";
}

function selectedBatch() {
  const history = Array.isArray(activeEtfPayload.history) ? activeEtfPayload.history : [];
  const selectedDate = activeEtfElements.batchFilter.value;
  if (selectedDate) {
    const selected = history.find((item) => item.batch_date === selectedDate);
    if (selected) return selected;
  }
  if (history.length) return history[history.length - 1];
  return {
    batch_date: "",
    generated_at: activeEtfPayload.generated_at,
    etfs: activeEtfPayload.etfs
  };
}

function renderActiveEtfs() {
  if (!activeEtfPayload || !Array.isArray(activeEtfPayload.etfs)) return;
  const batch = selectedBatch();
  const selected = activeEtfElements.filter.value;
  const keyword = activeEtfElements.search.value.trim().toLowerCase();
  const allEtfs = Array.isArray(batch.etfs) ? batch.etfs : [];
  const etfs = allEtfs.filter((etf) => selected === "all" || etf.code === selected);

  activeEtfElements.trackedCount.textContent = String(allEtfs.length);
  activeEtfElements.totalAdded.textContent = String(allEtfs.reduce((sum, etf) => sum + etf.added.length, 0));
  activeEtfElements.totalRemoved.textContent = String(allEtfs.reduce((sum, etf) => sum + etf.removed.length, 0));
  activeEtfElements.generatedAt.textContent = formatGeneratedAt(batch.generated_at);
  activeEtfElements.cards.innerHTML = etfs.map((etf) => renderEtfCard(etf, keyword)).join("");
}

function populateBatchFilter() {
  const history = Array.isArray(activeEtfPayload.history) ? activeEtfPayload.history : [];
  activeEtfElements.batchFilter.innerHTML = history
    .slice()
    .reverse()
    .map((item, index) =>
      '<option value="' + etfEscapeHtml(item.batch_date) + '">' +
      etfEscapeHtml(formatEtfDate(item.batch_date)) +
      (index === 0 ? "（最新）" : "") +
      "</option>"
    )
    .join("");
}

async function loadActiveEtfs() {
  activeEtfElements.runtimeStatus.textContent = "讀取資料中";
  activeEtfElements.runtimeDetail.textContent = "正在載入最近兩個公告交易日";
  try {
    const response = await fetch("active-etf.json", { cache: "no-store" });
    if (!response.ok) throw new Error("找不到 active-etf.json");
    const payload = await response.json();
    if (!Array.isArray(payload.etfs) || payload.etfs.length !== 5) {
      throw new Error("ETF資料不完整");
    }
    activeEtfPayload = payload;
    populateBatchFilter();
    renderActiveEtfs();
    const changedCount = payload.etfs.filter((etf) => etf.added.length || etf.removed.length).length;
    activeEtfElements.runtimeStatus.textContent = "資料已更新";
    activeEtfElements.runtimeDetail.textContent = changedCount
      ? changedCount + " 檔ETF的成分名單有變動"
      : "5檔ETF本次均無新增或剔除";
    activeEtfElements.sourceNote.innerHTML =
      "資料來源：" +
      '<a href="' + etfEscapeHtml(payload.source.url) + '" target="_blank" rel="noopener noreferrer">' +
      etfEscapeHtml(payload.source.name) +
      "</a>。" +
      etfEscapeHtml(payload.source.note);
  } catch (error) {
    activeEtfElements.runtimeStatus.textContent = "資料讀取失敗";
    activeEtfElements.runtimeDetail.textContent = error.message;
    activeEtfElements.cards.innerHTML =
      '<article class="panel etf-load-error">暫時無法取得ETF持股變動，請稍後重新讀取。</article>';
  }
}

activeEtfElements.batchFilter.addEventListener("change", renderActiveEtfs);
activeEtfElements.filter.addEventListener("change", renderActiveEtfs);
activeEtfElements.search.addEventListener("input", renderActiveEtfs);
activeEtfElements.reloadBtn.addEventListener("click", loadActiveEtfs);

loadActiveEtfs();
