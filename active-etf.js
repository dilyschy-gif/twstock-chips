let activeEtfPayload = null;

const activeEtfElements = {
  runtimeStatus: document.getElementById("etfRuntimeStatus"),
  runtimeDetail: document.getElementById("etfRuntimeDetail"),
  batchFilter: document.getElementById("etfBatchFilter"),
  groupFilter: document.getElementById("etfGroupFilter"),
  filter: document.getElementById("etfFilter"),
  consensusRegion: document.getElementById("consensusRegionFilter"),
  search: document.getElementById("etfSearch"),
  reloadBtn: document.getElementById("reloadEtfBtn"),
  trackedCount: document.getElementById("trackedEtfCount"),
  etfMix: document.getElementById("etfMix"),
  updatedCount: document.getElementById("updatedEtfCount"),
  delayedCount: document.getElementById("delayedEtfCount"),
  totalAdded: document.getElementById("totalAdded"),
  totalRemoved: document.getElementById("totalRemoved"),
  totalIncreased: document.getElementById("totalIncreased"),
  totalDecreased: document.getElementById("totalDecreased"),
  batchDate: document.getElementById("etfBatchDate"),
  generatedAt: document.getElementById("etfGeneratedAt"),
  visibleCount: document.getElementById("visibleEtfCount"),
  bullishConsensus: document.getElementById("bullishConsensus"),
  bearishConsensus: document.getElementById("bearishConsensus"),
  cards: document.getElementById("etfCards"),
  sourceNote: document.getElementById("etfSourceNote")
};

function etfEscapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
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

function formatWeight(value) {
  return Number(value || 0).toLocaleString("zh-TW", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  }) + "%";
}

function formatDelta(value) {
  const number = Number(value || 0);
  return (number > 0 ? "+" : "") + number.toFixed(2) + "pp";
}

function positionText(item) {
  const shares = formatNumber(item.shares) + "股";
  if (item.market_code === "TW" && item.lots !== null && item.lots !== undefined) {
    return shares + "／" + formatNumber(item.lots) + "張";
  }
  return shares;
}

function normalizeEtf(etf) {
  return {
    ...etf,
    added: asArray(etf.added),
    removed: asArray(etf.removed),
    increased: asArray(etf.increased),
    decreased: asArray(etf.decreased),
    top10_entered: asArray(etf.top10_entered),
    top10_exited: asArray(etf.top10_exited),
    trend_3d: asArray(etf.trend_3d),
    trend_5d: asArray(etf.trend_5d)
  };
}

function matchesSearch(item, keyword) {
  if (!keyword) return true;
  const text = [item.symbol, item.name, item.market].join(" ").toLowerCase();
  return text.includes(keyword);
}

function etfMatchesSearch(etf, keyword) {
  if (!keyword) return true;
  if ([etf.code, etf.name, etf.manager].join(" ").toLowerCase().includes(keyword)) {
    return true;
  }
  return [
    etf.added,
    etf.removed,
    etf.increased,
    etf.decreased,
    etf.trend_3d,
    etf.trend_5d
  ].some((items) => items.some((item) => matchesSearch(item, keyword)));
}

function marketSymbolCell(item) {
  return '<span class="market-chip">' + etfEscapeHtml(item.market) + "</span>" +
    "<strong>" + etfEscapeHtml(item.symbol) + "</strong>";
}

function renderPositionTable(items, type, keyword) {
  const filtered = items.filter((item) => matchesSearch(item, keyword));
  const isAdded = type === "added";
  const label = isAdded ? "新增" : "剔除";
  if (!filtered.length) {
    const message = keyword ? "沒有符合搜尋條件的股票" : "本次無" + label + "成分股";
    return '<div class="etf-empty">' + etfEscapeHtml(message) + "</div>";
  }
  const shown = filtered.slice(0, 10);
  return '<div class="table-wrap etf-table-wrap"><table class="etf-change-table">' +
    "<thead><tr><th>市場／代號</th><th>名稱</th><th>" + label + "持有數</th><th>權重</th></tr></thead>" +
    "<tbody>" +
    shown.map((item) =>
      "<tr>" +
        "<td>" + marketSymbolCell(item) + "</td>" +
        "<td>" + etfEscapeHtml(item.name) + "</td>" +
        '<td class="position-value">' + etfEscapeHtml(positionText(item)) + "</td>" +
        "<td>" + etfEscapeHtml(formatWeight(isAdded ? item.current_weight : item.previous_weight)) + "</td>" +
      "</tr>"
    ).join("") +
    "</tbody></table>" +
    (filtered.length > shown.length
      ? '<div class="table-more">另有' + (filtered.length - shown.length) + "檔</div>"
      : "") +
    "</div>";
}

