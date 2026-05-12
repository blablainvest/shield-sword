from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .indicators import (
    atr,
    clamp,
    failed_breakout,
    pct_return,
    relative_growth,
    rolling_returns,
    rounded_price,
    rsi,
    safe_div,
    structure_breakdown,
    volume_concentration,
    volume_declining_on_highs,
    vwap,
    zscore,
)
from .models import Candidate, Candle, CvdStats, FeatureSet, LongShortRatio, OrderbookStats, ScoreBreakdown, Ticker, TradePlan


@dataclass
class MarketSnapshot:
    ticker: Ticker
    orderbook: OrderbookStats
    candles: Dict[str, Sequence[Candle]]
    alt_market_return_1h: float = 0.0
    long_short_ratio: Optional[LongShortRatio] = None
    cvd: Optional[CvdStats] = None


def build_features(snapshot: MarketSnapshot) -> FeatureSet:
    candles_15m = list(snapshot.candles.get("15") or [])
    candles_1h = list(snapshot.candles.get("60") or [])
    candles_4h = list(snapshot.candles.get("240") or [])
    candles_1d = list(snapshot.candles.get("D") or [])

    close = snapshot.ticker.last_price
    current_atr = atr(candles_1h)
    current_vwap = vwap(candles_1h[-24:]) if candles_1h else close
    return_1h = pct_return(candles_1h, 1)
    hourly_returns = rolling_returns(candles_1h, 1)
    four_hour_returns = rolling_returns(candles_1h, 4)

    return FeatureSet(
        return_15m=pct_return(candles_15m, 1),
        return_1h=return_1h,
        return_4h=pct_return(candles_4h, 1) or pct_return(candles_1h, 4),
        return_24h=snapshot.ticker.price_24h_pct or pct_return(candles_1d, 1),
        z_return_1h=zscore(return_1h, hourly_returns),
        z_return_4h=zscore(pct_return(candles_1h, 4), four_hour_returns),
        volume_growth_1h=relative_growth(
            candles_1h[-1].volume if candles_1h else 0.0,
            [candle.volume for candle in candles_1h[-169:-1]],
        ),
        turnover_growth_1h=relative_growth(
            candles_1h[-1].turnover if candles_1h else 0.0,
            [candle.turnover for candle in candles_1h[-169:-1]],
        ),
        candle_volume_concentration=volume_concentration(candles_1h, 4),
        rsi_1h=rsi(candles_1h),
        atr_pct_1h=safe_div(current_atr, close) if close > 0 else 0.0,
        atr_distance_1h=safe_div(close - current_vwap, current_atr) if current_atr > 0 else 0.0,
        vwap_distance_pct_1h=safe_div(close - current_vwap, current_vwap) if current_vwap > 0 else 0.0,
        volume_declining_on_highs=volume_declining_on_highs(candles_1h),
        failed_breakout=failed_breakout(candles_1h),
        structure_breakdown=structure_breakdown(candles_1h),
    )


