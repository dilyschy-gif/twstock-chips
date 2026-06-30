// Google Apps Script: 逆勢抗跌掃描模組 v2.0
// 修正版：修正 appendRow([]) 例外，增加分頁與資料保護

var CONFIG = {
  MARKET_RED_THRESHOLD: -3.0,
  MARKET_YELLOW_THRESHOLD: -1.5,
  MARKET_GREEN_MIN: 2.0,

  MIN_VOLUME: 1000,
  MIN_INSTITUTIONAL_NET: 0,

  SCORE_THRESHOLD_RED: 70,
  SCORE_THRESHOLD_YELLOW: 50,
  SCORE_THRESHOLD_GREEN: 40,
  SCORE_THRESHOLD_NORMAL: 50,

  WEIGHT_CONTRARIAN: 0.20,
  WEIGHT_INSTITUTIONAL: 0.30,
  WEIGHT_N_BREAKOUT: 0.35,
  WEIGHT_WASHOUT: 0.15,

  SHEET_MAIN: '選股結果',
  SHEET_CHIPS: '籌碼面',
  SHEET_CONTRARIAN: '逆勢抗跌掃描',
  SHEET_BB: 'BB掃描歷史'
};

function runContrarianScan() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  var marketData = getMarketIndexData_();
  var marketLight = determineMarketLight_(marketData.changePercent, marketData.changePoints);
  var threshold = getScoreThreshold_(marketLight);

  var stocksData = getStocksFromMainSheet_(ss);
  if (!stocksData || stocksData.length === 0) {
    Logger.log('沒有可掃描的股票資料，請確認「選股結果」分頁是否存在且有內容');
    writeContrarianResults_(ss, [], marketLight, marketData, threshold);
    return;
  }

  var chipsHistory = getChipsHistory_(ss);

  var results = [];
  for (var i = 0; i < stocksData.length; i++) {
    var stock = stocksData[i];
    if (!stock.code || !stock.price) continue;

    var stockChips = chipsHistory[stock.code] || [];

    var contrarianScore = calcContrarian_(stock, marketData.changePercent);
    var institutionalScore = calcInstitutionalStreak_(stock, stockChips);
    var nBreakoutScore = calcNBreakout_(stock);
    var washoutScore = calcWashoutReversal_(stock);

    var totalScore =
      contrarianScore.score * CONFIG.WEIGHT_CONTRARIAN +
      institutionalScore.score * CONFIG.WEIGHT_INSTITUTIONAL +
      nBreakoutScore.score * CONFIG.WEIGHT_N_BREAKOUT +
      washoutScore.score * CONFIG.WEIGHT_WASHOUT;

    if (totalScore >= threshold || totalScore >= 30) {
      results.push({
        code: stock.code,
        name: stock.name,
        price: stock.price,
        changePercent: stock.changePercent || 0,
        volume: stock.volume || 0,
        volumeRatio: stock.volumeRatio || 0,
        totalScore: Math.round(totalScore * 10) / 10,
        passed: totalScore >= threshold,
        contrarianScore: contrarianScore.score,
        relativeStrength: contrarianScore.relativeStrength,
        nBreakoutScore: nBreakoutScore.score,
        hasNBreakout: nBreakoutScore.hasBreakout,
        nTarget: nBreakoutScore.nTarget,
        institutionalScore: institutionalScore.score,
        investTrustDays: institutionalScore.investTrustDays,
        foreignDays: institutionalScore.foreignDays,
        dealerDays: institutionalScore.dealerDays,
        washoutScore: washoutScore.score,
        hasWashout: washoutScore.hasWashout,
        compositeScore: stock.compositeScore || 0,
        bbSignal: stock.bbSignal || '',
        bandwidth: stock.bandwidth || 0
      });
    }
  }

  results.sort(function(a, b) { return b.totalScore - a.totalScore; });
  writeContrarianResults_(ss, results, marketLight, marketData, threshold);

  Logger.log('逆勢掃描完成：' + results.filter(function(r) { return r.passed; }).length + ' 檔達標');
}

