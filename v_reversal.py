# -*- coding: utf-8 -*-
"""V 型反轉早期辨識。

狀態定義：
- V0：左臂急跌、連黑、超賣，尚未出現合格轉折紅 K。
- V1：第一根合格轉折紅 K，重點是收盤位置與上影線，不把長上影列為買點。
- V2：V1 後守住紅 K 中值，並站上第一確認價。
- V3：從 V 底收復左臂跌幅 50%，且短均線轉強。
- VX：跌破 V 底、連續失守紅 K 中值，或三日內無法站回短均線。

這個模組只處理純資料計算，方便在沒有 Google Sheet 憑證時單獨測試。
"""

from typing import Dict, List, Optional


MIN_HISTORY_DAYS = 30
MIN_AVG_VOLUME_LOTS = 300
LEFT_ARM_MIN_DROP = 0.10


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sanitize_history(history: List[Dict]) -> List[Dict]:
    """移除停牌/補值列，並統一 OHLCV 型別與日期順序。"""
    rows = []
    for item in history or []:
        open_price = _number(item.get("open"))
        high = _number(item.get("high"))
        low = _number(item.get("low"))
        close = _number(item.get("close"))
        volume = _number(item.get("volume"))
        if min(open_price, high, low, close) <= 0 or volume <= 0:
            continue
        if high < max(open_price, close) or low > min(open_price, close):
            continue
        rows.append({
            "date": str(item.get("date", "")),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })
    rows.sort(key=lambda row: row["date"])
    return rows


def simple_ma(values: List[float], period: int) -> float:
    window = values[-period:]
    return sum(window) / len(window) if window else 0.0


def calculate_rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < 2:
        return 50.0
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    window = changes[-period:]
    gains = sum(max(change, 0) for change in window) / len(window)
    losses = sum(max(-change, 0) for change in window) / len(window)
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    relative_strength = gains / losses
    return 100 - (100 / (1 + relative_strength))


def calculate_atr(rows: List[Dict], period: int = 14) -> float:
    if len(rows) < 2:
        return 0.0
    true_ranges = []
    for index in range(1, len(rows)):
        current = rows[index]
        previous_close = rows[index - 1]["close"]
        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        ))
    window = true_ranges[-period:]
    return sum(window) / len(window) if window else 0.0


def _has_suspicious_price_gap(rows: List[Dict]) -> bool:
    """台股一般日漲跌幅不會超過 10%；35% 跳動多半是除權息/資料異常。"""
    recent = rows[-7:]
    for previous, current in zip(recent, recent[1:]):
        if previous["close"] and abs(current["close"] / previous["close"] - 1) > 0.35:
            return True
    return False


def _average_prior_volume(rows: List[Dict], index: int, period: int = 20) -> float:
    volumes = [row["volume"] for row in rows[max(0, index - period):index]]
    return sum(volumes) / len(volumes) if volumes else 0.0


def _base_context(rows: List[Dict], index: int, for_trigger: bool) -> Optional[Dict]:
    if index < 20:
        return None

    current = rows[index]
    closes = [row["close"] for row in rows[:index + 1]]
    prior_closes = closes[:-1]
    left_window = rows[max(0, index - 10):index]
    bottom_window = rows[max(0, index - 4):index + 1]
    left_peak = max(row["high"] for row in left_window)
    v_bottom = min(row["low"] for row in bottom_window)
    left_drop = (left_peak - v_bottom) / left_peak if left_peak else 0.0

    if for_trigger:
        black_rows = rows[max(0, index - 3):index]
        rsi_reference = calculate_rsi(prior_closes)
    else:
        black_rows = rows[max(0, index - 2):index + 1]
        rsi_reference = calculate_rsi(closes)

    black_count = sum(row["close"] < row["open"] for row in black_rows)
    ma5 = simple_ma(closes, 5)
    ma10 = simple_ma(closes, 10)
    ma20 = simple_ma(closes, 20)
    avg_volume_20 = _average_prior_volume(rows, index)
    near_bottom_pct = (current["close"] - v_bottom) / v_bottom if v_bottom else 1.0
    recovery_ratio = (
        (current["close"] - v_bottom) / (left_peak - v_bottom)
        if left_peak > v_bottom else 1.0
    )

    return {
        "left_peak": left_peak,
        "v_bottom": v_bottom,
        "left_drop": left_drop,
        "black_count": black_count,
        "rsi": calculate_rsi(closes),
        "rsi_reference": rsi_reference,
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "avg_volume_20": avg_volume_20,
        "avg_volume_lots": avg_volume_20 / 1000,
        "near_bottom_pct": near_bottom_pct,
        "recovery_ratio": recovery_ratio,
    }