def score_snapshot(snapshot: MarketSnapshot) -> Candidate:
    features = build_features(snapshot)
    ticker = snapshot.ticker
    orderbook = snapshot.orderbook
    technical_analysis = build_technical_analysis(snapshot.candles)

    market_anomaly = clamp(
        8.0 * max(features.z_return_1h, 0.0)
        + 4.0 * max(features.z_return_4h, 0.0)
        + 180.0 * max(features.return_1h, 0.0)
        + 80.0 * max(features.return_4h, 0.0),
        0.0,
        100.0,
    )
    volume_quality = clamp(18.0 * min(features.volume_growth_1h, 4.0) + 8.0 * min(features.turnover_growth_1h, 3.0), 0.0, 100.0)
    liquidity = liquidity_score(orderbook)
    derivatives_health = derivatives_score(ticker)
    ta_long = ta_long_score(features)
    ta_short = ta_short_score(features)
    relative_strength = clamp(50.0 + ((features.return_1h - snapshot.alt_market_return_1h) * 500.0), 0.0, 100.0)
    manipulation = manipulation_score(features, orderbook, ticker)
    late_risk = late_entry_risk(features, ticker)

    scores = ScoreBreakdown(
        market_anomaly=market_anomaly,
        volume_quality=volume_quality,
        liquidity=liquidity,
        derivatives_health=derivatives_health,
        catalyst_freshness=50.0,
        social_quality=50.0,
        ta_long=ta_long,
        ta_short=ta_short,
        relative_strength=relative_strength,
        manipulation_penalty=manipulation,
        late_entry_penalty=late_risk,
    )

    base_score = (
        0.20 * market_anomaly
        + 0.15 * volume_quality
        + 0.15 * liquidity
        + 0.10 * derivatives_health
        + 0.10 * scores.catalyst_freshness
        + 0.10 * scores.social_quality
        + 0.05 * relative_strength
    )
    long_score = clamp(base_score + 0.15 * ta_long - 0.30 * manipulation - 0.35 * late_risk)
    raw_short_score = (
        0.20 * late_risk
        + 0.18 * ta_short
        + 0.15 * market_anomaly
        + 0.12 * liquidity
        + 0.10 * volume_quality
        + 0.10 * max(0.0, 100.0 - derivatives_health)
        + 0.15 * min(manipulation, 85.0)
    )
    if not exhaustion_confirmed(features):
        raw_short_score -= 12.0 * max(features.z_return_1h, 0.0)
    short_score = clamp(raw_short_score)

    lifecycle = lifecycle_stage(features, late_risk)
    direction_bias, verdict, rank_bucket, reason = verdict_for(long_score, short_score, manipulation, late_risk, features, lifecycle)
    opportunity = max(long_score, short_score)
    confidence = confidence_score(snapshot, scores, features)
    trade_plan = make_trade_plan(direction_bias, verdict, snapshot, features)
    hype_cause = infer_hype_causes(features, manipulation)
    strategy_identifier = select_strategy_identifier(ticker, features, technical_analysis, direction_bias, verdict, snapshot.long_short_ratio)
    technical_analysis = enrich_strategy_context(technical_analysis, ticker, snapshot.long_short_ratio, snapshot.cvd, strategy_identifier, trade_plan)

    return Candidate(
        symbol=ticker.symbol,
        direction_bias=direction_bias,
        verdict=verdict,
        rank_bucket=rank_bucket,
        long_score=round(long_score, 2),
        short_score=round(short_score, 2),
        opportunity_score=round(opportunity, 2),
        manipulation_score=round(manipulation, 2),
        late_entry_risk=round(late_risk, 2),
        confidence=round(confidence, 2),
        theme_lifecycle_stage=lifecycle,
        hype_cause=hype_cause,
        reason_summary=reason,
        trade_plan=trade_plan,
        scores=scores,
        features=features,
        price_24h_pct=ticker.price_24h_pct,
        turnover_24h=ticker.turnover_24h,
        funding_rate=ticker.funding_rate,
        strategy_identifier=strategy_identifier,
        technical_analysis=technical_analysis,
    )


def build_technical_analysis(candles: Dict[str, Sequence[Candle]]) -> Dict[str, object]:
    candles_1h = list(candles.get("60") or [])
    candles_1d = list(candles.get("D") or [])
    signals = {
        "breakout_20d_high": _breakout_20d_high(candles_1d),
        "atr_volatility_expansion": _atr_volatility_expansion(candles_1h),
        "rsi_signal": _rsi_signal(candles_1h),
        "rsi_divergence": _rsi_divergence(candles_1h),
        "ema_cross": _ema_cross(candles_1h),
        "volume_spike": _volume_spike(candles_1h),
        "bollinger_squeeze": _bollinger_squeeze(candles_1h),
        "structure_break_hh_hl": _structure_break_hh_hl(candles_1h),
    }
    available = sum(1 for signal in signals.values() if signal["status"] == "available")
    status = "available" if available >= 4 else "partial" if available else "insufficient_data"
    return {
        "status": status,
        "principle": "derivatives_and_market_metrics_define_what_to_trade;technical_analysis_defines_when_and_where",
        "timeframes": {"primary": "60", "breakout": "D"},
        "signals": signals,
    }