function renderWeightTable(items, direction, keyword) {
  const filtered = items.filter((item) => matchesSearch(item, keyword));
  const label = direction === "up" ? "加碼" : "減碼";
  if (!filtered.length) {
    const message = keyword ? "沒有符合搜尋條件的股票" : "本次無顯著" + label;
    return '<div class="etf-empty">' + etfEscapeHtml(message) + "</div>";
  }
  const shown = filtered.slice(0, 10);
  return '<div class="table-wrap etf-table-wrap"><table class="etf-change-table weight-table">' +
    "<thead><tr><th>市場／代號</th><th>名稱</th><th>權重變化</th><th>趨勢</th></tr></thead>" +
    "<tbody>" +
    shown.map((item) => {
      const streak = Number(item.streak || 0);
      const streakText = Math.abs(streak) >= 3
        ? "連續" + Math.abs(streak) + "日" + (streak > 0 ? "加碼" : "減碼")
        : Math.abs(Number(item.delta_pp)) >= 0.3 ? "明顯調整" : "小幅調整";
      return "<tr>" +
        "<td>" + marketSymbolCell(item) + "</td>" +
        "<td>" + etfEscapeHtml(item.name) + "</td>" +
        '<td class="weight-shift ' + (Number(item.delta_pp) > 0 ? "positive" : "negative") + '">' +
          etfEscapeHtml(formatWeight(item.previous_weight)) + " → " +
          etfEscapeHtml(formatWeight(item.current_weight)) +
          "<small>" + etfEscapeHtml(formatDelta(item.delta_pp)) + "</small>" +
        "</td>" +
        "<td>" + etfEscapeHtml(streakText) + "</td>" +
      "</tr>";
    }).join("") +
    "</tbody></table>" +
    (filtered.length > shown.length
      ? '<div class="table-more">另有' + (filtered.length - shown.length) + "檔</div>"
      : "") +
    "</div>";
}

function renderTrendTable(items, label, keyword) {
  const filtered = items.filter((item) => matchesSearch(item, keyword));
  if (!filtered.length) {
    return '<div class="etf-empty">本次無符合門檻的' + etfEscapeHtml(label) + "趨勢</div>";
  }
  const shown = filtered.slice(0, 8);
  return '<div class="table-wrap etf-table-wrap"><table class="etf-change-table trend-table">' +
    "<thead><tr><th>市場／代號</th><th>名稱</th><th>" + etfEscapeHtml(label) + "權重變化</th></tr></thead>" +
    "<tbody>" +
    shown.map((item) =>
      "<tr>" +
        "<td>" + marketSymbolCell(item) + "</td>" +
        "<td>" + etfEscapeHtml(item.name) + "</td>" +
        '<td class="weight-shift ' + (Number(item.delta_pp) > 0 ? "positive" : "negative") + '">' +
          etfEscapeHtml(formatWeight(item.previous_weight)) + " → " +
          etfEscapeHtml(formatWeight(item.current_weight)) +
          "<small>" + etfEscapeHtml(formatDelta(item.delta_pp)) + "</small>" +
        "</td>" +
      "</tr>"
    ).join("") +
    "</tbody></table>" +
    (filtered.length > shown.length
      ? '<div class="table-more">另有' + (filtered.length - shown.length) + "檔</div>"
      : "") +
    "</div>";
}

function statusMeta(etf, batchDate) {
  if (etf.status === "unavailable") return ["資料不可用", "error"];
  if (etf.status === "error") return ["抓取異常・沿用快照", "error"];
  if (etf.status === "baseline") return ["建立基準中", "baseline"];
  if (etf.status === "delayed" || (etf.data_date && etf.data_date < batchDate)) {
    return ["待補最新公告", "delayed"];
  }
  const hasChanges = etf.added.length + etf.removed.length +
    etf.increased.length + etf.decreased.length > 0;
  return [hasChanges ? "經理人有操作" : "本次無顯著變動", hasChanges ? "changed" : "unchanged"];
}

function renderTop10Changes(etf) {
  const entered = etf.top10_entered.slice(0, 4).map((item) =>
    '<span class="rank-chip entered">↑前十 ' + etfEscapeHtml(item.symbol) + "</span>"
  );
  const exited = etf.top10_exited.slice(0, 4).map((item) =>
    '<span class="rank-chip exited">↓退出 ' + etfEscapeHtml(item.symbol) + "</span>"
  );
  const chips = entered.concat(exited);
  return chips.length
    ? '<div class="rank-chips">' + chips.join("") + "</div>"
    : "";
}

