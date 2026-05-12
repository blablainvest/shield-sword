import unittest

from hype_radar.models import Candle, LongShortRatio, OrderbookStats, Ticker
from hype_radar.scoring import (
    MarketSnapshot,
    build_technical_analysis,
    late_entry_risk,
    manipulation_score,
    score_snapshot,
)


def candles_from_closes(closes, volume=1000.0):
    candles = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        high = max(previous, close) * 1.003
        low = min(previous, close) * 0.997
        candles.append(
            Candle(
                start_ms=index * 3600000,
                open=previous,
                high=high,
                low=low,
                close=close,
                volume=volume,
                turnover=volume * close,
            )
        )
    return candles


def ticker(symbol="TESTUSDT", price=1.0, pct24=0.1, turnover=10_000_000, funding=0.0001):
    return Ticker(
        symbol=symbol,
        last_price=price,
        bid_price=price * 0.999,
        ask_price=price * 1.001,
        price_24h_pct=pct24,
        volume_24h=turnover / price,
        turnover_24h=turnover,
        funding_rate=funding,
        open_interest=1_000_000,
        open_interest_value=5_000_000,
    )


class ScoringTests(unittest.TestCase):
    def test_fresh_anomaly_can_outrank_late_24h_move_for_long(self):
        baseline = [1.0 + i * 0.0005 for i in range(180)]
        fresh_closes = baseline + [1.18]
        late_closes = [1.0 + i * 0.004 for i in range(181)]

        fresh = score_snapshot(
            MarketSnapshot(
                ticker=ticker("FRESHUSDT", price=fresh_closes[-1], pct24=0.11),
                orderbook=OrderbookStats(4.0, 200_000, 210_000, 410_000),
                candles={"60": candles_from_closes(fresh_closes, 8000), "15": candles_from_closes(fresh_closes[-96:], 4000), "240": candles_from_closes(fresh_closes[-80:], 8000), "D": candles_from_closes([1.0, 1.11])},
            )
        )
        late = score_snapshot(
            MarketSnapshot(
                ticker=ticker("LATEUSDT", price=late_closes[-1], pct24=0.72, funding=0.0012),
                orderbook=OrderbookStats(4.0, 200_000, 210_000, 410_000),
                candles={"60": candles_from_closes(late_closes, 3000), "15": candles_from_closes(late_closes[-96:], 2000), "240": candles_from_closes(late_closes[-80:], 3000), "D": candles_from_closes([1.0, 1.72])},
            )
        )

        self.assertGreater(fresh.long_score, late.long_score)
        self.assertGreater(late.late_entry_risk, fresh.late_entry_risk)

    def test_thin_orderbook_and_one_candle_pump_raise_manipulation(self):
        closes = [1.0] * 175 + [1.01, 1.02, 1.03, 1.28, 1.29]
        candidate = score_snapshot(
            MarketSnapshot(
                ticker=ticker("THINUSDT", price=1.29, pct24=0.3, funding=0.0015),
                orderbook=OrderbookStats(35.0, 3000, 2500, 5500),
                candles={"60": candles_from_closes(closes, 1000), "15": candles_from_closes(closes[-96:], 1000), "240": candles_from_closes(closes[-80:], 1000), "D": candles_from_closes([1.0, 1.3])},
            )
        )

        self.assertGreater(candidate.manipulation_score, 55)
        active = {item["key"]: item for item in candidate.manipulation_breakdown if item["points"] > 0}
        self.assertIn("spread", active)
        self.assertIn("depth_50bps", active)
        self.assertIn("funding", active)
        self.assertAlmostEqual(
            candidate.manipulation_score,
            manipulation_score(candidate.features, OrderbookStats(35.0, 3000, 2500, 5500), ticker("THINUSDT", price=1.29, pct24=0.3, funding=0.0015)),
        )

    def test_short_requires_exhaustion_confirmation(self):
        closes = [1.0 + i * 0.003 for i in range(181)]
        momentum = score_snapshot(
            MarketSnapshot(
                ticker=ticker("MOMENTUMUSDT", price=closes[-1], pct24=0.55),
                orderbook=OrderbookStats(4.0, 250_000, 250_000, 500_000),
                candles={"60": candles_from_closes(closes, 6000), "15": candles_from_closes(closes[-96:], 3000), "240": candles_from_closes(closes[-80:], 6000), "D": candles_from_closes([1.0, 1.55])},
            )
        )

        self.assertNotEqual(momentum.verdict, "SHORT_ENTER")

    def test_late_entry_breakdown_matches_score(self):
        closes = [1.0 + i * 0.004 for i in range(181)]
        candidate = score_snapshot(
            MarketSnapshot(
                ticker=ticker("LATEUSDT", price=closes[-1], pct24=0.72, funding=0.0012),
                orderbook=OrderbookStats(4.0, 200_000, 210_000, 410_000),
                candles={"60": candles_from_closes(closes, 3000), "15": candles_from_closes(closes[-96:], 2000), "240": candles_from_closes(closes[-80:], 3000), "D": candles_from_closes([1.0, 1.72])},
            )
        )
        active = {item["key"]: item for item in candidate.late_entry_breakdown if item["points"] > 0}

        self.assertIn("return_24h", active)
        self.assertIn("return_4h", active)
        self.assertIn("funding", active)
        self.assertAlmostEqual(candidate.late_entry_risk, late_entry_risk(candidate.features, candidate_to_ticker(candidate)))

    def test_actionable_trade_plan_uses_minimum_three_risk_reward(self):
        closes = [1.0 + i * 0.0005 for i in range(180)] + [1.18]
        candidate = score_snapshot(
            MarketSnapshot(
                ticker=ticker("RRUSDT", price=closes[-1], pct24=0.11),
                orderbook=OrderbookStats(4.0, 500_000, 500_000, 1_000_000),
                candles={
                    "60": candles_from_closes(closes, 10000),
                    "15": candles_from_closes(closes[-96:], 5000),
                    "240": candles_from_closes(closes[-80:], 10000),
                    "D": candles_from_closes([1.0, 1.11]),
                },
            )
        )

        if candidate.trade_plan.risk_reward is not None:
            self.assertGreaterEqual(candidate.trade_plan.risk_reward, 3.0)

    def test_technical_analysis_block_reports_structured_signals(self):
        hourly = candles_from_closes([1.0 + i * 0.002 for i in range(60)] + [1.35], 1000)
        hourly[-1] = Candle(
            start_ms=hourly[-1].start_ms,
            open=hourly[-1].open,
            high=hourly[-1].high,
            low=hourly[-1].low,
            close=hourly[-1].close,
            volume=8000,
            turnover=8000 * hourly[-1].close,
        )
        daily = candles_from_closes([1.0 + i * 0.01 for i in range(22)] + [1.45], 1000)

        block = build_technical_analysis({"60": hourly, "D": daily})

        self.assertEqual(block["status"], "available")
        self.assertEqual(block["signals"]["breakout_20d_high"]["status"], "available")
        self.assertTrue(block["signals"]["breakout_20d_high"]["value"])
        self.assertEqual(block["signals"]["volume_spike"]["status"], "available")
        self.assertTrue(block["signals"]["volume_spike"]["value"])
        self.assertIn(block["signals"]["ema_cross"]["value"], {"bullish", "bearish", "none"})

    def test_technical_analysis_block_degrades_when_history_is_missing(self):
        block = build_technical_analysis({"60": candles_from_closes([1.0, 1.01]), "D": []})

        self.assertEqual(block["status"], "insufficient_data")
        self.assertEqual(block["signals"]["breakout_20d_high"]["status"], "insufficient_data")
        self.assertEqual(block["signals"]["atr_volatility_expansion"]["status"], "insufficient_data")

    def test_strategy_context_includes_onchain_filter_and_execution_plan(self):
        closes = [1.0 + i * 0.0005 for i in range(180)] + [1.18]
        candidate = score_snapshot(
            MarketSnapshot(
                ticker=ticker("CTXUSDT", price=closes[-1], pct24=0.11, funding=-0.0008),
                orderbook=OrderbookStats(4.0, 500_000, 500_000, 1_000_000),
                candles={
                    "60": candles_from_closes(closes, 10000),
                    "15": candles_from_closes(closes[-96:], 5000),
                    "240": candles_from_closes(closes[-80:], 10000),
                    "D": candles_from_closes([1.0 + i * 0.01 for i in range(22)] + [1.45]),
                },
                long_short_ratio=LongShortRatio("CTXUSDT", long_ratio=0.40, short_ratio=0.60, timestamp_ms=1),
            )
        )

        ta = candidate.technical_analysis
        self.assertEqual(ta["onchain_filter"]["metrics"]["funding_rate"], -0.0008)
        self.assertEqual(ta["onchain_filter"]["metrics"]["long_ratio"], 0.40)
        self.assertEqual(ta["onchain_filter"]["metrics"]["long_short_ratio_status"], "available")
        self.assertIn("cvd", ta["onchain_filter"]["metrics"])
        self.assertEqual(ta["strategy_models"]["selected"], candidate.strategy_identifier)
        self.assertIn("entry_basis", ta["execution_context"])
        self.assertIn("trade_plan", ta["execution_context"])

    def test_extreme_positive_funding_with_crowded_longs_selects_mean_reversion(self):
        closes = [1.0 + i * 0.001 for i in range(181)]
        candidate = score_snapshot(
            MarketSnapshot(
                ticker=ticker("FUNDINGUSDT", price=closes[-1], pct24=0.08, funding=0.0015),
                orderbook=OrderbookStats(4.0, 500_000, 500_000, 1_000_000),
                candles={
                    "60": candles_from_closes(closes, 5000),
                    "15": candles_from_closes(closes[-96:], 5000),
                    "240": candles_from_closes(closes[-80:], 5000),
                    "D": candles_from_closes([1.0 + i * 0.01 for i in range(30)]),
                },
                long_short_ratio=LongShortRatio("FUNDINGUSDT", long_ratio=0.67, short_ratio=0.33, timestamp_ms=1),
            )
        )

        self.assertEqual(candidate.strategy_identifier, "mean_reversion_extreme_funding")

    def test_short_squeeze_takes_precedence_over_generic_extreme_funding(self):
        closes = [1.0 + i * 0.0005 for i in range(180)] + [1.18]
        candidate = score_snapshot(
            MarketSnapshot(
                ticker=ticker("SQUEEZEUSDT", price=closes[-1], pct24=0.11, funding=-0.0015),
                orderbook=OrderbookStats(4.0, 500_000, 500_000, 1_000_000),
                candles={
                    "60": candles_from_closes(closes, 10000),
                    "15": candles_from_closes(closes[-96:], 5000),
                    "240": candles_from_closes(closes[-80:], 10000),
                    "D": candles_from_closes([1.0 + i * 0.01 for i in range(22)] + [1.45]),
                },
                long_short_ratio=LongShortRatio("SQUEEZEUSDT", long_ratio=0.32, short_ratio=0.68, timestamp_ms=1),
            )
        )

        self.assertEqual(candidate.strategy_identifier, "short_squeeze_model")

    def test_strategy_context_marks_long_short_ratio_unavailable_when_missing(self):
        closes = [1.0 + i * 0.0005 for i in range(40)]
        candidate = score_snapshot(
            MarketSnapshot(
                ticker=ticker("NOLSRUSDT", price=closes[-1], pct24=0.02),
                orderbook=OrderbookStats(4.0, 500_000, 500_000, 1_000_000),
                candles={
                    "60": candles_from_closes(closes, 5000),
                    "15": candles_from_closes(closes[-30:], 5000),
                    "240": candles_from_closes(closes[-30:], 5000),
                    "D": candles_from_closes([1.0 + i * 0.01 for i in range(25)]),
                },
            )
        )

        onchain = candidate.technical_analysis["onchain_filter"]
        self.assertEqual(onchain["status"], "partial")
        self.assertEqual(onchain["metrics"]["long_short_ratio_status"], "unavailable")
        self.assertIsNone(onchain["metrics"]["long_ratio"])
        self.assertIsNone(onchain["metrics"]["short_ratio"])


def candidate_to_ticker(candidate):
    return ticker(
        candidate.symbol,
        price=1.0,
        pct24=candidate.price_24h_pct,
        turnover=candidate.turnover_24h,
        funding=candidate.funding_rate,
    )


if __name__ == "__main__":
    unittest.main()