def select_strategy_identifier(
    ticker: Ticker,
    features: FeatureSet,
    technical_analysis: Dict[str, object],
    direction_bias: str,
    verdict: str,
    long_short_ratio: Optional[LongShortRatio] = None,
) -> str:
    signals = technical_analysis.get("signals") if isinstance(technical_analysis, dict) else {}
    if not isinstance(signals, dict):
        return "unknown"

    breakout = _signal_value(signals, "breakout_20d_high") is True
    atr_expansion = _signal_value(signals, "atr_volatility_expansion") is True
    volume_spike_signal = _signal_value(signals, "volume_spike") is True
    squeeze = _signal_value(signals, "bollinger_squeeze") is True
    structure = _signal_value(signals, "structure_break_hh_hl")
    ema_cross = _signal_value(signals, "ema_cross")
    rsi_signal = _signal_value(signals, "rsi_signal")

    long_ratio = long_short_ratio.long_ratio if long_short_ratio else None
    short_ratio = long_short_ratio.short_ratio if long_short_ratio else None
    long_crowded = long_ratio is not None and long_ratio >= 0.62
    short_crowded = short_ratio is not None and short_ratio >= 0.62

    if ticker.funding_rate <= -0.0005 and ticker.price_24h_pct > 0.06 and (breakout or volume_spike_signal or atr_expansion):
        return "short_squeeze_model"
    if ticker.price_24h_pct < -0.08 and (structure == "bearish_break" or volume_spike_signal):
        return "oi_flush_model"
    if ticker.funding_rate >= 0.001 and (rsi_signal == "overbought" or long_crowded):
        return "mean_reversion_extreme_funding"
    if ticker.funding_rate <= -0.001 and (rsi_signal == "oversold" or short_crowded):
        return "mean_reversion_extreme_funding"
    if (squeeze or breakout) and atr_expansion and ema_cross in {"bullish_cross", "bullish"}:
        return "volatility_breakout_squeeze"
    if features.failed_breakout or structure in {"bullish_sweep_reclaim", "bearish_break"} or verdict in {"SHORT_ENTER", "SHORT_WATCH"}:
        return "liquidity_sweep_strategy"
    if direction_bias == "NEUTRAL" or verdict in {"WATCH_ONLY", "AVOID"}:
        return "unknown"
    return "unknown"


def enrich_strategy_context(
    technical_analysis: Dict[str, object],
    ticker: Ticker,
    long_short_ratio: Optional[LongShortRatio],
    cvd: Optional[CvdStats],
    strategy_identifier: str,
    trade_plan: TradePlan,
) -> Dict[str, object]:
    enriched = dict(technical_analysis)
    long_ratio = long_short_ratio.long_ratio if long_short_ratio else None
    short_ratio = long_short_ratio.short_ratio if long_short_ratio else None
    long_short_available = long_ratio is not None and short_ratio is not None
    cvd_available = cvd is not None and cvd.trade_count > 0
    derivatives_status = "available" if long_short_available and cvd_available else "partial"
    enriched["derivatives_filter"] = {
        "status": derivatives_status,
        "principle": "bybit_derivatives_metrics_define_what_to_trade",
        "metrics": {
            "funding_rate": ticker.funding_rate,
            "open_interest": ticker.open_interest,
            "open_interest_value": ticker.open_interest_value,
            "long_ratio": long_ratio,
            "short_ratio": short_ratio,
            "long_short_ratio_status": "available" if long_short_available else "unavailable",
            "long_short_timestamp_ms": long_short_ratio.timestamp_ms if long_short_ratio else None,
            "cvd": _cvd_payload(cvd),
        },
    }
    enriched["strategy_identifier"] = strategy_identifier
    enriched["strategy_models"] = {
        "selected": strategy_identifier,
        "supported": [
            "mean_reversion_extreme_funding",
            "short_squeeze_model",
            "oi_flush_model",
            "volatility_breakout_squeeze",
            "liquidity_sweep_strategy",
            "unknown",
        ],
    }
    enriched["execution_context"] = {
        "entry_basis": "TA confirmation signals define when/where to enter; do not use derivatives metrics alone as an entry trigger.",
        "stop_loss_basis": "ATR/recent structure from trade_plan.stop_loss and trade_plan.invalidation.",
        "take_profit_basis": "Structure/liquidity-based targets approximated by trade_plan.take_profit_1/2/3.",
        "trade_plan": trade_plan.to_dict() if hasattr(trade_plan, "to_dict") else {
            "entry": trade_plan.entry,
            "safer_entry": trade_plan.safer_entry,
            "invalidation": trade_plan.invalidation,
            "stop_loss": trade_plan.stop_loss,
            "take_profit_1": trade_plan.take_profit_1,
            "take_profit_2": trade_plan.take_profit_2,
            "take_profit_3": trade_plan.take_profit_3,
            "risk_reward": trade_plan.risk_reward,
            "risk_note": trade_plan.risk_note,
        },
    }
    return enriched