def _is_base_ready(context: Dict, for_trigger: bool) -> bool:
    near_limit = 0.12 if for_trigger else 0.08
    rsi_limit = 40 if for_trigger else 38
    price_limit = context["ma20"] * 1.05 if for_trigger else context["ma10"] * 1.03
    return all([
        context["left_drop"] >= LEFT_ARM_MIN_DROP,
        context["black_count"] >= 2,
        context["rsi_reference"] <= rsi_limit,
        context["avg_volume_lots"] >= MIN_AVG_VOLUME_LOTS,
        context["near_bottom_pct"] <= near_limit,
        context["recovery_ratio"] < 0.50,
        context["ma20"] > 0,
        context.get("current_close", 0) <= price_limit,
    ])


def _trigger_context(
    rows: List[Dict],
    index: int,
    market_change_pct: float,
    enforce_relative_strength: bool = True,
) -> Optional[Dict]:
    if index < 20:
        return None

    current = rows[index]
    previous = rows[index - 1]
    context = _base_context(rows, index, for_trigger=True)
    if not context:
        return None
    context["current_close"] = current["close"]
    if not _is_base_ready(context, for_trigger=True):
        return None

    candle_range = current["high"] - current["low"]
    body = current["close"] - current["open"]
    upper_wick = current["high"] - max(current["open"], current["close"])
    close_location = (current["close"] - current["low"]) / candle_range if candle_range else 0.0
    upper_wick_ratio = upper_wick / candle_range if candle_range else 1.0
    day_return = (
        (current["close"] - previous["close"]) / previous["close"] * 100
        if previous["close"] else 0.0
    )
    atr = calculate_atr(rows[:index + 1])
    body_atr = body / atr if atr else 0.0
    volume_ratio = (
        current["volume"] / context["avg_volume_20"]
        if context["avg_volume_20"] else 0.0
    )
    relative_strength = day_return - market_change_pct
    locked_limit_up = day_return >= 9.5 and close_location >= 0.95 and upper_wick_ratio <= 0.05
    strong_body = day_return >= 4.0 or body_atr >= 0.8
    reclaimed_pressure = any([
        current["close"] > previous["high"],
        current["close"] >= previous["open"],
        current["close"] >= (previous["open"] + previous["close"]) / 2,
    ])
    volume_ok = volume_ratio >= 1.2 or (locked_limit_up and volume_ratio >= 0.45)
    if market_change_pct >= 3:
        relative_ok = relative_strength >= 3.0 or locked_limit_up
    else:
        relative_ok = relative_strength >= 1.5 or locked_limit_up

    if not all([
        current["close"] > current["open"],
        strong_body,
        close_location >= 0.80,
        upper_wick_ratio <= 0.20,
        reclaimed_pressure,
        volume_ok,
        relative_ok if enforce_relative_strength else True,
    ]):
        return None

    prior_two_high = max(row["high"] for row in rows[max(0, index - 2):index])
    context.update({
        "trigger_index": index,
        "trigger_date": current["date"],
        "trigger_open": current["open"],
        "trigger_high": current["high"],
        "trigger_low": current["low"],
        "trigger_close": current["close"],
        "trigger_mid": (current["high"] + current["low"]) / 2,
        "v2_confirm": max(current["high"], context["ma5"], prior_two_high),
        "day_return": day_return,
        "body_atr": body_atr,
        "close_location": close_location,
        "upper_wick_ratio": upper_wick_ratio,
        "volume_ratio": volume_ratio,
        "relative_strength": relative_strength,
        "locked_limit_up": locked_limit_up,
        "reclaimed_pressure": reclaimed_pressure,
    })
    return context


def _chip_score(chip: Dict) -> int:
    score = 0
    if chip.get("trust_streak", 0) >= 5:
        score += 8
    elif chip.get("trust_positive_days_5", 0) >= 3:
        score += 5
    elif chip.get("latest_trust", 0) > 0:
        score += 2
    if chip.get("foreign_turn_buy"):
        score += 5
    elif chip.get("foreign_positive_days_2", 0) >= 2:
        score += 3
    if chip.get("latest_total", 0) > 0:
        score += 2
    return min(score, 15)


