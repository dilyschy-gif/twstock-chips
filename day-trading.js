/* 當沖選股雷達 — 前端
 * 讀取 GitHub Actions 產出的 public/data/day-trading/day-trading.json，
 * 呈現族群熱度排行與三條件隔日觀察名單。純前端過濾，不打任何外部 API。
 */

(function () {
  "use strict";

  var DATA_URL = "./public/data/day-trading/day-trading.json";

  var state = {
    data: null,
  };

  var $ = function (id) { return document.getElementById(id); };

  // ── 小工具 ────────────────────────────────────────────────

  function fmtInt(n) {
    if (n === null || n === undefined) return "—";
    return Number(n).toLocaleString("zh-Hant-TW");
  }

  function fmtTurnover(n) {
    if (n === null || n === undefined) return "—";
    if (n >= 1e8) return (n / 1e8).toFixed(2) + " 億";
    if (n >= 1e4) return Math.round(n / 1e4).toLocaleString("zh-Hant-TW") + " 萬";
    return String(n);
  }

  function fmtPct(n, digits) {
    if (n === null || n === undefined) return "—";
    return Number(n).toFixed(digits === undefined ? 2 : digits) + "%";
  }

  function fmtSigned(n) {
    if (n === null || n === undefined) return "—";
    var s = Number(n).toFixed(2);
    return (n > 0 ? "+" : "") + s + "%";
  }

  function setStatus(title, detail, ok) {
    $("runtimeStatus").textContent = title;
    $("runtimeDetail").textContent = detail || "";
    var dot = document.querySelector(".status-dot");
    if (dot) dot.style.background = ok ? "#22c55e" : "#f59e0b";
  }

  function escapeHtml(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ── Sparkline（近30日收盤，SVG polyline）────────────────────

  function sparkline(closes) {
    if (!closes || closes.length < 2) return "";
    var w = 84, h = 26, pad = 2;
    var min = Math.min.apply(null, closes);
    var max = Math.max.apply(null, closes);
    var span = (max - min) || 1;
    var pts = closes.map(function (c, i) {
      var x = pad + (w - 2 * pad) * i / (closes.length - 1);
      var y = pad + (h - 2 * pad) * (1 - (c - min) / span);
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    var trendDown = closes[closes.length - 1] < closes[0];
    return '<svg class="spark' + (trendDown ? " down" : "") + '" width="' + w +
      '" height="' + h + '" viewBox="0 0 ' + w + " " + h +
      '" aria-hidden="true"><polyline points="' + pts + '"></polyline></svg>';
  }

  // ── 族群熱度面板 ───────────────────────────────────────────

  function renderSectors(sectors) {
    var grid = $("sectorGrid");
    var top = sectors.slice(0, 10);
    grid.innerHTML = top.map(function (s) {
      return (
        '<article class="sector-card' + (s.hot ? " hot" : "") + '">' +
        '<span class="sector-rank">#' + s.rank + "</span>" +
        "<h3>" + escapeHtml(s.name) +
        (s.hot ? '<span class="sector-flame">熱門</span>' : "") + "</h3>" +
        "<dl>" +
        "<dt>加權漲幅</dt><dd>" + fmtSigned(s.w_change) + "</dd>" +
        "<dt>量比中位</dt><dd>" + (s.med_vol_ratio === null ? "—" : s.med_vol_ratio.toFixed(2) + "x") + "</dd>" +
        "<dt>成交占比</dt><dd>" + fmtPct(s.turnover_share) + "</dd>" +
        "<dt>上漲家數比</dt><dd>" + fmtPct(s.gainers_pct, 0) + "</dd>" +
        "</dl></article>"
      );
    }).join("");
  }

  // ── 名單表格 ───────────────────────────────────────────────

  function condBadges(cond) {
    function b(on, label, title) {
      return '<span class="cond-badge' + (on ? " on" : "") + '" title="' +
        title + '">' + label + "</span>";
    }
    return '<span class="cond-badges">' +
      b(cond.vol, "量", "成交量 ≥ 門檻或成交金額 ≥ 5,000 萬") +
      b(cond.amp, "波", "近 30 日平均每日振幅 ≥ 3%") +
      b(cond.hot, "題", "熱門族群成員或焦點資金股") +
      "</span>";
  }

  function applyFilters() {
    var data = state.data;
    if (!data) return;

    var q = $("stockSearch").value.trim().toLowerCase();
    var minPass = Number($("passFilter").value);
    var market = $("marketFilter").value;
    var minLots = Number($("volFilter").value);
    var sortBy = $("sortBy").value;

    var rows = data.stocks.filter(function (s) {
      if (s.passed < minPass) return false;
      if (market !== "all" && s.market !== market) return false;
      // 量門檻收緊：金額 5,000 萬的替代路徑只在預設 1,000 張時適用
      if (minLots > 1000 && s.volume_lots < minLots) return false;
      if (q) {
        var hay = (s.code + " " + s.name).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });

    rows.sort(function (a, b) {
      if (sortBy === "score" && b.passed !== a.passed) return b.passed - a.passed;
      var av = a[sortBy], bv = b[sortBy];
      if (av === null || av === undefined) av = -Infinity;
      if (bv === null || bv === undefined) bv = -Infinity;
      return bv - av;
    });

    var tbody = $("stockRows");
    tbody.innerHTML = rows.map(function (s) {
      var chgCls = s.change_pct > 0 ? "up" : (s.change_pct < 0 ? "down" : "");
      return "<tr>" +
        "<td><strong>" + escapeHtml(s.code) + "</strong></td>" +
        "<td>" + escapeHtml(s.name) + "</td>" +
        "<td>" + escapeHtml(s.market) + "</td>" +
        "<td>" + escapeHtml(s.industry) +
        (s.hot_reason && s.hot_reason.indexOf("焦點") !== -1
          ? '<br><span class="hot-reason">' + escapeHtml(s.hot_reason) + "</span>"
          : "") + "</td>" +
        '<td class="num">' + (s.close === null ? "—" : s.close) + "</td>" +
        '<td class="num ' + chgCls + '">' + fmtSigned(s.change_pct) + "</td>" +
        '<td class="num">' + fmtInt(s.volume_lots) + "</td>" +
        '<td class="num">' + fmtTurnover(s.turnover) + "</td>" +
        '<td class="num">' + (s.vol_ratio === null ? "—" : s.vol_ratio.toFixed(2) + "x") + "</td>" +
        '<td class="num">' + fmtPct(s.amp30) + "</td>" +
        '<td class="num">' + fmtPct(s.amp_today) + "</td>" +
        '<td class="num">' + (s.dt_ratio === null ? "—" : fmtPct(s.dt_ratio, 1)) + "</td>" +
        "<td>" + condBadges(s.cond) + "</td>" +
        '<td class="num"><span class="score-pill' + (s.score >= 80 ? " high" : "") + '">' +
        s.score + "</span></td>" +
        "<td>" + sparkline(s.closes) + "</td>" +
        "</tr>";
    }).join("");

    $("emptyHint").hidden = rows.length > 0;
    $("listTitle").textContent = "隔日當沖觀察名單（" + rows.length + " 檔）";
  }

  // ── 載入 ───────────────────────────────────────────────────

  function load() {
    setStatus("讀取資料中", "day-trading.json", true);
    fetch(DATA_URL, { cache: "no-cache" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        state.data = data;
        $("dataDate").textContent = data.data_date || "--";
        $("pass3Count").textContent = fmtInt(data.counts.pass3);
        $("pass2Count").textContent = fmtInt(data.counts.pass2);
        var hot = data.sectors.filter(function (s) { return s.hot; });
        $("hotSectorCount").textContent = hot.length;
        $("lastUpdated").textContent = "資料日 " + data.data_date +
          "・產出 " + (data.generated_at_taipei || "");
        renderSectors(data.sectors);
        applyFilters();
        setStatus("資料已載入", data.data_date + "・universe " +
          fmtInt(data.counts.universe) + " 檔", true);
      })
      .catch(function (err) {
        setStatus("資料尚未產生", String(err), false);
        $("lastUpdated").textContent = "請先在 GitHub Actions 執行「每日當沖選股雷達」" +
          "（首次請用 workflow_dispatch 並填 backfill 60）";
      });
  }

  ["stockSearch", "passFilter", "marketFilter", "volFilter", "sortBy"]
    .forEach(function (id) {
      $(id).addEventListener("input", applyFilters);
      $(id).addEventListener("change", applyFilters);
    });
  $("reloadBtn").addEventListener("click", load);

  load();
})();