function renderEtfCard(rawEtf, keyword, batchDate) {
  const etf = normalizeEtf(rawEtf);
  const [statusLabel, statusClass] = statusMeta(etf, batchDate);
  const openAttribute = etf.priority ? " open" : "";
  return '<article class="panel etf-card">' +
    '<div class="etf-card-header">' +
      "<div>" +
        '<p class="eyebrow">' + etfEscapeHtml(etf.code) +
          "・" + etfEscapeHtml(etf.manager) +
          "・" + etfEscapeHtml(etf.region) + "</p>" +
        "<h2>" + etfEscapeHtml(etf.name) + "</h2>" +
      "</div>" +
      '<span class="change-state ' + statusClass + '">' + etfEscapeHtml(statusLabel) + "</span>" +
    "</div>" +
    '<div class="etf-date-row">' +
      "<span>比較期間：" + etfEscapeHtml(formatEtfDate(etf.previous_date)) +
        " → " + etfEscapeHtml(formatEtfDate(etf.data_date)) + "</span>" +
      "<span>目前持股 " + etfEscapeHtml(etf.current_holdings_count) + "檔</span>" +
      '<a href="' + etfEscapeHtml(etf.official_url) +
        '" target="_blank" rel="noopener noreferrer">TWSE商品資料</a>' +
    "</div>" +
    (etf.fetch_error
      ? '<p class="etf-warning">本次抓取異常，畫面沿用最近成功快照。</p>'
      : "") +
    renderTop10Changes(etf) +
    '<div class="etf-change-grid">' +
      '<section class="etf-change-block added-block">' +
        '<div class="etf-change-title"><h3>新建倉</h3><strong>' + etf.added.length + "</strong></div>" +
        renderPositionTable(etf.added, "added", keyword) +
      "</section>" +
      '<section class="etf-change-block removed-block">' +
        '<div class="etf-change-title"><h3>完全出清</h3><strong>' + etf.removed.length + "</strong></div>" +
        renderPositionTable(etf.removed, "removed", keyword) +
      "</section>" +
    "</div>" +
    '<details class="etf-detail-section"' + openAttribute + ">" +
      "<summary><span>權重調整</span><strong>加碼" + etf.increased.length +
        "／減碼" + etf.decreased.length + "</strong></summary>" +
      '<div class="etf-change-grid">' +
        '<section class="etf-change-block increased-block">' +
          '<div class="etf-change-title"><h3>權重增加</h3><strong>' + etf.increased.length + "</strong></div>" +
          renderWeightTable(etf.increased, "up", keyword) +
        "</section>" +
        '<section class="etf-change-block decreased-block">' +
          '<div class="etf-change-title"><h3>權重下降</h3><strong>' + etf.decreased.length + "</strong></div>" +
          renderWeightTable(etf.decreased, "down", keyword) +
        "</section>" +
      "</div>" +
    "</details>" +
    '<details class="etf-detail-section">' +
      "<summary><span>中短期趨勢</span><strong>3日／5日</strong></summary>" +
      '<div class="etf-change-grid">' +
        '<section class="etf-change-block trend-block">' +
          '<div class="etf-change-title"><h3>3日權重趨勢</h3><strong>' + etf.trend_3d.length + "</strong></div>" +
          renderTrendTable(etf.trend_3d, "3日", keyword) +
        "</section>" +
        '<section class="etf-change-block trend-block">' +
          '<div class="etf-change-title"><h3>5日權重趨勢</h3><strong>' + etf.trend_5d.length + "</strong></div>" +
          renderTrendTable(etf.trend_5d, "5日", keyword) +
        "</section>" +
      "</div>" +
    "</details>" +
  "</article>";
}

function selectedBatch() {
  const history = asArray(activeEtfPayload.history);
  const selectedDate = activeEtfElements.batchFilter.value;
  if (selectedDate) {
    const selected = history.find((item) => item.batch_date === selectedDate);
    if (selected) return selected;
  }
  return {
    batch_date: activeEtfPayload.batch_date,
    generated_at: activeEtfPayload.generated_at,
    summary: activeEtfPayload.summary,
    consensus: activeEtfPayload.consensus,
    etfs: activeEtfPayload.etfs
  };
}