def _institutional_signal(chip: Dict) -> str:
    parts = []
    if chip.get("trust_streak", 0) >= 5:
        parts.append(f"投信連買{chip['trust_streak']}日")
    elif chip.get("trust_positive_days_5", 0) >= 3:
        parts.append("投信5日買超至少3日")
    if chip.get("foreign_turn_buy"):
        parts.append("外資翻多")
    elif chip.get("foreign_positive_days_2", 0) >= 2:
        parts.append("外資連2買")
    if chip.get("latest_total", 0) > 0:
        parts.append("三法人買超")
    return "、".join(parts) or "籌碼中性"


def _score(context: Dict, trigger: Optional[Dict], chip: Dict) -> int:
    drop_score = min(15, max(0, (context["left_drop"] - 0.08) / 0.12 * 15))
    rsi = context["rsi_reference"]
    rsi_score = 10 if rsi <= 30 else 8 if rsi <= 35 else 5 if rsi <= 40 else 0
    black_score = 5 if context["black_count"] >= 3 else 3
    near_score = max(0, 10 * (1 - min(context["recovery_ratio"], 1)))
    liquidity_score = 5 if context["avg_volume_lots"] >= MIN_AVG_VOLUME_LOTS else 0
    total = drop_score + rsi_score + black_score + near_score + liquidity_score + _chip_score(chip)

    if trigger:
        candle_score = 0
        candle_score += 10 if trigger["close_location"] >= 0.90 else 8
        candle_score += 5 if trigger["upper_wick_ratio"] <= 0.08 else 3
        candle_score += 5 if trigger["reclaimed_pressure"] else 0
        candle_score += 5 if trigger["day_return"] >= 6 or trigger["body_atr"] >= 1 else 3
        total += min(candle_score, 25)
        if trigger["volume_ratio"] >= 1.5:
            total += 10
        elif trigger["volume_ratio"] >= 1.2:
            total += 8
        elif trigger["locked_limit_up"]:
            total += 7
        total += 5 if trigger["relative_strength"] >= 3 or trigger["locked_limit_up"] else 2
    return round(min(total, 100))


def _badges(state: str, context: Dict, trigger: Optional[Dict], chip: Dict) -> List[str]:
    badges = [state, f"左臂跌{context['left_drop'] * 100:.1f}%"]
    if context["rsi_reference"] <= 38:
        badges.append("RSI超賣")
    if context["black_count"] >= 3:
        badges.append("連3黑")
    elif context["black_count"] >= 2:
        badges.append("3日2黑")
    if trigger:
        badges.append("第一根轉折紅K" if state == "V1" else "已出現轉折紅K")
        if trigger["close_location"] >= 0.95:
            badges.append("收近最高")
        if trigger["upper_wick_ratio"] <= 0.05:
            badges.append("無長上影")
        if trigger["locked_limit_up"]:
            badges.append("漲停鎖住")
            if trigger["volume_ratio"] < 1.2:
                badges.append("低量漲停例外")
    if chip.get("trust_streak", 0) >= 5:
        badges.append(f"投信連買{chip['trust_streak']}日")
    if chip.get("foreign_turn_buy"):
        badges.append("外資翻多")
    return badges


