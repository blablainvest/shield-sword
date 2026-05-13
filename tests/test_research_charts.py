import unittest
from datetime import datetime, timezone

from hype_radar.models import Candle, Ticker
from hype_radar.research_charts import (
    HOUR_MS,
    build_research_charts_stage,
    detect_market_social_events,
    detect_social_market_scenario,
    hourly_buckets,
    normalize_lunarcrush_series,
    normalize_market_series,
)


def candles_from(start_ms, closes, turnovers):
    candles = []
    previous = closes[0]
    for index, close in enumerate(closes):
        candles.append(
            Candle(
                start_ms=start_ms + index * HOUR_MS,
                open=previous,
                high=max(previous, close),
                low=min(previous, close),
                close=close,
                volume=turnovers[index] / close,
                turnover=turnovers[index],
            )
        )
        previous = close
    return candles


def ticker():
    return Ticker(
        symbol="TESTUSDT",
        last_price=1.0,
        bid_price=0.99,
        ask_price=1.01,
        price_24h_pct=0.0,
        volume_24h=1_000_000,
        turnover_24h=1_000_000,
        funding_rate=0.0,
        open_interest=1_000_000,
        open_interest_value=1_000_000,
    )


def lunar_payload(buckets, values):
    return {
        "topic_time_series": {
            "data": [
                {"time": bucket, "posts_active": value}
                for bucket, value in zip(buckets, values)
                if value is not None
            ]
        }
    }


def market_from(closes, turnovers):
    price_points = []
    volume_points = []
    previous_close = None
    previous_turnover = None
    for index, close in enumerate(closes):
        price_change = None if previous_close is None else (close - previous_close) / previous_close
        volume_change = None if previous_turnover in (None, 0) else (turnovers[index] - previous_turnover) / previous_turnover
        price_points.append({"time": index, "value": price_change, "close": close})
        volume_points.append({"time": index, "value": volume_change, "turnover": turnovers[index]})
        previous_close = close
        previous_turnover = turnovers[index]
    return {"status": "available", "price_points": price_points, "volume_points": volume_points}


def social_from(values):
    return {
        "status": "available",
        "points": [{"time": index, "value": value} for index, value in enumerate(values)],
        "context": {"sentiment": 65, "creators_count": 10, "top_posts": []},
    }


def scenario_for(mentions, closes, turnovers, oi=None):
    market = market_from(closes, turnovers)
    social = social_from(mentions)
    oi_payload = oi or {"status": "unavailable", "points": [], "change_pct": None}
    events = detect_market_social_events(market, social, oi_payload)
    return detect_social_market_scenario(market, social, oi_payload, events), events