function calculateSummary(etfs, batchDate) {
  return {
    tracked_etfs: etfs.length,
    domestic_etfs: etfs.filter((etf) => etf.category === "domestic").length,
    foreign_etfs: etfs.filter((etf) => etf.category === "foreign").length,
    updated_etfs: etfs.filter((etf) => etf.data_date === batchDate).length,
    delayed_etfs: etfs.filter((etf) => etf.data_date && etf.data_date < batchDate).length,
    added: etfs.reduce((sum, etf) => sum + etf.added.length, 0),
    removed: etfs.reduce((sum, etf) => sum + etf.removed.length, 0),
    increased: etfs.reduce((sum, etf) => sum + etf.increased.length, 0),
    decreased: etfs.reduce((sum, etf) => sum + etf.decreased.length, 0)
  };
}

function renderSummary(batch, etfs) {
  const summary = batch.summary || calculateSummary(etfs, batch.batch_date);
  activeEtfElements.trackedCount.textContent = String(summary.tracked_etfs || etfs.length);
  activeEtfElements.etfMix.textContent =
    "台股" + (summary.domestic_etfs || 0) + "／海外" + (summary.foreign_etfs || 0);
  activeEtfElements.updatedCount.textContent = String(summary.updated_etfs || 0);
  activeEtfElements.delayedCount.textContent = String(summary.delayed_etfs || 0);
  activeEtfElements.totalAdded.textContent = String(summary.added || 0);
  activeEtfElements.totalRemoved.textContent = String(summary.removed || 0);
  activeEtfElements.totalIncreased.textContent = String(summary.increased || 0);
  activeEtfElements.totalDecreased.textContent = String(summary.decreased || 0);
  activeEtfElements.batchDate.textContent = formatEtfDate(batch.batch_date);
  activeEtfElements.generatedAt.textContent = formatGeneratedAt(batch.generated_at);
}

function renderConsensusItem(item, direction) {
  const managers = asArray(item.managers).join("、");
  const funds = asArray(item.funds);
  const managerCount = direction === "bullish"
    ? item.positive_managers || item.manager_count || 0
    : item.negative_managers || item.manager_count || 0;
  return '<details class="consensus-item ' + direction + '">' +
    "<summary>" +
      '<span class="consensus-symbol">' +
        '<span class="market-chip">' + etfEscapeHtml(item.market) + "</span>" +
        "<strong>" + etfEscapeHtml(item.symbol) + "</strong>" +
        "<small>" + etfEscapeHtml(item.name) + "</small>" +
      "</span>" +
      '<span class="consensus-metrics">' +
        "<strong>" + (Number(item.score) > 0 ? "+" : "") + etfEscapeHtml(item.score) + "</strong>" +
        "<small>" + etfEscapeHtml(item.signal) + "</small>" +
      "</span>" +
    "</summary>" +
    '<div class="consensus-detail">' +
      "<p><strong>" + etfEscapeHtml(managerCount) + "家投信</strong>、" +
        etfEscapeHtml(item.etf_count) + "檔ETF採取同向操作；權重變化合計 " +
        etfEscapeHtml(formatDelta(item.delta_pp_sum)) + "。</p>" +
      "<p>投信：" + etfEscapeHtml(managers || "--") + "</p>" +
      '<div class="consensus-funds">' +
        funds.slice(0, 8).map((fund) =>
          "<span><strong>" + etfEscapeHtml(fund.code) + "</strong> " +
          etfEscapeHtml(asArray(fund.labels).join("、")) + " " +
          etfEscapeHtml(formatDelta(fund.delta_pp)) + "</span>"
        ).join("") +
      "</div>" +
    "</div>" +
  "</details>";
}

function renderConsensus(batch) {
  const region = activeEtfElements.consensusRegion.value;
  const consensus = batch.consensus?.[region] || { bullish: [], bearish: [] };
  const bullish = asArray(consensus.bullish).slice(0, 10);
  const bearish = asArray(consensus.bearish).slice(0, 10);
  activeEtfElements.bullishConsensus.innerHTML = bullish.length
    ? bullish.map((item) => renderConsensusItem(item, "bullish")).join("")
    : '<div class="etf-empty">本批次沒有共識加碼訊號</div>';
  activeEtfElements.bearishConsensus.innerHTML = bearish.length
    ? bearish.map((item) => renderConsensusItem(item, "bearish")).join("")
    : '<div class="etf-empty">本批次沒有共識減碼訊號</div>';
}

function groupMatches(etf, group) {
  if (group === "priority") return Boolean(etf.priority);
  if (group === "domestic" || group === "foreign") return etf.category === group;
  return true;
}

