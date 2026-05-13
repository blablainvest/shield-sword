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
DEFAULT_WINDOW_HOURS = 48
FALLBACK_WINDOW_HOURS = 24


def build_research_charts_stage(
    symbol: str,
    ticker: Ticker,
    hourly_candles: Sequence[Candle],
    token_data: Optional[Dict[str, Any]],
    open_interest_rows: Optional[Sequence[Dict[str, Any]]] = None,
    research_time: Optional[datetime] = None,
) -> Dict[str, Any]:
    research_time = _as_utc(research_time or datetime.now(timezone.utc))
    buckets = _selected_buckets(research_time, hourly_candles, token_data)
    market = normalize_market_series(hourly_candles, buckets)
    lunar_payload = (token_data or {}).get("lunarcrush") if token_data else None
    social = normalize_lunarcrush_series(lunar_payload if isinstance(lunar_payload, dict) else None, buckets)
    oi = normalize_open_interest_series(open_interest_rows or [], buckets)
    events = detect_market_social_events(market, social, oi)
    scenario = detect_social_market_scenario(market, social, oi, events)
    indexed_points = indexed_overlay_points(social["points"], market["price_points"], market["volume_points"])
    metrics = {
        "symbol": symbol,
        "research_time": research_time.isoformat(),
        "window_hours": len(buckets),
        "bucket_ms": HOUR_MS,
        "coverage_status": _coverage_status(len(buckets), market, social),
        "status": "available" if market["status"] == "available" else "partial",
        "raw_points": {
            "mentions": social["points"],
            "price": market["price_points"],
            "volume": market["volume_points"],
        },
        "indexed_points": indexed_points,
        "events": events,
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
            "window_hours": len(buckets),
        },
    }


