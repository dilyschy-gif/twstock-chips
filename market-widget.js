(() => {
  const elements = {
    updated: document.getElementById("marketUpdated"),
    usdTwdValue: document.getElementById("usdTwdValue"),
    usdTwdChange: document.getElementById("usdTwdChange"),
    twiiValue: document.getElementById("twiiValue"),
    twiiChange: document.getElementById("twiiChange"),
    message: document.getElementById("marketMessage")
  };

  if (!elements.updated || !elements.message) return;

  loadMarketOverview();

  async function loadMarketOverview() {
    setMessage("正在讀取匯率與指數...", false);

    try {
      const response = await fetch("/api/market", { cache: "no-store" });
      if (!response.ok) throw new Error("總體資料讀取失敗");
      const payload = await response.json();

      renderMetric(elements.usdTwdValue, elements.usdTwdChange, payload.usdTwd, {
        decimals: 3,
        changeDecimals: 3,
        suffix: "",
        percentOnly: true
      });
      renderMetric(elements.twiiValue, elements.twiiChange, payload.twii, {
        decimals: 2,
        changeDecimals: 2,
        suffix: " 點",
        percentOnly: false
      });

      elements.updated.textContent = formatUpdated(payload.updatedAt);
      setMessage(buildMarketMessage(payload), false);
    } catch (error) {
      elements.updated.textContent = "讀取失敗";
      elements.usdTwdValue.textContent = "--";
      elements.usdTwdChange.textContent = "--";
      elements.twiiValue.textContent = "--";
      elements.twiiChange.textContent = "--";
      setMessage(error.message || "總體資料暫時無法讀取", true);
    }
  }

  function renderMetric(valueEl, changeEl, item, options) {
    if (!item || !Number.isFinite(item.value)) {
      valueEl.textContent = "--";
      changeEl.textContent = "--";
      changeEl.className = "";
      return;
    }

    valueEl.textContent = formatNumber(item.value, options.decimals);
    const change = Number(item.change || 0);
    const changePct = Number(item.changePct || 0);
    const prefix = change > 0 ? "+" : "";
    const changeText = options.percentOnly
      ? prefix + formatNumber(changePct, 2) + "%"
      : prefix + formatNumber(changePct, 2) + "%（" + prefix + formatNumber(change, options.changeDecimals) + options.suffix + "）";

    changeEl.textContent = changeText;
    changeEl.className = change > 0 ? "positive" : change < 0 ? "negative" : "";
  }

  function buildMarketMessage(payload) {
    const twii = payload.twii || {};
    const usdTwd = payload.usdTwd || {};
    const indexTrend = Number(twii.change || 0) >= 0 ? "台股指數偏強" : "台股指數偏弱";
    const fxTrend = Number(usdTwd.change || 0) >= 0 ? "美元兌台幣走升" : "美元兌台幣走弱";
    return "大盤監控：" + indexTrend + "；" + fxTrend + "。請搭配籌碼與技術訊號判斷進出場。";
  }

  function formatNumber(value, decimals) {
    return Number(value).toLocaleString("zh-TW", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    });
  }

  function formatUpdated(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "剛剛更新";
    return "更新：" + date.toLocaleString("zh-TW", { hour12: false });
  }

  function setMessage(text, warning) {
    elements.message.textContent = text;
    elements.message.classList.toggle("warning", Boolean(warning));
  }
})();