function getMarketIndexData_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheets = ss.getSheets();

  for (var s = 0; s < sheets.length; s++) {
    var sheet = sheets[s];
    var data = sheet.getDataRange().getValues();
    for (var r = 0; r < data.length; r++) {
      for (var c = 0; c < data[r].length; c++) {
        var cellValue = String(data[r][c]);
        if (cellValue.indexOf('加權指') >= 0 || cellValue.indexOf('加權指數') >= 0 || cellValue.indexOf('加權') >= 0) {
          var rowData = data[r].join(' ');
          var pointsMatch = rowData.match(/[+-]?\d+\.?\d*\s*點/);
          var percentMatch = rowData.match(/[+-]?\d+\.?\d*\s*%/);
          var indexMatch = rowData.match(/\d{4,5}\.\d+/);

          if (percentMatch) {
            var changePercent = parseFloat(percentMatch[0].replace('%', '').replace(/\s/g, ''));
            var changePoints = pointsMatch ? parseFloat(pointsMatch[0].replace('點', '').replace(/\s/g, '')) : 0;
            var indexValue = indexMatch ? parseFloat(indexMatch[0]) : 0;
            return {
              changePercent: changePercent,
              changePoints: changePoints,
              indexValue: indexValue
            };
          }
        }
      }
    }
  }

  try {
    var url = 'https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?interval=1d&range=2d';
    var response = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    var json = JSON.parse(response.getContentText());
    var result = json.chart.result[0];
    var closes = result.indicators.quote[0].close;
    var prevClose = closes[closes.length - 2];
    var lastClose = closes[closes.length - 1];
    var changePoints = lastClose - prevClose;
    var changePercent = (changePoints / prevClose) * 100;
    return {
      changePercent: Math.round(changePercent * 100) / 100,
      changePoints: Math.round(changePoints * 100) / 100,
      indexValue: Math.round(lastClose * 100) / 100
    };
  } catch (e) {
    Logger.log('無法取得大盤數據：' + e.message);
    return { changePercent: 0, changePoints: 0, indexValue: 0 };
  }
}

function determineMarketLight_(changePercent, changePoints) {
  if (changePercent <= CONFIG.MARKET_RED_THRESHOLD) return '🔴紅燈';
  if (changePercent <= CONFIG.MARKET_YELLOW_THRESHOLD) return '🟡黃燈';
  if (changePercent >= CONFIG.MARKET_GREEN_MIN) return '🟢綠燈';
  return '⚪平燈';
}

function getScoreThreshold_(marketLight) {
  switch (marketLight) {
    case '🔴紅燈': return CONFIG.SCORE_THRESHOLD_RED;
    case '🟡黃燈': return CONFIG.SCORE_THRESHOLD_YELLOW;
    case '🟢綠燈': return CONFIG.SCORE_THRESHOLD_GREEN;
    default: return CONFIG.SCORE_THRESHOLD_NORMAL;
  }
}

function calcContrarian_(stock, marketChangePercent) {
  var score = 0;
  var stockChange = stock.changePercent || 0;
  var relativeStrength = 0;

  if (marketChangePercent < -1) {
    var scoreA = 0;
    if (stockChange > 0) {
      scoreA = 30;
      if (stock.volumeRatio > 1.5) scoreA = 50;
    }

    relativeStrength = Math.abs(marketChangePercent) - Math.abs(Math.min(stockChange, 0));
    var scoreB = 0;
    if (relativeStrength >= 2) scoreB = 50;
    else if (relativeStrength >= 1) scoreB = 35;
    else if (relativeStrength >= 0.5) scoreB = 20;

    score = Math.max(scoreA, scoreB);
  } else if (marketChangePercent >= CONFIG.MARKET_GREEN_MIN) {
    if (stockChange > marketChangePercent * 1.5) score = 30;
  }

  return {
    score: score,
    relativeStrength: Math.round(relativeStrength * 100) / 100
  };
}

function calcNBreakout_(stock) {
  var score = 0;
  var hasBreakout = false;
  var nTarget = stock.nTarget || 0;

  if (nTarget > 0 && stock.price > 0) {
    var upside = ((nTarget - stock.price) / stock.price) * 100;
    if (upside > 5) score += 20;

    var signal = String(stock.bbSignal || '').trim();
    if (signal.indexOf('起漲') >= 0) {
      score += 30;
      hasBreakout = true;
    } else if (signal.indexOf('多頭') >= 0) {
      score += 20;
    } else if (signal.indexOf('收斂') >= 0) {
      score += 10;
    }

    if (stock.bandwidth > 0 && stock.bandwidth < 8) score += 20;
    if (stock.startPrice > 0 && stock.price > stock.startPrice) score += 10;
  }

  if (hasBreakout && stock.volumeRatio > 1.5) score += 20;

  return {
    score: Math.min(score, 100),
    hasBreakout: hasBreakout,
    nTarget: nTarget
  };
}