function populateEtfFilter() {
  const group = activeEtfElements.groupFilter.value;
  const selected = activeEtfElements.filter.value;
  const universe = asArray(activeEtfPayload.universe)
    .filter((etf) => groupMatches(etf, group));
  activeEtfElements.filter.innerHTML =
    '<option value="all">全部（' + universe.length + "檔）</option>" +
    universe.map((etf) =>
      '<option value="' + etfEscapeHtml(etf.code) + '">' +
      etfEscapeHtml(etf.code + " " + etf.name) +
      "</option>"
    ).join("");
  activeEtfElements.filter.value = universe.some((etf) => etf.code === selected)
    ? selected
    : "all";
}

function populateBatchFilter() {
  const history = asArray(activeEtfPayload.history);
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

function renderActiveEtfs() {
  if (!activeEtfPayload || !Array.isArray(activeEtfPayload.etfs)) return;
  const batch = selectedBatch();
  const group = activeEtfElements.groupFilter.value;
  const selected = activeEtfElements.filter.value;
  const keyword = activeEtfElements.search.value.trim().toLowerCase();
  const allEtfs = asArray(batch.etfs).map(normalizeEtf);
  const etfs = allEtfs.filter((etf) =>
    groupMatches(etf, group) &&
    (selected === "all" || etf.code === selected) &&
    etfMatchesSearch(etf, keyword)
  );

  renderSummary(batch, allEtfs);
  renderConsensus(batch);
  activeEtfElements.visibleCount.textContent = "顯示" + etfs.length + "檔";
  activeEtfElements.cards.innerHTML = etfs.length
    ? etfs.map((etf) => renderEtfCard(etf, keyword, batch.batch_date)).join("")
    : '<article class="panel etf-load-error">目前篩選條件沒有符合的ETF或股票。</article>';
}

async function loadActiveEtfs() {
  activeEtfElements.runtimeStatus.textContent = "讀取資料中";
  activeEtfElements.runtimeDetail.textContent = "正在載入全市場主動ETF經理人操作";
  try {
    const response = await fetch("active-etf.json", { cache: "no-store" });
    if (!response.ok) throw new Error("找不到 active-etf.json");
    const payload = await response.json();
    if (!Array.isArray(payload.etfs) || payload.etfs.length < 5) {
      throw new Error("ETF資料不完整");
    }
    activeEtfPayload = payload;
    populateBatchFilter();
    populateEtfFilter();
    renderActiveEtfs();
    const summary = payload.summary || {};
    activeEtfElements.runtimeStatus.textContent = "經理人雷達已更新";
    activeEtfElements.runtimeDetail.textContent =
      "追蹤" + (summary.tracked_etfs || payload.etfs.length) + "檔；" +
      "新增" + (summary.added || 0) + "、剔除" + (summary.removed || 0) +
      "、加碼" + (summary.increased || 0) + "、減碼" + (summary.decreased || 0);
    const source = payload.source || {};
    activeEtfElements.sourceNote.innerHTML =
      "商品清單：" +
      '<a href="' + etfEscapeHtml(source.universe_url) +
      '" target="_blank" rel="noopener noreferrer">' +
      etfEscapeHtml(source.universe_name || "TWSE") +
      "</a>；持股整合：" +
      '<a href="' + etfEscapeHtml(source.portfolio_url) +
      '" target="_blank" rel="noopener noreferrer">' +
      etfEscapeHtml(source.portfolio_name || "公開資料") +
      "</a>。" +
      etfEscapeHtml(source.note || "");
  } catch (error) {
    activeEtfElements.runtimeStatus.textContent = "資料讀取失敗";
    activeEtfElements.runtimeDetail.textContent = error.message;
    activeEtfElements.cards.innerHTML =
      '<article class="panel etf-load-error">暫時無法取得ETF經理人操作資料，請稍後重新讀取。</article>';
  }
}

activeEtfElements.batchFilter.addEventListener("change", renderActiveEtfs);
activeEtfElements.groupFilter.addEventListener("change", () => {
  populateEtfFilter();
  renderActiveEtfs();
});
activeEtfElements.filter.addEventListener("change", renderActiveEtfs);
activeEtfElements.consensusRegion.addEventListener("change", renderActiveEtfs);
activeEtfElements.search.addEventListener("input", renderActiveEtfs);
activeEtfElements.reloadBtn.addEventListener("click", loadActiveEtfs);

loadActiveEtfs();
