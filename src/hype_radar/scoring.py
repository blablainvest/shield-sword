from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

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
from .models import Candidate, Candle, FeatureSet, OrderbookStats, ScoreBreakdown, Ticker, TradePlan


@dataclass
class MarketSnapshot:
    ticker: Ticker
    orderbook: OrderbookStats
    candles: Dict[str, Sequence[Candle]]
    alt_market_return_1h: float = 0.0


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
    manipulation_parts = manipulation_breakdown(features, orderbook, ticker)
    late_entry_parts = late_entry_breakdown(features, ticker)
    manipulation = clamp(sum(item["points"] for item in manipulation_parts))
    late_risk = clamp(sum(item["points"] for item in late_entry_parts))

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
        manipulation_breakdown=manipulation_parts,
        late_entry_breakdown=late_entry_parts,
        scores=scores,
        features=features,
        price_24h_pct=ticker.price_24h_pct,
        turnover_24h=ticker.turnover_24h,
        funding_rate=ticker.funding_rate,
    )


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
    return clamp(sum(item["points"] for item in manipulation_breakdown(features, orderbook, ticker)))


def manipulation_breakdown(features: FeatureSet, orderbook: OrderbookStats, ticker: Ticker) -> List[Dict[str, Any]]:
    spread_points = clamp((orderbook.spread_bps - 8.0) * 2.5, 0.0, 25.0)
    depth_points = clamp((50000.0 - orderbook.depth_total_usdt_50bps) / 2500.0, 0.0, 25.0)
    concentration_points = 20.0 if features.candle_volume_concentration > 0.62 else 8.0 if features.candle_volume_concentration > 0.48 else 0.0
    funding_abs = abs(ticker.funding_rate)
    funding_points = 15.0 if funding_abs > 0.001 else 8.0 if funding_abs > 0.0005 else 0.0
    no_volume_pump_points = 12.0 if features.return_1h > 0.12 and features.volume_growth_1h < 1.5 else 0.0
    missing_oi_points = 10.0 if ticker.open_interest_value <= 0 else 0.0
    return [
        risk_factor("spread", "Спред стакана", spread_points, 25.0, orderbook.spread_bps, "bps", "Широкий спред ухудшает вход/выход и повышает риск проскальзывания."),
        risk_factor("depth_50bps", "Глубина стакана 50 bps", depth_points, 25.0, orderbook.depth_total_usdt_50bps, "USDT", "Малая глубина означает, что цену легче сдвинуть небольшим объемом."),
        risk_factor("volume_concentration", "Концентрация объема", concentration_points, 20.0, features.candle_volume_concentration, "ratio", "Если объем сидит в одной из последних 4 часовых свечей, движение менее устойчиво."),
        risk_factor("funding", "Funding", funding_points, 15.0, funding_abs, "ratio", "Перегретый funding показывает перекос в деривативах."),
        risk_factor("pump_without_volume", "Рост 1ч без роста объема", no_volume_pump_points, 12.0, features.return_1h, "ratio", "Цена выросла сильно, но объем не подтвердил движение."),
        risk_factor("missing_open_interest", "Нет open interest", missing_oi_points, 10.0, ticker.open_interest_value, "USDT", "Без OI хуже видно деривативный контекст."),
    ]


def late_entry_risk(features: FeatureSet, ticker: Ticker) -> float:
    return clamp(sum(item["points"] for item in late_entry_breakdown(features, ticker)))


def late_entry_breakdown(features: FeatureSet, ticker: Ticker) -> List[Dict[str, Any]]:
    funding_abs = abs(ticker.funding_rate)
    return [
        risk_factor("return_24h", "Рост за 24ч", clamp(features.return_24h * 100.0, 0.0, 30.0), 30.0, features.return_24h, "ratio", "Чем сильнее монета уже выросла за сутки, тем выше риск догонять движение."),
        risk_factor("return_4h", "Рост за 4ч", clamp(features.return_4h * 160.0, 0.0, 24.0), 24.0, features.return_4h, "ratio", "Быстрый 4ч разгон повышает риск входа после основной импульсной части."),
        risk_factor("rsi_1h", "RSI 1ч выше 68", clamp((features.rsi_1h - 68.0) * 1.8, 0.0, 22.0), 22.0, features.rsi_1h, "number", "Высокий RSI показывает перегретость краткосрочного движения."),
        risk_factor("atr_distance", "Удаление от VWAP через ATR", clamp((features.atr_distance_1h - 1.8) * 8.0, 0.0, 24.0), 24.0, features.atr_distance_1h, "number", "Чем дальше цена от VWAP в ATR, тем хуже качество входа по текущей цене."),
        risk_factor("funding", "Funding", 12.0 if funding_abs > 0.00075 else 0.0, 12.0, funding_abs, "ratio", "Перегретый funding повышает риск входа после crowded move."),
        risk_factor("volume_declining_on_highs", "Объем снижается на хаях", 10.0 if features.volume_declining_on_highs else 0.0, 10.0, features.volume_declining_on_highs, "bool", "Цена обновляет highs, но объем слабеет: возможное выдыхание импульса."),
    ]


def risk_factor(key: str, label: str, points: float, max_points: float, value: Any, value_type: str, description: str) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "points": round(points, 2),
        "max_points": max_points,
        "value": value,
        "value_type": value_type,
        "active": points > 0,
        "description": description,
    }


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
