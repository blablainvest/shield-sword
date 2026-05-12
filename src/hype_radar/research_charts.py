from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from .models import Candle, Ticker
from .token_intelligence import (
    extract_lunarcrush_metrics,
    lunar_endpoint_list,
    social_volume_value,
)

HOUR_MS = 60 * 60 * 1000


def build_research_charts_stage(
    symbol: str,
    ticker: Ticker,
    hourly_candles: Sequence[Candle],
    token_data: Optional[Dict[str, Any]],
    open_interest_rows: Optional[Sequence[Dict[str, Any]]] = None,
    research_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    research_time = _as_utc(research_time or datetime.now(timezone.utc))
    buckets = hourly_buckets(research_time)
    market = normalize_market_series(hourly_candles, buckets)
    lunar_payload = (token_data or {}).get("lunarcrush") if token_data else None
    social = normalize_lunarcrush_series(lunar_payload if isinstance(lunar_payload, dict) else None, buckets)
    oi = normalize_open_interest_series(open_interest_rows or [], buckets)
    scenario = detect_social_market_scenario(market, social, oi)
    metrics = {
        "symbol": symbol,
        "research_time": research_time.isoformat(),
        "window_hours": 24,
        "bucket_ms": HOUR_MS,
        "status": "available" if market["status"] == "available" else "partial",
        "charts": {
            "mentions": {
                "label": "Ticker social mentions",
                "unit": "mentions",
                "status": social["status"],
                "source": social["source"],
                "points": social["points"],
                "reason": social.get("reason"),
            },
            "price_change": {
                "label": "Hourly price change",
                "unit": "pct",
                "status": market["status"],
                "source": "bybit_klines_1h",
                "points": market["price_points"],
            },
            "volume_change": {
                "label": "Hourly volume change",
                "unit": "pct",
                "status": market["status"],
                "source": "bybit_klines_1h",
                "points": market["volume_points"],
            },
        },
        "derivatives": {
            "open_interest": {
                "status": oi["status"],
                "source": "bybit_open_interest_1h" if oi["status"] == "available" else "bybit_ticker_snapshot",
                "points": oi["points"],
                "change_pct": oi["change_pct"],
                "current_value": ticker.open_interest_value or ticker.open_interest,
            },
        },
        "social_context": social["context"],
        "scenario": scenario,
    }
    return {
        "status": "pass" if market["status"] == "available" else "warn",
        "score": scenario.get("score"),
        "reason": scenario.get("conclusion"),
        "metrics": metrics,
        "raw_source": {
            "lunarcrush_status": social["status"],
            "open_interest_status": oi["status"],
            "hourly_candles": len(hourly_candles),
        },
    }


def hourly_buckets(research_time: datetime, hours: int = 24) -> List[int]:
    end_ms = int(_as_utc(research_time).timestamp() * 1000)
    end_hour = end_ms - (end_ms % HOUR_MS)
    return [end_hour - ((hours - 1 - index) * HOUR_MS) for index in range(hours)]


def normalize_market_series(candles: Sequence[Candle], buckets: Sequence[int]) -> Dict[str, Any]:
    by_start = {int(candle.start_ms - (candle.start_ms % HOUR_MS)): candle for candle in candles}
    sorted_candles = sorted(candles, key=lambda candle: candle.start_ms)
    previous_by_start: Dict[int, Candle] = {}
    previous: Optional[Candle] = None
    for candle in sorted_candles:
        start = int(candle.start_ms - (candle.start_ms % HOUR_MS))
        previous_by_start[start] = previous
        previous = candle

    price_points: List[Dict[str, Any]] = []
    volume_points: List[Dict[str, Any]] = []
    for bucket in buckets:
        candle = by_start.get(bucket)
        previous_candle = previous_by_start.get(bucket)
        price_change = None
        volume_change = None
        if candle and previous_candle and previous_candle.close > 0:
            price_change = (candle.close - previous_candle.close) / previous_candle.close
        if candle and previous_candle and previous_candle.turnover > 0:
            volume_change = (candle.turnover - previous_candle.turnover) / previous_candle.turnover
        price_points.append({"time": bucket, "value": price_change, "close": candle.close if candle else None})
        volume_points.append({"time": bucket, "value": volume_change, "turnover": candle.turnover if candle else None})

    available = sum(1 for point in price_points if point["value"] is not None)
    return {
        "status": "available" if available >= 12 else "insufficient_data",
        "price_points": price_points,
        "volume_points": volume_points,
    }


def normalize_lunarcrush_series(payload: Optional[Dict[str, Any]], buckets: Sequence[int]) -> Dict[str, Any]:
    if not payload or payload.get("skipped"):
        return _empty_social_series(buckets, payload.get("reason") if isinstance(payload, dict) else "LunarCrush unavailable.")
    rows = lunar_endpoint_list(payload, "topic_time_series")
    by_bucket: Dict[int, float] = {}
    for row in rows:
        value = social_volume_value(row)
        timestamp = _row_timestamp_ms(row)
        if value is None or timestamp is None:
            continue
        by_bucket[timestamp - (timestamp % HOUR_MS)] = value

    metrics = extract_lunarcrush_metrics(payload)
    context = {
        "sentiment": metrics.get("sentiment"),
        "creators_count": metrics.get("influencers_count"),
        "contributors": metrics.get("social_contributors_24h"),
        "top_posts": metrics.get("top_posts") or [],
        "topic_trend": metrics.get("topic_trend"),
    }
    if not by_bucket:
        return {
            **_empty_social_series(buckets, "LunarCrush did not return hourly topic mentions."),
            "context": context,
        }
    points = [{"time": bucket, "value": by_bucket.get(bucket)} for bucket in buckets]
    available = sum(1 for point in points if point["value"] is not None)
    return {
        "status": "available" if available >= 3 else "insufficient_data",
        "source": "lunarcrush_topic_time_series_hour",
        "points": points,
        "reason": None,
        "context": context,
    }


def normalize_open_interest_series(rows: Sequence[Dict[str, Any]], buckets: Sequence[int]) -> Dict[str, Any]:
    by_bucket: Dict[int, float] = {}
    for row in rows:
        timestamp = _row_timestamp_ms(row)
        value = _first_float(row.get("openInterest"), row.get("open_interest"), row.get("openInterestValue"))
        if timestamp is None or value is None:
            continue
        by_bucket[timestamp - (timestamp % HOUR_MS)] = value
    points = [{"time": bucket, "value": by_bucket.get(bucket)} for bucket in buckets]
    values = [point["value"] for point in points if point["value"] is not None]
    change_pct = (values[-1] - values[0]) / values[0] if len(values) >= 2 and values[0] > 0 else None
    return {
        "status": "available" if len(values) >= 3 else "unavailable",
        "points": points,
        "change_pct": change_pct,
    }


def detect_social_market_scenario(market: Dict[str, Any], social: Dict[str, Any], oi: Dict[str, Any]) -> Dict[str, Any]:
    mentions = _values(social.get("points") or [])
    prices = _values(market.get("price_points") or [])
    volumes = _values(market.get("volume_points") or [])
    sentiment = _first_float((social.get("context") or {}).get("sentiment"))
    creators = _first_float((social.get("context") or {}).get("creators_count"), (social.get("context") or {}).get("contributors"))
    top_posts = (social.get("context") or {}).get("top_posts") or []
    social_run = _longest_increase_run(mentions)
    first_mentions = _first_spike_index(mentions, threshold_ratio=0.35)
    first_price = _first_abs_threshold_index(prices, 0.025)
    first_volume = _first_abs_threshold_index(volumes, 0.50)
    mentions_drop = _drops_after_spike(mentions, first_mentions)
    sentiment_positive = sentiment is not None and sentiment >= 60
    sentiment_unstable = sentiment is None or sentiment < 45
    few_creators = creators is None or creators < 5
    creators_growing = creators is not None and creators >= 5
    price_range_bound = _max_abs(prices) < 0.025
    volume_compresses = _tail_average(volumes, 6) < _head_average(volumes, 6)
    oi_grows = (oi.get("change_pct") or 0.0) > 0.03
    live_narrative = bool(top_posts)

    if social["status"] != "available":
        return {
            "code": "insufficient_social_data",
            "label": "Insufficient social data",
            "score": None,
            "conclusion": "LunarCrush hourly mentions are unavailable; charts show Bybit market data and social status only.",
            "evidence": ["No fake social data was generated."],
        }
    if social_run >= 3 and social_run <= 6 and price_range_bound and volume_compresses and oi_grows and live_narrative:
        return {
            "code": "strong_signal",
            "label": "C strong signal",
            "score": 90,
            "conclusion": "Early social accumulation; add watchlist; wait market setup.",
            "evidence": [
                "Mentions grew for %s consecutive hours." % social_run,
                "Price is still range-bound and volume is compressing.",
                "Bybit derivatives/open interest is growing.",
                "Top posts show a live narrative.",
            ],
        }
    if first_price is not None and (first_mentions is None or first_price < first_mentions) and mentions_drop and sentiment_unstable and few_creators:
        return {
            "code": "fake_pump",
            "label": "B fake pump",
            "score": 25,
            "conclusion": "Attention lagged; people discuss an already happened pump; not early organic signal.",
            "evidence": [
                "Price moved before social mentions.",
                "Mentions lagged and faded after the spike.",
                "Sentiment is unstable and creator breadth is thin.",
            ],
        }
    if first_mentions is not None and first_price is not None and first_mentions < first_price and (first_volume is None or first_price <= first_volume) and sentiment_positive and creators_growing:
        return {
            "code": "organic_growth",
            "label": "A organic growth",
            "score": 75,
            "conclusion": "Social attention leads price; organic social-driven move.",
            "evidence": [
                "Mentions rose before price and volume confirmation.",
                "Sentiment is positive.",
                "Creator breadth is growing.",
            ],
        }
    return {
        "code": "mixed",
        "label": "Mixed / no named scenario",
        "score": 50,
        "conclusion": "Social, price, volume, and open-interest timing do not cleanly match scenarios A, B, or C.",
        "evidence": [
            "mentions_first=%s price_first=%s volume_first=%s social_run=%s" % (first_mentions, first_price, first_volume, social_run)
        ],
    }


def _empty_social_series(buckets: Sequence[int], reason: Optional[str]) -> Dict[str, Any]:
    return {
        "status": "unavailable",
        "source": "lunarcrush",
        "points": [{"time": bucket, "value": None} for bucket in buckets],
        "reason": reason,
        "context": {"sentiment": None, "creators_count": None, "contributors": None, "top_posts": [], "topic_trend": None},
    }


def _row_timestamp_ms(row: Dict[str, Any]) -> Optional[int]:
    value = row.get("time") or row.get("timestamp") or row.get("ts") or row.get("date")
    if value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = int(value)
        return timestamp * 1000 if timestamp < 10_000_000_000 else timestamp
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _values(points: Sequence[Dict[str, Any]]) -> List[Optional[float]]:
    return [_first_float(point.get("value")) for point in points]


def _first_float(*values: Any) -> Optional[float]:
    for value in values:
        try:
            if value in (None, ""):
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_spike_index(values: Sequence[Optional[float]], threshold_ratio: float) -> Optional[int]:
    present = [value for value in values if value is not None]
    if len(present) < 2:
        return None
    baseline = max(1.0, sum(present[: max(1, min(6, len(present)))]) / max(1, min(6, len(present))))
    threshold = baseline * (1.0 + threshold_ratio)
    for index, value in enumerate(values):
        if value is not None and value >= threshold:
            return index
    return None


def _first_abs_threshold_index(values: Sequence[Optional[float]], threshold: float) -> Optional[int]:
    for index, value in enumerate(values):
        if value is not None and abs(value) >= threshold:
            return index
    return None


def _longest_increase_run(values: Sequence[Optional[float]]) -> int:
    longest = 0
    current = 0
    previous: Optional[float] = None
    for value in values:
        if value is None:
            current = 0
            previous = None
            continue
        if previous is not None and value > previous:
            current += 1
        else:
            current = 1
        previous = value
        longest = max(longest, current)
    return longest


def _drops_after_spike(values: Sequence[Optional[float]], spike_index: Optional[int]) -> bool:
    if spike_index is None or spike_index + 3 >= len(values):
        return False
    peak = values[spike_index]
    tail = [value for value in values[spike_index + 2 : spike_index + 5] if value is not None]
    return bool(peak is not None and tail and min(tail) <= peak * 0.70)


def _max_abs(values: Sequence[Optional[float]]) -> float:
    present = [abs(value) for value in values if value is not None]
    return max(present) if present else 0.0


def _tail_average(values: Sequence[Optional[float]], count: int) -> float:
    present = [value for value in values[-count:] if value is not None]
    return sum(present) / len(present) if present else 0.0


def _head_average(values: Sequence[Optional[float]], count: int) -> float:
    present = [value for value in values[:count] if value is not None]
    return sum(present) / len(present) if present else 0.0


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