function calcInstitutionalStreak_(stock, chipsHistory) {
  var score = 0;
  var detail = String(stock.chipsDetail || '');
  var investTrustDays = 0;
  var foreignDays = 0;
  var dealerDays = 0;

  var trustMatch = detail.match(/投信連買(\d+)日/);
  if (trustMatch) investTrustDays = parseInt(trustMatch[1]);

  var foreignMatch = detail.match(/外資連買(\d+)日/);
  if (foreignMatch) foreignDays = parseInt(foreignMatch[1]);

  var dealerMatch = detail.match(/自營商連買(\d+)日/);
  if (dealerMatch) dealerDays = parseInt(dealerMatch[1]);

  if (investTrustDays >= 15) score += 50;
  else if (investTrustDays >= 10) score += 40;
  else if (investTrustDays >= 5) score += 25;
  else if (investTrustDays >= 3) score += 15;

  if (foreignDays >= 5) score += 20;
  else if (foreignDays >= 3) score += 10;

  if (dealerDays >= 3) score += 10;

  if (detail.indexOf('三法人同買') >= 0) score += 20;

  return {
    score: Math.min(score, 100),
    investTrustDays: investTrustDays,
    foreignDays: foreignDays,
    dealerDays: dealerDays
  };
}

function calcWashoutReversal_(stock) {
  var score = 0;
  var hasWashout = false;
  var kValue = stock.kValue || 50;
  var dValue = stock.dValue || 50;

  if (kValue < 30 && kValue > dValue) {
    score += 25;
    hasWashout = true;
  } else if (kValue < 40 && kValue > dValue) {
    score += 15;
  }

  if (hasWashout && stock.volumeRatio > 2.0) score += 25;
  else if (stock.volumeRatio > 3.0) score += 15;

  return {
    score: Math.min(score, 50),
    hasWashout: hasWashout
  };
}

function getStocksFromMainSheet_(ss) {
  var sheet = ss.getSheetByName(CONFIG.SHEET_MAIN);
  if (!sheet) {
    Logger.log('找不到「' + CONFIG.SHEET_MAIN + '」分頁');
    return [];
  }

  var data = sheet.getDataRange().getValues();
  if (!data || data.length < 2) {
    Logger.log('「' + CONFIG.SHEET_MAIN + '」分頁沒有資料');
    return [];
  }

  var headers = data[0];
  var stocks = [];
  var colMap = {};

  for (var c = 0; c < headers.length; c++) {
    var h = String(headers[c]).trim();
    if (h === '代號') colMap.code = c;
    else if (h === '名稱') colMap.name = c;
    else if (h === '現價') colMap.price = c;
    else if (h === 'BB訊號') colMap.bbSignal = c;
    else if (h === 'N字目標') colMap.nTarget = c;
    else if (h === '起漲點') colMap.startPrice = c;
    else if (h === '帶寬') colMap.bandwidth = c;
    else if (h === 'K值') colMap.kValue = c;
    else if (h === 'D值') colMap.dValue = c;
    else if (h === '量比') colMap.volumeRatio = c;
    else if (h === 'compositeScore') colMap.compositeScore = c;
    else if (h === 'chipsDetail') colMap.chipsDetail = c;
    else if (h === 'badges') colMap.badges = c;
  }

  for (var r = 1; r < data.length; r++) {
    var row = data[r];
    var code = String(row[colMap.code] || '').trim();
    if (!code || code.length < 4) continue;

    stocks.push({
      code: code,
      name: String(row[colMap.name] || ''),
      price: parseFloat(row[colMap.price]) || 0,
      bbSignal: String(row[colMap.bbSignal] || ''),
      nTarget: parseFloat(row[colMap.nTarget]) || 0,
      startPrice: parseFloat(row[colMap.startPrice]) || 0,
      bandwidth: parseFloat(row[colMap.bandwidth]) || 0,
      kValue: parseFloat(row[colMap.kValue]) || 50,
      dValue: parseFloat(row[colMap.dValue]) || 50,
      volumeRatio: parseFloat(row[colMap.volumeRatio]) || 0,
      compositeScore: parseFloat(row[colMap.compositeScore]) || 0,
      chipsDetail: String(row[colMap.chipsDetail] || ''),
      badges: String(row[colMap.badges] || ''),
      changePercent: 0,
      volume: 0
    });
  }

  return stocks;
}