def _cvd_payload(cvd: Optional[CvdStats]) -> Dict[str, object]:
    if cvd is None or cvd.trade_count <= 0:
        return {
            "status": "unavailable",
            "source": "bybit_recent_trade",
            "reason": "Bybit does not expose a ready-made historical CVD metric here; the pipeline computes recent-trade CVD when public recent trades are available.",
        }
    return {
        "status": "available",
        "source": "bybit_recent_trade",
        "cvd_base": cvd.cvd_base,
        "buy_volume_base": cvd.buy_volume_base,
        "sell_volume_base": cvd.sell_volume_base,
        "trade_count": cvd.trade_count,
        "first_timestamp_ms": cvd.first_timestamp_ms,
        "last_timestamp_ms": cvd.last_timestamp_ms,
    }


def _signal_value(signals: Dict[str, object], name: str) -> object:
    signal = signals.get(name)
    if isinstance(signal, dict):
        return signal.get("value")
    return None


def _available(value: object, detail: Dict[str, object] | None = None) -> Dict[str, object]:
    payload: Dict[str, object] = {"status": "available", "value": value}
    if detail:
        payload.update(detail)
    return payload


def _insufficient(required: int, actual: int) -> Dict[str, object]:
    return {"status": "insufficient_data", "value": None, "required_candles": required, "actual_candles": actual}


def _breakout_20d_high(candles: Sequence[Candle]) -> Dict[str, object]:
    if len(candles) < 21:
        return _insufficient(21, len(candles))
    prior_high = max(candle.high for candle in candles[-21:-1])
    latest = candles[-1]
    return _available(latest.close > prior_high, {"prior_20d_high": rounded_price(prior_high), "close": rounded_price(latest.close)})


def _atr_volatility_expansion(candles: Sequence[Candle]) -> Dict[str, object]:
    period = 14
    if len(candles) < period * 2 + 1:
        return _insufficient(period * 2 + 1, len(candles))
    current = atr(candles, period)
    previous_ranges = _true_ranges(candles[-(period * 2 + 1) : -period])
    baseline = _avg(previous_ranges)
    ratio = safe_div(current, baseline, 0.0)
    return _available(ratio >= 1.35, {"atr": current, "baseline_atr": baseline, "expansion_ratio": round(ratio, 4)})


def _rsi_signal(candles: Sequence[Candle]) -> Dict[str, object]:
    if len(candles) < 15:
        return _insufficient(15, len(candles))
    value = rsi(candles)
    if value >= 72.0:
        signal = "overbought"
    elif value <= 30.0:
        signal = "oversold"
    elif value >= 55.0:
        signal = "bullish"
    elif value <= 45.0:
        signal = "bearish"
    else:
        signal = "neutral"
    return _available(signal, {"rsi_1h": round(value, 4)})


def _rsi_divergence(candles: Sequence[Candle]) -> Dict[str, object]:
    if len(candles) < 35:
        return _insufficient(35, len(candles))
    current_rsi = rsi(candles[-15:])
    prior_window = candles[-35:-15]
    prior_rsi = rsi(prior_window)
    latest_close = candles[-1].close
    prior_high = max(candle.close for candle in prior_window)
    prior_low = min(candle.close for candle in prior_window)
    if latest_close > prior_high and current_rsi < prior_rsi - 5.0:
        value = "bearish"
    elif latest_close < prior_low and current_rsi > prior_rsi + 5.0:
        value = "bullish"
    else:
        value = "none"
    return _available(value, {"current_rsi": round(current_rsi, 4), "prior_rsi": round(prior_rsi, 4)})