def _build_result(state: str, rows: List[Dict], context: Dict, trigger: Optional[Dict], chip: Dict) -> Dict:
    current = rows[-1]
    left_peak = context["left_peak"]
    v_bottom = context["v_bottom"]
    price_range = max(left_peak - v_bottom, 0)
    trigger_mid = trigger.get("trigger_mid") if trigger else None
    v2_confirm = trigger.get("v2_confirm") if trigger else max(
        context["ma5"],
        max(row["high"] for row in rows[-2:]),
    )
    score = _score(context, trigger, chip)
    badges = _badges(state, context, trigger, chip)

    state_notes = {
        "V0": "左臂急跌與超賣已成立，等待第一根收近最高且無長上影的強紅K。",
        "V1": "第一根轉折紅K成立；先觀察紅K中值是否守住，不追隔日跳空。",
        "V2": "已守住紅K中值並站上第一確認價，可列為右腳啟動觀察。",
        "V3": "已收復左臂跌幅50%且短均線轉強，型態成立但不再屬最早買點。",
        "VX": "V型條件失敗，暫不列入買點。",
    }
    level_note = f"V底{v_bottom:.2f}；第一確認{v2_confirm:.2f}"
    if trigger_mid is not None:
        level_note += f"；紅K中值{trigger_mid:.2f}"

    return {
        "state": state,
        "score": score,
        "close": current["close"],
        "left_drop_pct": context["left_drop"] * 100,
        "rsi14": context["rsi"],
        "black_count": context["black_count"],
        "close_location": trigger.get("close_location") if trigger else None,
        "upper_wick_ratio": trigger.get("upper_wick_ratio") if trigger else None,
        "volume_ratio": trigger.get("volume_ratio") if trigger else (
            current["volume"] / context["avg_volume_20"] if context["avg_volume_20"] else 0
        ),
        "relative_strength": trigger.get("relative_strength") if trigger else None,
        "institutional_signal": _institutional_signal(chip),
        "left_peak": left_peak,
        "v_bottom": v_bottom,
        "trigger_mid": trigger_mid,
        "v2_confirm": v2_confirm,
        "recover_50": v_bottom + price_range * 0.5,
        "recover_618": v_bottom + price_range * 0.618,
        "invalid_price": v_bottom,
        "trigger_date": trigger.get("trigger_date") if trigger else "",
        "badges": "、".join(badges),
        "chips_detail": chip.get("chips_detail", ""),
        "note": f"{state_notes[state]} {level_note}",
    }


def evaluate_v_reversal(
    history: List[Dict],
    chip: Optional[Dict] = None,
    market_change_pct: float = 0.0,
) -> Optional[Dict]:
    """回傳目前 V 狀態與關鍵價位；完全不符合時回傳 None。"""
    rows = sanitize_history(history)
    chip = chip or {}
    if len(rows) < MIN_HISTORY_DAYS or _has_suspicious_price_gap(rows):
        return None

    current_index = len(rows) - 1
    current_trigger = _trigger_context(rows, current_index, market_change_pct)
    if current_trigger:
        return _build_result("V1", rows, current_trigger, current_trigger, chip)

    recent_trigger = None
    for days_ago in range(1, 4):
        trigger_index = current_index - days_ago
        candidate = _trigger_context(
            rows,
            trigger_index,
            market_change_pct=0.0,
            enforce_relative_strength=False,
        )
        if candidate:
            recent_trigger = candidate
            break

    if recent_trigger:
        trigger_index = recent_trigger["trigger_index"]
        post_rows = rows[trigger_index + 1:]
        closes = [row["close"] for row in rows]
        current_close = rows[-1]["close"]
        ma5 = simple_ma(closes, 5)
        previous_ma5 = simple_ma(closes[:-1], 5)
        ma10 = simple_ma(closes, 10)
        below_mid_count = sum(row["close"] < recent_trigger["trigger_mid"] for row in post_rows)
        two_consecutive_below_mid = (
            len(post_rows) >= 2
            and all(row["close"] < recent_trigger["trigger_mid"] for row in post_rows[-2:])
        )
        recovery_ratio = (
            (current_close - recent_trigger["v_bottom"])
            / (recent_trigger["left_peak"] - recent_trigger["v_bottom"])
            if recent_trigger["left_peak"] > recent_trigger["v_bottom"] else 0
        )
        days_since = current_index - trigger_index

        if (
            current_close < recent_trigger["v_bottom"]
            or two_consecutive_below_mid
            or (days_since >= 3 and current_close < ma5)
        ):
            state = "VX"
        elif recovery_ratio >= 0.50 and current_close >= ma10 and ma5 > previous_ma5:
            state = "V3"
        elif current_close >= recent_trigger["v2_confirm"] and below_mid_count == 0:
            state = "V2"
        else:
            state = "V1"
        recent_trigger["rsi"] = calculate_rsi(closes)
        return _build_result(state, rows, recent_trigger, recent_trigger, chip)

    base_context = _base_context(rows, current_index, for_trigger=False)
    if not base_context:
        return None
    base_context["current_close"] = rows[-1]["close"]
    if not _is_base_ready(base_context, for_trigger=False):
        return None
    return _build_result("V0", rows, base_context, None, chip)