def hourly_buckets(research_time: datetime, hours: int = DEFAULT_WINDOW_HOURS) -> List[int]:
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
        "status": "available" if available >= max(12, len(buckets) // 2) else "insufficient_data",
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
        "status": "available" if available >= max(3, min(8, len(buckets) // 6)) else "insufficient_data",
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


def indexed_overlay_points(
    mentions_points: Sequence[Dict[str, Any]],
    price_points: Sequence[Dict[str, Any]],
    volume_points: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    mentions_index = _scale_0_100(_values(mentions_points))
    price_index = _scale_0_100([_first_float(point.get("close")) for point in price_points])
    volume_index = _scale_0_100([_first_float(point.get("turnover")) for point in volume_points])
    points: List[Dict[str, Any]] = []
    for index, point in enumerate(mentions_points):
        points.append(
            {
                "time": point.get("time"),
                "mentions": mentions_index[index] if index < len(mentions_index) else None,
                "price": price_index[index] if index < len(price_index) else None,
                "volume": volume_index[index] if index < len(volume_index) else None,
            }
        )
    return points


def detect_market_social_events(market: Dict[str, Any], social: Dict[str, Any], oi: Dict[str, Any]) -> Dict[str, Any]:
    mentions = _values(social.get("points") or [])
    closes = [_first_float(point.get("close")) for point in market.get("price_points") or []]
    price_changes = _values(market.get("price_points") or [])
    turnovers = [_first_float(point.get("turnover")) for point in market.get("volume_points") or []]
    volume_changes = _values(market.get("volume_points") or [])
    oi_values = _values(oi.get("points") or [])
    return {
        "mentions_event": _event_payload("mentions", _first_mentions_event_index(mentions), social.get("points") or [], mentions),
        "price_event": _event_payload("price", _first_price_event_index(closes, price_changes), market.get("price_points") or [], closes, price_changes),
        "volume_event": _event_payload("volume", _first_volume_event_index(turnovers, volume_changes), market.get("volume_points") or [], turnovers, volume_changes),
        "oi_event": _event_payload("open_interest", _first_growth_event_index(oi_values, 0.03, 12), oi.get("points") or [], oi_values),
    }


def detect_social_market_scenario(
    market: Dict[str, Any],
    social: Dict[str, Any],
    oi: Dict[str, Any],
    events: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    events = events or detect_market_social_events(market, social, oi)
    mentions = _values(social.get("points") or [])
    prices = _values(market.get("price_points") or [])
    volumes = _values(market.get("volume_points") or [])
    first_mentions = _event_index(events.get("mentions_event"))
    first_price = _event_index(events.get("price_event"))
    first_volume = _event_index(events.get("volume_event"))
    social_run = _longest_increase_run(mentions)
    mentions_drop = _drops_after_spike(mentions, first_mentions)
    price_momentum_fades = _momentum_fades(prices, first_price)
    volume_momentum_fades = _momentum_fades(volumes, first_volume)
    volume_confirms_price = _within_after(first_price, first_volume, 0, 6)
    volume_confirms_after_social = _within_after(first_mentions, first_volume, 1, 12)
    price_confirms_after_social = _within_after(first_mentions, first_price, 1, 12)
    oi_event = _event_index(events.get("oi_event"))

    if social["status"] != "available":
        return {
            "code": "insufficient_social_data",
            "label": "Insufficient social data",
            "score": None,
            "conclusion": "LunarCrush hourly mentions are unavailable; charts show Bybit market data and social status only.",
            "evidence": ["No fake social data was generated."],
        }
    if market.get("status") != "available":
        return {
            "code": "insufficient_market_data",
            "label": "Insufficient market data",
            "score": None,
            "conclusion": "Bybit hourly price/volume coverage is too thin for timing classification.",
            "evidence": ["Market points are kept raw; no synthetic price or volume values were generated."],
        }
    if first_mentions is not None and first_price is None and first_volume is None and social_run >= 3:
        return {
            "code": "early_narrative",
            "label": "Early Narrative",
            "score": 80,
            "conclusion": "Mentions are building before price and volume confirmation.",
            "evidence": [
                "Mentions event fired before market confirmation.",
                "Mentions grew for %s consecutive hours." % social_run,
                "Price and turnover have not confirmed yet.",
            ],
        }
    if first_mentions is not None and first_price is not None and first_mentions < first_price and price_confirms_after_social and volume_confirms_after_social:
        return {
            "code": "narrative",
            "label": "Narrative",
            "score": 75,
            "conclusion": "Mentions led the move; price and turnover confirmed within the timing window.",
            "evidence": [
                "Mentions event came before price and volume.",
                "Price confirmed %sh after mentions." % (first_price - first_mentions),
                "Volume confirmed %sh after mentions." % (first_volume - first_mentions),
            ],
        }
    if (
        first_price is not None
        and first_mentions is not None
        and first_price < first_mentions
        and first_mentions - first_price > 6
        and price_momentum_fades
        and volume_momentum_fades
    ):
        return {
            "code": "exhaustion_late_hype",
            "label": "Exhaustion / Late Hype",
            "score": 30,
            "conclusion": "Social attention arrived after the move while price/volume momentum faded.",
            "evidence": [
                "Price event came before mentions.",
                "Mentions arrived after market momentum.",
                "Recent price and volume momentum faded after the event.",
            ],
        }
    if first_price is not None and first_mentions is not None and first_price < first_mentions and volume_confirms_price:
        return {
            "code": "insider_pump",
            "label": "Insider Pump",
            "score": 65,
            "conclusion": "Price moved before social attention, and turnover confirmed the move.",
            "evidence": [
                "Price event came before mentions.",
                "Volume confirmed within %sh of price." % (first_volume - first_price),
                "Open interest event is %s." % ("present" if oi_event is not None else "not confirmed"),
            ],
        }
    if first_price is not None and (first_mentions is None or first_price < first_mentions) and not volume_confirms_price:
        return {
            "code": "fake_pump",
            "label": "Fake Pump",
            "score": 25,
            "conclusion": "Price led the story, but volume did not confirm the move.",
            "evidence": [
                "Price event came before social attention.",
                "No strong turnover confirmation within 6 hours.",
                "Mentions%s after the price move." % (" faded" if mentions_drop else " lagged"),
            ],
        }
    return {
        "code": "mixed",
        "label": "Mixed",
        "score": 50,
        "conclusion": "Social, price, volume, and open-interest timing do not cleanly match a named scenario.",
        "evidence": [
            "mentions_first=%s price_first=%s volume_first=%s social_run=%s" % (first_mentions, first_price, first_volume, social_run)
        ],
    }


def _selected_buckets(
    research_time: datetime,
    hourly_candles: Sequence[Candle],
    token_data: Optional[Dict[str, Any]],
) -> List[int]:
    buckets_48 = hourly_buckets(research_time, DEFAULT_WINDOW_HOURS)
    market_48 = normalize_market_series(hourly_candles, buckets_48)
    lunar_payload = (token_data or {}).get("lunarcrush") if token_data else None
    social_48 = normalize_lunarcrush_series(lunar_payload if isinstance(lunar_payload, dict) else None, buckets_48)
    market_points = sum(1 for point in market_48["price_points"] if point["value"] is not None)
    social_points = sum(1 for point in social_48["points"] if point["value"] is not None)
    if market_points >= 24 and (social_48["status"] == "available" or social_points >= 6):
        return buckets_48
    return hourly_buckets(research_time, FALLBACK_WINDOW_HOURS)


def _coverage_status(window_hours: int, market: Dict[str, Any], social: Dict[str, Any]) -> str:
    if window_hours == DEFAULT_WINDOW_HOURS and market["status"] == "available" and social["status"] == "available":
        return "full_48h"
    if window_hours == FALLBACK_WINDOW_HOURS:
        return "fallback_24h"
    return "partial"


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


def _event_index(event: Any) -> Optional[int]:
    if isinstance(event, dict):
        return _first_int(event.get("index"))
    return None


def _first_int(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_mentions_event_index(values: Sequence[Optional[float]]) -> Optional[int]:
    present = [value for value in values if value is not None]
    if len(present) < 3:
        return None
    baseline_count = max(3, min(12, len(present) // 3))
    baseline = max(1.0, _median(present[:baseline_count]))
    for index, value in enumerate(values):
        if value is None or value < baseline * 1.5:
            continue
        recent = [item for item in values[max(0, index - 2) : index + 1] if item is not None]
        if len(recent) >= 2 and recent[-1] > recent[0]:
            return index
    return None


def _first_price_event_index(closes: Sequence[Optional[float]], hourly_changes: Sequence[Optional[float]]) -> Optional[int]:
    for index, close in enumerate(closes):
        hourly = hourly_changes[index] if index < len(hourly_changes) else None
        if hourly is not None and abs(hourly) >= 0.025:
            return index
        prior = [value for value in closes[max(0, index - 6) : index] if value is not None]
        if close is None or len(prior) < 3:
            continue
        baseline = _median(prior)
        if baseline > 0 and abs((close - baseline) / baseline) >= 0.04:
            return index
    return None


def _first_volume_event_index(turnovers: Sequence[Optional[float]], hourly_changes: Sequence[Optional[float]]) -> Optional[int]:
    for index, turnover in enumerate(turnovers):
        hourly = hourly_changes[index] if index < len(hourly_changes) else None
        if hourly is not None and hourly >= 0.50:
            return index
        prior = [value for value in turnovers[max(0, index - 12) : index] if value is not None]
        if turnover is None or len(prior) < 4:
            continue
        baseline = _median(prior)
        if baseline > 0 and turnover >= baseline * 2.0:
            return index
    return None


def _first_growth_event_index(values: Sequence[Optional[float]], threshold: float, lookback: int) -> Optional[int]:
    for index, value in enumerate(values):
        prior = [item for item in values[max(0, index - lookback) : index] if item is not None]
        if value is None or len(prior) < 2:
            continue
        baseline = prior[0]
        if baseline > 0 and (value - baseline) / baseline >= threshold:
            return index
    return None


def _event_payload(
    kind: str,
    index: Optional[int],
    points: Sequence[Dict[str, Any]],
    values: Sequence[Optional[float]],
    fallback_values: Optional[Sequence[Optional[float]]] = None,
) -> Optional[Dict[str, Any]]:
    if index is None or index < 0 or index >= len(points):
        return None
    value = values[index] if index < len(values) else None
    if value is None and fallback_values is not None and index < len(fallback_values):
        value = fallback_values[index]
    return {
        "kind": kind,
        "index": index,
        "time": points[index].get("time"),
        "value": value,
    }


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


def _within_after(start: Optional[int], candidate: Optional[int], min_hours: int, max_hours: int) -> bool:
    if start is None or candidate is None:
        return False
    delta = candidate - start
    return min_hours <= delta <= max_hours


def _momentum_fades(values: Sequence[Optional[float]], event_index: Optional[int]) -> bool:
    if event_index is None:
        return False
    after = [abs(value) for value in values[event_index + 1 : event_index + 7] if value is not None]
    before = [abs(value) for value in values[max(0, event_index - 6) : event_index + 1] if value is not None]
    if not after or not before:
        return False
    return (sum(after) / len(after)) < (sum(before) / len(before)) * 0.65


def _scale_0_100(values: Sequence[Optional[float]]) -> List[Optional[float]]:
    present = [value for value in values if value is not None]
    if not present:
        return [None for _ in values]
    min_value = min(present)
    max_value = max(present)
    span = max_value - min_value
    if span <= 0:
        return [50.0 if value is not None else None for value in values]
    return [round(((value - min_value) / span) * 100.0, 4) if value is not None else None for value in values]


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