function getChipsHistory_(ss) {
  var sheet = ss.getSheetByName(CONFIG.SHEET_CHIPS);
  if (!sheet) return {};

  var data = sheet.getDataRange().getValues();
  var history = {};

  for (var r = 1; r < data.length; r++) {
    var row = data[r];
    var code = String(row[1] || '').trim();
    if (!code || code.length < 4) continue;

    if (!history[code]) history[code] = [];
    history[code].push({
      date: row[0],
      foreign: parseFloat(row[4]) || 0,
      investTrust: parseFloat(row[5]) || 0,
      dealer: parseFloat(row[6]) || 0,
      total: parseFloat(row[7]) || 0
    });
  }

  return history;
}

function writeContrarianResults_(ss, results, marketLight, marketData, threshold) {
  var sheet = ss.getSheetByName(CONFIG.SHEET_CONTRARIAN);
  if (!sheet) sheet = ss.insertSheet(CONFIG.SHEET_CONTRARIAN);
  sheet.clear();

  var now = new Date();
  var timeStr = Utilities.formatDate(now, 'Asia/Taipei', 'yyyy/MM/dd HH:mm');
  var passedCount = results.filter(function(r) { return r.passed; }).length;

  sheet.appendRow([
    '掃描時間：' + timeStr,
    '大盤燈號：' + marketLight,
    '漲跌幅：' + marketData.changePercent + '%（' + marketData.changePoints + '點）',
    '篩選門檻：' + threshold,
    '達標：' + passedCount + ' 檔'
  ]);

  // 原本這裡是 appendRow([]) 會丟錯，改成 19 欄空字串
  sheet.appendRow(new Array(19).fill(''));

  sheet.appendRow([
    '代號', '名稱', '現價', '總分', '達標',
    '抗跌分', '相對強度',
    'N突破分', 'N字目標', '突破',
    '法人分', '投信連買', '外資連買', '自營連買',
    '洗盤分', '洗盤訊號',
    '原始綜合分', 'BB訊號', '帶寬%'
  ]);

  for (var i = 0; i < results.length; i++) {
    var r = results[i];
    sheet.appendRow([
      r.code,
      r.name,
      r.price,
      r.totalScore,
      r.passed ? '✅' : '',
      r.contrarianScore,
      r.relativeStrength,
      r.nBreakoutScore,
      r.nTarget || '',
      r.hasNBreakout ? '✅突破' : '',
      r.institutionalScore,
      r.investTrustDays > 0 ? r.investTrustDays + '日' : '',
      r.foreignDays > 0 ? r.foreignDays + '日' : '',
      r.dealerDays > 0 ? r.dealerDays + '日' : '',
      r.washoutScore,
      r.hasWashout ? '✅洗盤反轉' : '',
      r.compositeScore,
      r.bbSignal,
      r.bandwidth
    ]);
  }

  if (results.length > 0) {
    var headerRow = sheet.getRange(3, 1, 1, 19);
    headerRow.setFontWeight('bold');
    headerRow.setBackground('#E8F5E9');

    for (var j = 0; j < results.length; j++) {
      if (results[j].passed) sheet.getRange(j + 4, 1, 1, 19).setBackground('#FFF9C4');
    }
  }

  for (var col = 1; col <= 19; col++) {
    sheet.autoResizeColumn(col);
  }

  Logger.log('結果已寫入「' + CONFIG.SHEET_CONTRARIAN + '」分頁');
}

function setupContrarianTrigger() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'runContrarianScan') {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }

  ScriptApp.newTrigger('runContrarianScan')
    .timeBased()
    .atHour(18)
    .nearMinute(30)
    .everyDays(1)
    .inTimezone('Asia/Taipei')
    .create();

  Logger.log('逆勢掃描觸發器已設定：每日 18:30（台灣時間）');
}