def _ema_cross(candles: Sequence[Candle]) -> Dict[str, object]:
    if len(candles) < 35:
        return _insufficient(35, len(candles))
    closes = [candle.close for candle in candles]
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    previous_delta = ema12[-2] - ema26[-2]
    current_delta = ema12[-1] - ema26[-1]
    if previous_delta <= 0 < current_delta:
        value = "bullish_cross"
    elif previous_delta >= 0 > current_delta:
        value = "bearish_cross"
    elif current_delta > 0:
        value = "bullish"
    elif current_delta < 0:
        value = "bearish"
    else:
        value = "none"
    return _available(value, {"ema_12": rounded_price(ema12[-1]), "ema_26": rounded_price(ema26[-1])})


def _volume_spike(candles: Sequence[Candle]) -> Dict[str, object]:
    if len(candles) < 21:
        return _insufficient(21, len(candles))
    baseline = _avg([candle.volume for candle in candles[-21:-1]])
    ratio = safe_div(candles[-1].volume, baseline, 0.0)
    return _available(ratio >= 2.0, {"volume_ratio": round(ratio, 4)})


def _bollinger_squeeze(candles: Sequence[Candle]) -> Dict[str, object]:
    if len(candles) < 40:
        return _insufficient(40, len(candles))
    current_width = _bollinger_width([candle.close for candle in candles[-20:]])
    previous_widths = [_bollinger_width([candle.close for candle in candles[index - 20 : index]]) for index in range(len(candles) - 19, len(candles))]
    baseline = _avg([width for width in previous_widths if width > 0])
    ratio = safe_div(current_width, baseline, 0.0)
    return _available(current_width <= 0.08 and ratio <= 0.75, {"band_width": round(current_width, 4), "width_ratio": round(ratio, 4)})


def _structure_break_hh_hl(candles: Sequence[Candle]) -> Dict[str, object]:
    if len(candles) < 12:
        return _insufficient(12, len(candles))
    prior = candles[-12:-1]
    recent = candles[-6:]
    latest = candles[-1]
    prior_high = max(candle.high for candle in prior)
    prior_low = min(candle.low for candle in prior)
    higher_lows = min(candle.low for candle in recent[-3:]) > min(candle.low for candle in recent[:3])
    if latest.close > prior_high and higher_lows:
        value = "bullish_hh_hl"
    elif latest.low < prior_low and latest.close > prior_low:
        value = "bullish_sweep_reclaim"
    elif latest.close < prior_low:
        value = "bearish_break"
    else:
        value = "none"
    return _available(value, {"prior_high": rounded_price(prior_high), "prior_low": rounded_price(prior_low)})


def _true_ranges(candles: Sequence[Candle]) -> List[float]:
    ranges: List[float] = []
    for previous, current in zip(candles, candles[1:]):
        ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
    return ranges


