import unittest
from datetime import datetime, timezone

from hype_radar.models import Candle, Ticker
from hype_radar.research_charts import (
    HOUR_MS,
    build_research_charts_stage,
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


class ResearchChartTests(unittest.TestCase):
    def setUp(self):
        self.research_time = datetime(2026, 5, 12, 12, 30, tzinfo=timezone.utc)
        self.buckets = hourly_buckets(self.research_time)
        self.start = self.buckets[0] - HOUR_MS

    def test_market_normalization_aligns_last_24_hour_buckets(self):
        closes = [100 + index for index in range(25)]
        turnovers = [1_000 + index * 10 for index in range(25)]
        market = normalize_market_series(candles_from(self.start, closes, turnovers), self.buckets)

        self.assertEqual(market["status"], "available")
        self.assertEqual(len(market["price_points"]), 24)
        self.assertAlmostEqual(market["price_points"][0]["value"], 0.01)
        self.assertAlmostEqual(market["volume_points"][0]["value"], 0.01)

    def test_lunarcrush_degrades_without_api_payload(self):
        social = normalize_lunarcrush_series({"skipped": True, "reason": "LUNARCRUSH_API_KEY is not configured."}, self.buckets)

        self.assertEqual(social["status"], "unavailable")
        self.assertTrue(all(point["value"] is None for point in social["points"]))
        self.assertIn("LUNARCRUSH_API_KEY", social["reason"])

    def test_scenario_a_organic_growth(self):
        scenario = detect_social_market_scenario(
            {
                "price_points": [{"value": 0.0}] * 8 + [{"value": 0.04}] + [{"value": 0.0}] * 15,
                "volume_points": [{"value": 0.0}] * 10 + [{"value": 0.80}] + [{"value": 0.0}] * 13,
            },
            {
                "status": "available",
                "points": [{"value": value} for value in ([10] * 4 + [16, 20, 24] + [25] * 17)],
                "context": {"sentiment": 72, "creators_count": 8, "top_posts": []},
            },
            {"change_pct": 0.0},
        )

        self.assertEqual(scenario["code"], "organic_growth")

    def test_scenario_b_fake_pump(self):
        scenario = detect_social_market_scenario(
            {
                "price_points": [{"value": 0.0}] * 4 + [{"value": 0.09}] + [{"value": 0.0}] * 19,
                "volume_points": [{"value": 0.0}] * 24,
            },
            {
                "status": "available",
                "points": [{"value": value} for value in ([10] * 8 + [20, 22, 8, 7] + [6] * 12)],
                "context": {"sentiment": 35, "creators_count": 2, "top_posts": []},
            },
            {"change_pct": 0.0},
        )

        self.assertEqual(scenario["code"], "fake_pump")

    def test_scenario_c_strong_signal(self):
        scenario = detect_social_market_scenario(
            {
                "price_points": [{"value": 0.005}] * 24,
                "volume_points": [{"value": 0.10}] * 6 + [{"value": -0.10}] * 18,
            },
            {
                "status": "available",
                "points": [{"value": value} for value in ([10] * 5 + [11, 12, 13, 14] + [14] * 15)],
                "context": {"sentiment": 62, "creators_count": 10, "top_posts": ["Narrative is live"]},
            },
            {"change_pct": 0.08},
        )

        self.assertEqual(scenario["code"], "strong_signal")

    def test_build_stage_returns_chart_status_without_fake_social_values(self):
        closes = [100 + index for index in range(25)]
        turnovers = [1_000 + index * 10 for index in range(25)]
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