class ResearchChartTests(unittest.TestCase):
    def setUp(self):
        self.research_time = datetime(2026, 5, 12, 12, 30, tzinfo=timezone.utc)
        self.buckets = hourly_buckets(self.research_time)
        self.start = self.buckets[0] - HOUR_MS

    def test_market_normalization_aligns_last_48_hour_buckets(self):
        closes = [100 + index for index in range(49)]
        turnovers = [1_000 + index * 10 for index in range(49)]
        market = normalize_market_series(candles_from(self.start, closes, turnovers), self.buckets)

        self.assertEqual(market["status"], "available")
        self.assertEqual(len(market["price_points"]), 48)
        self.assertAlmostEqual(market["price_points"][0]["value"], 0.01)
        self.assertAlmostEqual(market["volume_points"][0]["value"], 0.01)

    def test_lunarcrush_degrades_without_api_payload(self):
        social = normalize_lunarcrush_series({"skipped": True, "reason": "LUNARCRUSH_API_KEY is not configured."}, self.buckets)

        self.assertEqual(social["status"], "unavailable")
        self.assertTrue(all(point["value"] is None for point in social["points"]))
        self.assertIn("LUNARCRUSH_API_KEY", social["reason"])

    def test_scenario_narrative_mentions_lead_price_and_volume(self):
        mentions = [10] * 10 + [16, 20, 24] + [24] * 35
        closes = [100] * 14 + [105] + [106] * 33
        turnovers = [1_000] * 15 + [2_400] + [2_300] * 32

        scenario, events = scenario_for(mentions, closes, turnovers)

        self.assertEqual(scenario["code"], "narrative")
        self.assertLess(events["mentions_event"]["index"], events["price_event"]["index"])
        self.assertLess(events["mentions_event"]["index"], events["volume_event"]["index"])

    def test_scenario_fake_pump_price_leads_without_volume(self):
        mentions = [10] * 18 + [16, 20, 10, 8] + [8] * 26
        closes = [100] * 10 + [105] + [105] * 37
        turnovers = [1_000] * 48

        scenario, events = scenario_for(mentions, closes, turnovers)

        self.assertEqual(scenario["code"], "fake_pump")
        self.assertLess(events["price_event"]["index"], events["mentions_event"]["index"])
        self.assertIsNone(events["volume_event"])

    def test_scenario_insider_pump_price_leads_and_volume_confirms(self):
        mentions = [10] * 15 + [16, 20, 24] + [24] * 30
        closes = [100] * 10 + [105] + [107] * 37
        turnovers = [1_000] * 11 + [2_400] + [2_500] * 36
        oi = {
            "status": "available",
            "points": [{"time": index, "value": 1_000 + index * 10} for index in range(48)],
            "change_pct": 0.10,
        }

        scenario, events = scenario_for(mentions, closes, turnovers, oi)

        self.assertEqual(scenario["code"], "insider_pump")
        self.assertLess(events["price_event"]["index"], events["mentions_event"]["index"])
        self.assertLessEqual(events["volume_event"]["index"] - events["price_event"]["index"], 6)

    def test_scenario_early_narrative_mentions_without_market_confirmation(self):
        mentions = [10] * 8 + [16, 20, 24, 28] + [28] * 36
        closes = [100] * 48
        turnovers = [1_000] * 48

        scenario, events = scenario_for(mentions, closes, turnovers)

        self.assertEqual(scenario["code"], "early_narrative")
        self.assertIsNotNone(events["mentions_event"])
        self.assertIsNone(events["price_event"])
        self.assertIsNone(events["volume_event"])

    def test_scenario_exhaustion_late_hype_after_fading_market_move(self):
        mentions = [10] * 18 + [16, 22, 26] + [24] * 27
        closes = [100] * 10 + [106, 107, 108, 108.4, 108.6, 108.7] + [108.7] * 32
        turnovers = [1_000] * 10 + [2_500, 2_400, 2_200, 1_800, 1_300, 1_050] + [1_000] * 32

        scenario, events = scenario_for(mentions, closes, turnovers)

        self.assertEqual(scenario["code"], "exhaustion_late_hype")
        self.assertLess(events["price_event"]["index"], events["mentions_event"]["index"])

    def test_build_stage_returns_48h_indexed_points_and_events(self):
        closes = [100 + index for index in range(49)]
        turnovers = [1_000 + index * 10 for index in range(49)]
        mentions = [10] * 10 + [16, 20, 24] + [24] * 35
        stage = build_research_charts_stage(
            "TESTUSDT",
            ticker(),
            candles_from(self.start, closes, turnovers),
            {"lunarcrush": lunar_payload(self.buckets, mentions)},
            research_time=self.research_time,
        )

        self.assertEqual(stage["metrics"]["window_hours"], 48)
        self.assertEqual(stage["metrics"]["coverage_status"], "full_48h")
        self.assertEqual(len(stage["metrics"]["indexed_points"]), 48)
        self.assertIn("mentions_event", stage["metrics"]["events"])

    def test_build_stage_returns_chart_status_without_fake_social_values(self):
        closes = [100 + index for index in range(49)]
        turnovers = [1_000 + index * 10 for index in range(49)]
        stage = build_research_charts_stage(
            "TESTUSDT",
            ticker(),
            candles_from(self.start, closes, turnovers),
            {"lunarcrush": {"skipped": True, "reason": "LUNARCRUSH_API_KEY is not configured."}},
            research_time=self.research_time,
        )

        mentions = stage["metrics"]["charts"]["mentions"]
        self.assertEqual(stage["status"], "pass")
        self.assertEqual(mentions["status"], "unavailable")
        self.assertTrue(all(point["value"] is None for point in mentions["points"]))
        self.assertEqual(stage["metrics"]["scenario"]["code"], "insufficient_social_data")


if __name__ == "__main__":
    unittest.main()