def _ema_series(values: Sequence[float], period: int) -> List[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    series = [values[0]]
    for value in values[1:]:
        series.append(value * alpha + series[-1] * (1.0 - alpha))
    return series


def _bollinger_width(values: Sequence[float]) -> float:
    middle = _avg(values)
    if middle <= 0:
        return 0.0
    variance = _avg([(value - middle) ** 2 for value in values])
    stdev = variance ** 0.5
    return safe_div(4.0 * stdev, middle, 0.0)


def _avg(values: Sequence[float]) -> float:
    clean = list(values)
    return sum(clean) / len(clean) if clean else 0.0


def liquidity_score(orderbook: OrderbookStats) -> float:
    spread_component = clamp(100.0 - orderbook.spread_bps * 3.0)
    depth_component = clamp(orderbook.depth_total_usdt_50bps / 2500.0)
    balance = safe_div(min(orderbook.depth_bid_usdt_50bps, orderbook.depth_ask_usdt_50bps), max(orderbook.depth_bid_usdt_50bps, orderbook.depth_ask_usdt_50bps), 0.0)
    return clamp(0.45 * spread_component + 0.45 * depth_component + 10.0 * balance)


def derivatives_score(ticker: Ticker) -> float:
    funding_abs = abs(ticker.funding_rate)
    funding_component = clamp(100.0 - funding_abs * 400000.0)
    oi_component = clamp(ticker.open_interest_value / 100000.0)
    return clamp(0.75 * funding_component + 0.25 * oi_component)


def ta_long_score(features: FeatureSet) -> float:
    score = 50.0
    score += clamp(features.return_1h * 450.0, -30.0, 30.0)
    score += clamp(features.return_4h * 180.0, -20.0, 20.0)
    score += 12.0 if 50.0 <= features.rsi_1h <= 72.0 else -8.0 if features.rsi_1h > 82.0 else 0.0
    score += 10.0 if 0.0 <= features.atr_distance_1h <= 2.2 else -18.0 if features.atr_distance_1h > 3.5 else 0.0
    score -= 15.0 if features.failed_breakout or features.structure_breakdown else 0.0
    return clamp(score)


def ta_short_score(features: FeatureSet) -> float:
    score = 35.0
    score += 18.0 if features.rsi_1h >= 72.0 else 0.0
    score += 16.0 if features.atr_distance_1h >= 2.5 else 0.0
    score += 18.0 if features.failed_breakout else 0.0
    score += 12.0 if features.volume_declining_on_highs else 0.0
    score += 12.0 if features.structure_breakdown else 0.0
    score += clamp(features.return_24h * 60.0, 0.0, 18.0)
    return clamp(score)


def manipulation_score(features: FeatureSet, orderbook: OrderbookStats, ticker: Ticker) -> float:
    score = 0.0
    score += clamp((orderbook.spread_bps - 8.0) * 2.5, 0.0, 25.0)
    score += clamp((50000.0 - orderbook.depth_total_usdt_50bps) / 2500.0, 0.0, 25.0)
    score += 20.0 if features.candle_volume_concentration > 0.62 else 8.0 if features.candle_volume_concentration > 0.48 else 0.0
    score += 15.0 if abs(ticker.funding_rate) > 0.001 else 8.0 if abs(ticker.funding_rate) > 0.0005 else 0.0
    score += 12.0 if features.return_1h > 0.12 and features.volume_growth_1h < 1.5 else 0.0
    score += 10.0 if ticker.open_interest_value <= 0 else 0.0
    return clamp(score)


def late_entry_risk(features: FeatureSet, ticker: Ticker) -> float:
    score = 0.0
    score += clamp(features.return_24h * 100.0, 0.0, 30.0)
    score += clamp(features.return_4h * 160.0, 0.0, 24.0)
    score += clamp((features.rsi_1h - 68.0) * 1.8, 0.0, 22.0)
    score += clamp((features.atr_distance_1h - 1.8) * 8.0, 0.0, 24.0)
    score += 12.0 if abs(ticker.funding_rate) > 0.00075 else 0.0
    score += 10.0 if features.volume_declining_on_highs else 0.0
    return clamp(score)


def exhaustion_confirmed(features: FeatureSet) -> bool:
    return (
        features.failed_breakout
        or features.structure_breakdown
        or (features.volume_declining_on_highs and features.rsi_1h > 70.0)
        or (features.atr_distance_1h > 3.0 and features.rsi_1h > 76.0)
    )


def lifecycle_stage(features: FeatureSet, late_risk: float) -> str:
    if features.structure_breakdown or (late_risk > 75 and exhaustion_confirmed(features)):
        return "distribution"
    if late_risk > 62 or exhaustion_confirmed(features):
        return "exhaustion"
    if features.return_24h > 0.25 or late_risk > 45:
        return "mainstream_hype"
    if features.z_return_1h > 1.2 or features.volume_growth_1h > 2.0:
        return "acceleration"
    return "early_discovery"


def verdict_for(
    long_score: float,
    short_score: float,
    manipulation: float,
    late_risk: float,
    features: FeatureSet,
    lifecycle: str,
) -> tuple:
    if manipulation > 82.0:
        return ("NEUTRAL", "AVOID", "top_short_watch", "Avoid: manipulation/liquidity risk is too high.")

    if short_score >= 72.0 and exhaustion_confirmed(features):
        return ("SHORT", "SHORT_ENTER", "top_short_watch", "Exhaustion confirmed; short setup is actionable if liquidity holds.")
    if short_score >= 58.0 and lifecycle in {"exhaustion", "distribution", "mainstream_hype"}:
        return ("SHORT", "SHORT_WATCH", "top_short_watch", "Potential is extended; wait for failed retest or structure break before short.")

    if long_score >= 75.0 and manipulation < 45.0 and late_risk < 62.0:
        return ("LONG", "LONG_ENTER", "top_long", "Fresh anomaly with tradable liquidity and continuation confirmation.")
    if long_score >= 58.0 and manipulation < 60.0:
        return ("LONG", "LONG_WAIT_PULLBACK", "top_long", "Constructive long candidate, but entry needs pullback or confirmation.")

    return ("NEUTRAL", "WATCH_ONLY", "top_long", "Watch only: edge is not strong enough for a trade plan.")


def confidence_score(snapshot: MarketSnapshot, scores: ScoreBreakdown, features: FeatureSet) -> float:
    data_quality = 0.0
    data_quality += 20.0 if len(snapshot.candles.get("60") or []) >= 120 else 8.0
    data_quality += 12.0 if len(snapshot.candles.get("15") or []) >= 40 else 4.0
    data_quality += 12.0 if snapshot.orderbook.depth_total_usdt_50bps > 25000 else 4.0
    data_quality += 8.0 if snapshot.ticker.turnover_24h > 5000000 else 4.0
    signal_quality = 0.25 * scores.liquidity + 0.20 * scores.volume_quality + 0.20 * scores.market_anomaly + 0.15 * max(scores.ta_long, scores.ta_short)
    penalty = 0.20 * scores.manipulation_penalty
    return clamp(data_quality + signal_quality - penalty)


def make_trade_plan(direction: str, verdict: str, snapshot: MarketSnapshot, features: FeatureSet) -> TradePlan:
    price = snapshot.ticker.last_price
    candles_1h = list(snapshot.candles.get("60") or [])
    current_atr = atr(candles_1h) or price * 0.03
    if price <= 0 or verdict in {"AVOID", "WATCH_ONLY"}:
        return TradePlan(risk_note="No actionable trade: score/R:R/liquidity conditions are not met.")

    if direction == "LONG":
        recent_low = min((candle.low for candle in candles_1h[-6:]), default=price - current_atr)
        stop = min(price - 1.25 * current_atr, recent_low * 0.995)
        risk = max(price - stop, price * 0.002)
        safer_entry = max(price - 0.8 * current_atr, stop + risk * 0.6)
        return TradePlan(
            entry=rounded_price(price),
            safer_entry=rounded_price(safer_entry),
            invalidation=rounded_price(recent_low),
            stop_loss=rounded_price(stop),
            take_profit_1=rounded_price(price + risk),
            take_profit_2=rounded_price(price + 2.0 * risk),
            take_profit_3=rounded_price(price + 3.0 * risk),
            risk_reward=3.0,
            risk_note="Read-only long plan; cancel if spread widens, depth drops, or price loses the retest level.",
        )

    if direction == "SHORT":
        recent_high = max((candle.high for candle in candles_1h[-6:]), default=price + current_atr)
        stop = max(price + 1.25 * current_atr, recent_high * 1.005)
        risk = max(stop - price, price * 0.002)
        safer_entry = min(price + 0.6 * current_atr, stop - risk * 0.6)
        return TradePlan(
            entry=rounded_price(price),
            safer_entry=rounded_price(safer_entry),
            invalidation=rounded_price(recent_high),
            stop_loss=rounded_price(stop),
            take_profit_1=rounded_price(price - risk),
            take_profit_2=rounded_price(price - 2.0 * risk),
            take_profit_3=rounded_price(price - 3.0 * risk),
            risk_reward=3.0,
            risk_note="Read-only short plan; do not short early momentum without failed retest/structure confirmation.",
        )

    return TradePlan(risk_note="No actionable direction.")


def infer_hype_causes(features: FeatureSet, manipulation: float) -> List[str]:
    causes: List[str] = []
    if features.z_return_1h > 1.5:
        causes.append("market_anomaly")
    if features.volume_growth_1h > 2.0:
        causes.append("volume_spike")
    if manipulation > 55.0:
        causes.append("manipulative")
    if features.return_24h > 0.18:
        causes.append("mainstream_hype")
    return causes or ["market_watch"]
