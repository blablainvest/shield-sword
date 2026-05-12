import unittest

from hype_radar.models import Candle, OrderbookStats, Ticker
from hype_radar.scoring import MarketSnapshot, late_entry_risk, manipulation_score, score_snapshot


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
