import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from hype_radar.engine import HypeRadarEngine, ScanConfig, _manipulation_status
from hype_radar.models import Candle, CvdStats, Instrument, LongShortRatio, OrderbookStats, Ticker
from hype_radar.storage import RadarStore, _preferred_side, _research_card, _summary, _why_it_moved, lifecycle_label, manipulation_level
from hype_radar.token_intelligence import (
    MppTokenIntelligenceClient,
    NullTokenIntelligenceClient,
    classify_fundamental,
    compact_lunarcrush_payload,
    extract_lunarcrush_metrics,
    fdv_tier,
    fundamentals_stage_payload,
    market_cap_to_fdv_profile,
    select_coingecko_identity,
    social_stage_payload,
)


def test_engine(**kwargs):
    kwargs.setdefault("bybit", FakeBybit())
    kwargs.setdefault("token_intelligence", NullTokenIntelligenceClient())
    return HypeRadarEngine(**kwargs)


def candles_from_closes(closes, volume=2000.0):
    candles = []
    for index, close in enumerate(closes):
        previous = closes[index - 1] if index else close
        candles.append(
            Candle(
                start_ms=index * 3600000,
                open=previous,
                high=max(previous, close) * 1.003,
                low=min(previous, close) * 0.997,
                close=close,
                volume=volume,
                turnover=volume * close,
            )
        )
    return candles


def ticker(symbol, price=1.0, pct24=0.12, turnover=5_000_000):
    return Ticker(
        symbol=symbol,
        last_price=price,
        bid_price=price * 0.999,
        ask_price=price * 1.001,
        price_24h_pct=pct24,
        volume_24h=turnover / price,
        turnover_24h=turnover,
        funding_rate=0.0001,
        open_interest=500_000,
        open_interest_value=2_000_000,
    )


class FakeBybit:
    def __init__(self):
        self.closes = [1.0 + index * 0.0005 for index in range(180)] + [1.08]

    def instruments_info(self):
        return [
            Instrument("BTCUSDT", "BTC", "USDT", "Trading", "LinearPerpetual", 1, 0.1),
            Instrument("DWFUSDT", "DWF", "USDT", "Trading", "LinearPerpetual", 1, 0.0001),
            Instrument("DROPUSDT", "DROP", "USDT", "Trading", "LinearPerpetual", 1, 0.0001),
        ]

    def tickers(self):
        return [
            ticker("BTCUSDT", price=100000, pct24=0.05, turnover=100_000_000),
            ticker("DWFUSDT", price=self.closes[-1], pct24=0.12, turnover=8_000_000),
            ticker("DROPUSDT", price=self.closes[-1], pct24=-0.18, turnover=9_000_000),
        ]

    def long_short_ratio(self, symbol, period="1h", category="linear"):
        ratios = {
            "DWFUSDT": LongShortRatio(symbol, 0.62, 0.38, 123456),
            "DROPUSDT": LongShortRatio(symbol, 0.41, 0.59, 123456),
        }
        return ratios.get(symbol)

    def recent_trade_cvd(self, symbol, limit=1000, category="linear"):
        return CvdStats(symbol, cvd_base=250.0, buy_volume_base=1250.0, sell_volume_base=1000.0, trade_count=30, first_timestamp_ms=1, last_timestamp_ms=2)

    def klines(self, symbol, interval, limit=200, category="linear"):
        candles = candles_from_closes(self.closes)
        return candles[-limit:]

    def orderbook(self, symbol, limit=50, category="linear"):
        return OrderbookStats(4.0, 100_000, 120_000, 220_000)


class FakeTokenIntelligence:
    def __init__(self, payload_by_base):
        self.payload_by_base = payload_by_base

    def configured(self):
        return True

    def research(self, base_coin):
        return self.payload_by_base[base_coin]


class WindowFakeBybit(FakeBybit):
    def instruments_info(self):
        return [
            Instrument("ALPHAUSDT", "ALPHA", "USDT", "Trading", "LinearPerpetual", 1, 0.0001),
            Instrument("BETAUSDT", "BETA", "USDT", "Trading", "LinearPerpetual", 1, 0.0001),
            Instrument("GAMMAUSDT", "GAMMA", "USDT", "Trading", "LinearPerpetual", 1, 0.0001),
            Instrument("OMEGAUSDT", "OMEGA", "USDT", "Trading", "LinearPerpetual", 1, 0.0001),
        ]

    def tickers(self):
        return [
            ticker("ALPHAUSDT", price=101, pct24=0.50, turnover=8_000_000),
            ticker("BETAUSDT", price=120, pct24=0.10, turnover=8_000_000),
            ticker("GAMMAUSDT", price=95, pct24=-0.10, turnover=8_000_000),
            ticker("OMEGAUSDT", price=80, pct24=-0.50, turnover=8_000_000),
        ]

    def klines(self, symbol, interval, limit=200, category="linear"):
        closes_by_symbol = {
            "ALPHAUSDT": [100, 101],
            "BETAUSDT": [100, 120],
            "GAMMAUSDT": [100, 95],
            "OMEGAUSDT": [100, 80],
        }
        return candles_from_closes(closes_by_symbol[symbol], volume=2000.0)[-limit:]


def token_payload(base, coin_id="token", circ=80, total=100, market_cap=100_000_000, fdv=300_000_000, volume=15_000_000):
    return {
        "base_coin": base,
        "identity": {
            "coin_id": coin_id,
            "symbol": base,
            "name": "%s Token" % base,
            "confidence": 0.95,
            "reason": "Exact ticker match.",
        },
        "coingecko": {
            "coin_data": {
                "categories": ["Artificial Intelligence"],
                "description": {"en": "%s narrative token." % base},
                "links": {
                    "homepage": ["https://example.com/%s" % base.lower()],
                    "whitepaper": "https://example.com/%s.pdf" % base.lower(),
                    "twitter_screen_name": "%s_token" % base.lower(),
                    "telegram_channel_identifier": "%s_chat" % base.lower(),
                    "subreddit_url": "https://reddit.com/r/%s" % base.lower(),
                    "repos_url": {"github": ["https://github.com/example/%s" % base.lower()]},
                },
                "market_data": {
                    "market_cap": {"usd": market_cap},
                    "fully_diluted_valuation": {"usd": fdv},
                    "total_volume": {"usd": volume},
                    "circulating_supply": circ,
                    "total_supply": total,
                },
                "platforms": {},
            },
            "market": [
                {
                    "id": coin_id,
                    "market_cap": market_cap,
                    "fully_diluted_valuation": fdv,
                    "total_volume": volume,
                    "price_change_percentage_24h": 12.5,
                    "price_change_percentage_7d_in_currency": 24.0,
                    "circulating_supply": circ,
                    "total_supply": total,
                }
            ],
        },
        "errors": [],
    }


class RecordingCoinGecko:
    def __init__(self):
        self.calls = []

    def search(self, query):
        self.calls.append(("search", query))
        return {
            "coins": [
                {
                    "id": "%s-token" % query.lower(),
                    "symbol": query,
                    "name": "%s Token" % query,
                    "market_cap_rank": 500,
                }
            ]
        }

    def coin(self, coin_id):
        self.calls.append(("coin", coin_id))
        return token_payload(coin_id.replace("-token", "").upper(), coin_id=coin_id)["coingecko"]["coin_data"]

    def markets(self, coin_id):
        self.calls.append(("markets", coin_id))
        return token_payload(coin_id.replace("-token", "").upper(), coin_id=coin_id)["coingecko"]["market"]

    def trending(self):
        self.calls.append(("trending", None))
        raise AssertionError("CoinGecko trending must not be called")

    def categories(self):
        self.calls.append(("categories", None))
        raise AssertionError("CoinGecko categories must not be called")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ENABLE_BLACKLIST_FILTER", None)

    def test_rejected_symbol_stays_visible_without_blacklist_stage(self):
        report = test_engine().scan(
            ScanConfig(top=5, max_symbols=2, min_turnover_24h=1_000_000, workers=1)
        )
        by_symbol = {candidate.symbol: candidate for candidate in report.all_candidates}

        self.assertIn("BTCUSDT", by_symbol)
        self.assertTrue(by_symbol["BTCUSDT"].is_rejected)
        self.assertEqual(by_symbol["BTCUSDT"].failed_stage, "market_scan")

        dwf = by_symbol["DWFUSDT"]
        self.assertFalse(any(stage.stage == "blacklist_screen" for stage in dwf.stages))

    def test_top_lists_are_selected_by_24h_gainers_and_losers(self):
        report = test_engine().scan(
            ScanConfig(top=1, max_symbols=2, min_turnover_24h=1_000_000, workers=1)
        )

        self.assertEqual([candidate.symbol for candidate in report.top_long], ["DWFUSDT"])
        self.assertEqual([candidate.symbol for candidate in report.top_short_watch], ["DROPUSDT"])
        self.assertEqual(report.top_long[0].rank_bucket, "top_24h_gainer")
        self.assertEqual(report.top_short_watch[0].rank_bucket, "top_24h_loser")

    def test_market_scan_can_rank_by_custom_1h_window(self):
        report = HypeRadarEngine(
            bybit=WindowFakeBybit(),
            token_intelligence=NullTokenIntelligenceClient(),
        ).market_scan(ScanConfig(top=1, min_turnover_24h=1_000_000, window_hours=1, workers=1))
        payload = report.to_dict()
        gainers = payload["top_gainers_pipeline"]
        losers = payload["top_losers_pipeline"]

        self.assertEqual(gainers[0]["symbol"], "BETAUSDT")
        self.assertEqual(losers[0]["symbol"], "OMEGAUSDT")
        gainer_selection = [stage for stage in gainers[0]["stages"] if stage["stage"] == "initial_selection"][0]
        loser_selection = [stage for stage in losers[0]["stages"] if stage["stage"] == "initial_selection"][0]
        self.assertEqual(gainer_selection["metrics"]["bucket"], "top_1h_gainer")
        self.assertEqual(loser_selection["metrics"]["bucket"], "top_1h_loser")
        self.assertEqual(gainer_selection["metrics"]["scan_window_hours"], 1)
        self.assertAlmostEqual(gainer_selection["metrics"]["price_change_window_pct"], 0.20)

    def test_market_scan_does_not_run_full_research_until_requested(self):
        engine = test_engine()
        report = engine.market_scan(ScanConfig(top=1, min_turnover_24h=1_000_000, workers=1))
        gainers = report.to_dict()["top_gainers_24h_pipeline"]

        self.assertEqual(gainers[0]["symbol"], "DWFUSDT")
        self.assertIsNone(gainers[0]["candidate"])
        selection = [stage for stage in gainers[0]["stages"] if stage["stage"] == "initial_selection"][0]
        self.assertIsNotNone(selection["metrics"]["volume_change_24h_pct"])
        self.assertEqual(selection["metrics"]["scan_window_hours"], 24)
        self.assertEqual(selection["metrics"]["price_change_window_pct"], selection["metrics"]["price_24h_pct"])
        self.assertEqual(selection["metrics"]["funding_rate"], 0.0001)
        self.assertEqual(selection["metrics"]["open_interest_value"], 2_000_000)
        self.assertEqual(selection["metrics"]["long_ratio"], 0.62)
        self.assertEqual(selection["metrics"]["short_ratio"], 0.38)

        researched = engine.research_symbol("DWFUSDT", ScanConfig(top=1, min_turnover_24h=1_000_000, workers=1))
        self.assertIsNotNone(researched.candidate)
        self.assertIn(
            researched.candidate.strategy_identifier,
            {
                "mean_reversion_extreme_funding",
                "short_squeeze_model",
                "oi_flush_model",
                "volatility_breakout_squeeze",
                "liquidity_sweep_strategy",
                "unknown",
            },
        )
        self.assertIn("signals", researched.candidate.technical_analysis)
        self.assertEqual(researched.candidate.technical_analysis["derivatives_filter"]["metrics"]["cvd"]["status"], "available")
        self.assertTrue(any(stage.stage == "technical_analysis" for stage in researched.stages))
        ta = [stage for stage in researched.stages if stage.stage == "technical_analysis"][0]
        self.assertEqual(ta.metrics["strategy_identifier"], researched.candidate.strategy_identifier)
        self.assertIn("breakout_20d_high", ta.metrics["signals"])
        fundamentals = [stage for stage in researched.stages if stage.stage == "fundamentals"][0]
        manipulation = [stage for stage in researched.stages if stage.stage == "manipulation_detector"][0]
        self.assertEqual(fundamentals.metrics["circulating_supply_warn_threshold"], 0.30)
        self.assertNotIn("blacklist_risk_score", manipulation.metrics)

    def test_research_card_has_compact_decision_layer(self):
        payload = token_payload("DROP", coin_id="drop-token", circ=20, total=100, market_cap=10_000_000, fdv=120_000_000, volume=200_000)
        intelligence = FakeTokenIntelligence({"DROP": payload})
        researched = HypeRadarEngine(
            bybit=FakeBybit(),
            token_intelligence=intelligence,
        ).research_symbol("DROPUSDT", ScanConfig(top=1, min_turnover_24h=1_000_000, workers=1))

        card = _research_card(researched.to_dict())
        decision = card["decision_layer"]

        self.assertEqual(decision["verdict"], researched.final_verdict)
        self.assertIn("no_trade_reason", decision)
        self.assertGreaterEqual(len(decision["activation_triggers"]), 1)
        self.assertEqual(decision["derivatives"]["cvd_status"], "available")
        self.assertIn("циркуляция", " ".join(decision["fundamentals"]["hard_blockers"]).lower())
        self.assertIn("decision_score", decision["ta"])
        self.assertIn("decision_relevant_ta", card["technical_analysis"]["metrics"])
        self.assertIn("blocks", decision)
        self.assertIn("final_decision", decision)
        self.assertEqual(set(decision["blocks"].keys()), {"project", "fundamental", "social", "ta"})
        self.assertIn("cvd_summary", decision["blocks"]["project"])
        self.assertIn(decision["blocks"]["fundamental"]["verdict"], {"ok", "risk", "blocker"})
        self.assertNotEqual(decision["blocks"]["fundamental"]["verdict_label"], "Блокер")
        self.assertIn(
            decision["blocks"]["fundamental"]["verdict_label"],
            {"Слабый фундаментал", "Средний фундаментал", "Сильный фундаментал"},
        )
        self.assertIn("tag", decision["blocks"]["fundamental"])
        self.assertIn("scenario_label_ru", decision["blocks"]["social"])
        self.assertIn("velocity_level", decision["blocks"]["social"])
        self.assertNotIn("cvd_note", decision["blocks"]["social"])
        self.assertIn("entry_conditions", decision["blocks"]["ta"])
        self.assertIn("technical_context", decision["blocks"]["ta"])
        self.assertIn("trade_map", decision["blocks"]["ta"])
        self.assertIn("tag", decision["blocks"]["ta"])
        self.assertNotIn("Watch only", decision["verdict_label"])

    def test_preferred_side_uses_short_for_weak_project_pump_and_negative_cvd(self):
        final = {"direction_bias": "LONG", "verdict": "WATCH_ONLY", "long_score": 50, "short_score": 51}
        fundamentals = {"metrics": {"circulating_supply_ratio": 0.20}}
        research_charts = {"metrics": {"scenario": {"code": "fake_pump"}}}
        derivatives = {"cvd_bias": "negative"}

        self.assertEqual(_preferred_side(final, fundamentals, research_charts, derivatives), "short")

    def test_preferred_side_is_neutral_without_edge(self):
        final = {"direction_bias": "LONG", "verdict": "WATCH_ONLY", "long_score": 50, "short_score": 51}

        self.assertEqual(_preferred_side(final, {"metrics": {}}, {"metrics": {}}, {"cvd_bias": "neutral"}), "neutral")

    def test_coingecko_identity_prefers_exact_symbol_over_rank(self):
        identity = select_coingecko_identity(
            "B3",
            {
                "coins": [
                    {"id": "baby-b3", "symbol": "BABYB3", "name": "Baby B3", "market_cap_rank": 1},
                    {"id": "b3", "symbol": "b3", "name": "B3", "market_cap_rank": 500},
                ]
            },
        )

        self.assertEqual(identity.coin_id, "b3")
        self.assertGreaterEqual(identity.confidence, 0.9)

    def test_fundamentals_can_pass_with_coingecko_when_lunarcrush_is_missing(self):
        intelligence = FakeTokenIntelligence({"DWF": token_payload("DWF", coin_id="dwf-token")})
        researched = HypeRadarEngine(
            bybit=FakeBybit(),
            token_intelligence=intelligence,
        ).research_symbol("DWFUSDT", ScanConfig(top=1, min_turnover_24h=1_000_000, workers=1))

        fundamentals = [stage for stage in researched.stages if stage.stage == "fundamentals"][0]
        self.assertEqual(fundamentals.status, "warn")
        self.assertEqual(fundamentals.metrics["fundamental_label"], "Спекулятивный риск")
        self.assertIn("осторожности", fundamentals.metrics["fundamental_label_reason"])
        self.assertEqual(fundamentals.metrics["data_coverage"], "coingecko_only")
        self.assertIn("trend_label", fundamentals.metrics)
        self.assertEqual(fundamentals.metrics["fdv_tier"], "mid")
        self.assertEqual(fundamentals.metrics["fdv_tier_label"], "Mid cap / средняя")
        self.assertEqual(fundamentals.metrics["market_cap_to_fdv_level"], "20-40")
        self.assertEqual(fundamentals.metrics["homepage_url"], "https://example.com/dwf")
        self.assertNotIn("github_stars", fundamentals.metrics)
        self.assertNotIn("watchlist_portfolio_users", fundamentals.metrics)

    def test_fdv_tier_boundaries(self):
        cases = [
            (49_000_000, "tiny"),
            (50_000_000, "low"),
            (99_000_000, "low"),
            (100_000_000, "mid"),
            (999_000_000, "mid"),
            (1_000_000_000, "big"),
            (10_000_000_000, "giant"),
            (None, "unknown"),
        ]

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(fdv_tier(value)["tier"], expected)

    def test_market_cap_to_fdv_profile_boundaries(self):
        cases = [
            (0.19, "0-20"),
            (0.20, "20-40"),
            (0.40, "40-60"),
            (0.60, "60-80"),
            (0.80, "80-100"),
            (1.00, "80-100"),
            (1.01, "anomaly"),
            (None, "unknown"),
        ]

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(market_cap_to_fdv_profile(value)["level"], expected)

    def test_risk_summary_uses_russian_explanations(self):
        final = {
            "manipulation_score": 56,
            "late_entry_risk": 46,
            "theme_lifecycle_stage": "exhaustion",
            "hype_cause": ["volume_spike", "market_anomaly", "market_watch"],
        }

        summary = _summary(final)
        why = _why_it_moved(final)

        self.assertEqual(manipulation_level(55), "низкий")
        self.assertEqual(manipulation_level(56), "средний")
        self.assertEqual(manipulation_level(83), "высокий")
        self.assertEqual(lifecycle_label("exhaustion"), "истощение движения")
        self.assertTrue(all("Selected from" not in item for item in summary))
        self.assertTrue(any("Риск позднего входа" in item for item in summary))
        self.assertIn("всплеск объема", why[0])
        self.assertIn("аномальное движение цены", why[0])

    def test_manipulation_status_thresholds(self):
        self.assertEqual(_manipulation_status(55), "pass")
        self.assertEqual(_manipulation_status(56), "warn")
        self.assertEqual(_manipulation_status(82), "warn")
        self.assertEqual(_manipulation_status(83), "fail")

    def test_fundamental_two_label_scale(self):
        strong = {
            "project_quality_score": 70,
            "narrative_score": 75,
            "tokenomics_risk_score": 35,
            "circulating_supply_ratio": 0.20,
            "volume_to_market_cap": 0.20,
        }
        overheated = {**strong, "volume_to_market_cap": 1.10}
        tokenomics = {**strong, "tokenomics_risk_score": 70}
        weak = {**strong, "project_quality_score": 45, "narrative_score": 50}
        missing = {**strong, "project_quality_score": 20, "narrative_score": 20}

        self.assertEqual(classify_fundamental(strong)[0], "Нарратив подтвержден")
        self.assertEqual(classify_fundamental(strong)[1], "pass")
        self.assertEqual(classify_fundamental(overheated)[0], "Спекулятивный риск")
        self.assertEqual(classify_fundamental(tokenomics)[0], "Спекулятивный риск")
        self.assertEqual(classify_fundamental(weak)[0], "Спекулятивный риск")
        self.assertEqual(classify_fundamental(missing)[0], "Недостаточно данных")

    def test_coingecko_search_is_cached_and_global_feeds_are_not_called(self):
        fake = RecordingCoinGecko()
        client = MppTokenIntelligenceClient()
        client._tempo_configured = lambda: False
        client.direct_coingecko = fake
        client.lunarcrush_key = ""

        first = client.research("DWF")
        second = client.research("DWF")
        calls = [name for name, _ in fake.calls]

        self.assertEqual(first["identity"]["coin_id"], "dwf-token")
        self.assertTrue(second["coingecko"]["search"]["cached"])
        self.assertEqual(calls.count("search"), 1)
        self.assertEqual(calls.count("coin"), 2)
        self.assertEqual(calls.count("markets"), 2)
        self.assertNotIn("trending", calls)
        self.assertNotIn("categories", calls)

    def test_low_float_marks_fundamentals_weak_and_feeds_manipulation_risk(self):
        payload = token_payload("DROP", coin_id="drop-token", circ=20, total=100, market_cap=10_000_000, fdv=120_000_000, volume=200_000)
        intelligence = FakeTokenIntelligence({"DROP": payload})
        researched = HypeRadarEngine(
            bybit=FakeBybit(),
            token_intelligence=intelligence,
        ).research_symbol("DROPUSDT", ScanConfig(top=1, min_turnover_24h=1_000_000, workers=1))

        fundamentals = [stage for stage in researched.stages if stage.stage == "fundamentals"][0]
        manipulation = [stage for stage in researched.stages if stage.stage == "manipulation_detector"][0]

        self.assertEqual(fundamentals.status, "warn")
        self.assertEqual(fundamentals.metrics["fundamental_label"], "Спекулятивный риск")
        self.assertLess(fundamentals.metrics["circulating_supply_ratio"], 0.30)
        self.assertEqual(fundamentals.metrics["tokenomics_risk_level"], "высокий")
        self.assertFalse(any(stage.stage == "blacklist_screen" for stage in researched.stages))
        self.assertNotIn("blacklist_risk_score", manipulation.metrics)
        self.assertEqual(manipulation.metrics["circulating_supply_ratio"], fundamentals.metrics["circulating_supply_ratio"])

    def test_low_float_and_extreme_volume_are_not_marked_as_clean_fundamental_strength(self):
        payload = token_payload(
            "NIL",
            coin_id="nil-token",
            circ=24,
            total=100,
            market_cap=205_000_000,
            fdv=855_000_000,
            volume=266_000_000,
        )
        fundamentals = fundamentals_stage_payload(payload, ["market_anomaly", "volume_spike"])

        self.assertEqual(fundamentals["status"], "warn")
        self.assertNotEqual(fundamentals["metrics"]["fundamental_label"], "Нарратив подтвержден")
        self.assertEqual(fundamentals["metrics"]["fundamental_label"], "Спекулятивный риск")
        self.assertGreater(fundamentals["metrics"]["volume_to_market_cap"], 1.0)
        self.assertTrue(any("Vol/MC" in flag for flag in fundamentals["metrics"]["red_flags"]))
        self.assertTrue(any("Циркуляция" in flag for flag in fundamentals["metrics"]["red_flags"]))
        self.assertLessEqual(len(fundamentals["metrics"]["project_brief_ru"]), 500)

    def test_lunarcrush_context_feeds_fundamental_thesis(self):
        payload = token_payload("B3", coin_id="b3-token")
        payload["lunarcrush"] = {
            "data": {
                "ai_summary": {
                    "summary": "B3 is a gaming infrastructure project with rising community attention.",
                    "whatsup": "Mentions accelerated after a game ecosystem announcement.",
                    "supportive": [{"title": "Gaming narrative expanding"}],
                    "critical": [{"title": "Unlock discussion is active"}],
                },
                "metrics": {"social_growth": 82, "galaxy_score": 78},
                "top_topics": [{"name": "Gaming"}],
                "alerts": [{"title": "Social volume spike"}],
            }
        }

        fundamentals = fundamentals_stage_payload(payload, ["market_anomaly"])

        self.assertEqual(fundamentals["metrics"]["trend_source"], "CoinGecko category")
        self.assertEqual(fundamentals["metrics"]["trend_label"], "Artificial Intelligence")
        self.assertEqual(fundamentals["metrics"]["social_topic"], "Gaming")
        self.assertIn("game ecosystem announcement", fundamentals["metrics"]["why_moved"])
        self.assertTrue(any("Gaming narrative" in item for item in fundamentals["metrics"]["movement_supportive"]))
        self.assertTrue(any("Unlock discussion" in item for item in fundamentals["metrics"]["movement_suspicious"]))
        self.assertTrue(all("Тикер уверенно" not in item for item in fundamentals["metrics"]["movement_supportive"]))

    def test_coingecko_taxonomy_wins_over_lunarcrush_social_topic_conflict(self):
        payload = token_payload("STRK", coin_id="starknet")
        payload["coingecko"]["coin_data"]["categories"] = ["Layer 2 (L2)", "Ethereum Ecosystem"]
        payload["coingecko"]["coin_data"]["description"]["en"] = (
            "Starknet is a permissionless Ethereum Layer 2 validity rollup using STARK proofs."
        )
        payload["coingecko"]["coin_data"]["platforms"] = {"ethereum": "0x123"}
        payload["lunarcrush"] = {
            "data": {
                "top_topics": [{"name": "coins solana ecosystem"}],
                "metrics": {"social_growth": 12, "galaxy_score": 42},
            }
        }

        fundamentals = fundamentals_stage_payload(payload, ["market_anomaly"])
        metrics = fundamentals["metrics"]

        self.assertEqual(metrics["sector"], "Layer 2 (L2)")
        self.assertEqual(metrics["chain_ecosystem"], "ethereum")
        self.assertEqual(metrics["trend_label"], "Layer 2 (L2)")
        self.assertEqual(metrics["trend_source"], "CoinGecko category")
        self.assertIn("solana", metrics["social_topic"].lower())
        self.assertIn("coingecko", metrics["source_conflict"].lower())

    def test_weak_social_fixture_does_not_emit_old_acceleration_stage(self):
        payload = token_payload("B3", coin_id="b3-token")
        payload["lunarcrush"] = {
            "data": {
                "metrics": {"social_growth": 5, "galaxy_score": 25},
                "top_topics": [{"name": "Gaming"}],
            }
        }

        fundamentals = fundamentals_stage_payload(payload, ["market_anomaly"])
        metrics = fundamentals["metrics"]

        self.assertNotIn("trend_stage", metrics)
        self.assertNotEqual(metrics["attention_phase"], "разгон")

    def test_pump_group_text_does_not_create_blacklist_stage(self):
        payload = token_payload("DROP", coin_id="drop-token")
        payload["coingecko"]["coin_data"]["description"]["en"] = "Promoted by a pump group."
        intelligence = FakeTokenIntelligence({"DROP": payload})
        researched = HypeRadarEngine(
            bybit=FakeBybit(),
            token_intelligence=intelligence,
        ).research_symbol("DROPUSDT", ScanConfig(top=1, min_turnover_24h=1_000_000, workers=1))

        self.assertFalse(any(stage.stage == "blacklist_screen" for stage in researched.stages))

    def test_lunarcrush_time_series_spike_passes_social_velocity_filter(self):
        token_data = token_payload("B3", coin_id="b3-token")
        token_data["lunarcrush"] = {
            "topic": {"data": {"posts_active": 240, "contributors_active": 42, "interactions": 5000}},
            "topic_time_series": {
                "data": [
                    {"time": 1, "posts_active": 90, "sentiment": 5, "spam": 90, "social_dominance": 50},
                    {"time": 2, "posts_active": 100, "galaxy_score": 1},
                    {"time": 3, "posts_active": 110, "alt_rank": 999},
                    {"time": 4, "posts_active": 240},
                ]
            },
            "posts": {"data": [{"post_title": "B3 mentions are accelerating"}]},
            "creators": {"data": [{"name": "creator"}]},
        }

        stage = social_stage_payload(token_data, fallback_score=5.0)
        metrics = stage["metrics"]

        self.assertEqual(stage["status"], "pass")
        self.assertEqual(metrics["social_label"], "Резкий всплеск упоминаний")
        self.assertGreaterEqual(metrics["social_volume_velocity_ratio"], 2.0)
        self.assertEqual(metrics["social_volume_source"], "topic_time_series")
        self.assertNotIn("social_quality_score", metrics)
        self.assertNotIn("coordination_risk_score", metrics)

    def test_moderate_lunarcrush_growth_warns_social_velocity_filter(self):
        token_data = token_payload("B3", coin_id="b3-token")
        token_data["lunarcrush"] = {
            "topic_time_series": {
                "data": [
                    {"time": 1, "posts_active": 100},
                    {"time": 2, "posts_active": 105},
                    {"time": 3, "posts_active": 100},
                    {"time": 4, "posts_active": 140},
                ]
            }
        }

        stage = social_stage_payload(token_data, fallback_score=5.0)

        self.assertEqual(stage["status"], "warn")
        self.assertEqual(stage["metrics"]["social_label"], "Ускорение упоминаний")

    def test_lunarcrush_snapshot_social_volume_falls_back_without_time_series(self):
        token_data = token_payload("B3", coin_id="b3-token")
        token_data["lunarcrush"] = {
            "topic": {"data": {"posts_active": 180}},
            "data": {"metrics": {"social_growth": 50}},
        }

        stage = social_stage_payload(token_data, fallback_score=5.0)
        metrics = stage["metrics"]

        self.assertEqual(stage["status"], "warn")
        self.assertEqual(metrics["social_volume_source"], "topic_snapshot")
        self.assertEqual(metrics["social_volume_current"], 180)
        self.assertAlmostEqual(metrics["social_volume_velocity_ratio"], 1.5)

    def test_missing_lunarcrush_social_volume_skips_social_filter(self):
        stage = social_stage_payload({"lunarcrush": {"topic": {"data": {"name": "B3"}}}}, fallback_score=5.0)

        self.assertEqual(stage["status"], "skipped")

    def test_non_velocity_lunarcrush_fields_do_not_change_social_verdict(self):
        base_payload = {
            "topic_time_series": {
                "data": [
                    {"time": 1, "posts_active": 100},
                    {"time": 2, "posts_active": 100},
                    {"time": 3, "posts_active": 240},
                ]
            }
        }
        noisy_payload = {
            **base_payload,
            "topic": {"data": {"sentiment": 0, "spam": 9999, "social_dominance": 100, "galaxy_score": 1}},
            "posts": {"data": [{"post_title": "pump bot spam shill"}]},
            "creators": {"data": [{"name": "creator"}]},
        }

        base = social_stage_payload({"lunarcrush": base_payload}, fallback_score=5.0)
        noisy = social_stage_payload({"lunarcrush": noisy_payload}, fallback_score=5.0)

        self.assertEqual(noisy["status"], base["status"])
        self.assertEqual(noisy["score"], base["score"])
        self.assertNotIn("coordination_risk_score", noisy["metrics"])

    def test_extract_lunarcrush_normalizes_posts_active_as_social_volume(self):
        metrics = extract_lunarcrush_metrics(
            {
                "topic_time_series": {
                    "data": [
                        {"time": 1, "posts_active": 40},
                        {"time": 2, "posts_active": 80},
                    ]
                }
            }
        )

        self.assertEqual(metrics["social_volume_current"], 80)
        self.assertEqual(metrics["social_volume_previous"], 40)
        self.assertEqual(metrics["social_volume_source"], "topic_time_series")

    def test_lunarcrush_raw_payload_is_compacted_before_storage(self):
        payload = {
            "data": [
                {"time": index, "posts_active": index, "body": "x" * 1000}
                for index in range(60)
            ]
        }

        compacted = compact_lunarcrush_payload("/public/topic/%24abc/time-series/v2?bucket=hour", payload)

        self.assertEqual(len(compacted["data"]), 48)
        self.assertEqual(compacted["data"][0]["time"], 12)
        self.assertNotIn("body", compacted["data"][0])

    def test_sqlite_history_round_trip(self):
        report = test_engine().scan(
            ScanConfig(top=5, max_symbols=2, min_turnover_24h=1_000_000, workers=1)
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RadarStore(tmpdir + "/radar.sqlite3")
            store.save_report(report)
            latest = store.latest_report()
            runs = store.list_runs()
            candidate = store.get_candidate(report.run.run_id, "DWFUSDT")
            stages = store.get_stages(report.run.run_id)

        self.assertIsNotNone(latest)
        self.assertEqual(len(runs), 1)
        self.assertIsNotNone(candidate)
        self.assertFalse(any(stage["stage"] == "blacklist_screen" for stage in stages))

    def test_repeated_research_is_append_only_and_stale_after_24h(self):
        researched = test_engine().research_symbol(
            "DWFUSDT", ScanConfig(top=1, min_turnover_24h=1_000_000, workers=1)
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RadarStore(tmpdir + "/radar.sqlite3")
            first = store.save_research("manual", researched)
            second = store.save_research("manual", researched)

            self.assertNotEqual(first["research_id"], second["research_id"])
            cards = store.list_research()
            self.assertEqual(len(cards), 2)
            self.assertEqual([card["research_id"] for card in cards], [second["research_id"], first["research_id"]])

            stale_created_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
            with store._connect() as conn:
                conn.execute(
                    "UPDATE research_cards SET created_at = ? WHERE id = ?",
                    (stale_created_at, first["research_id"]),
                )

            cards = store.list_research()
            by_id = {card["research_id"]: card for card in cards}
            self.assertFalse(by_id[second["research_id"]]["is_stale_after_24h"])
            self.assertTrue(by_id[first["research_id"]]["is_stale_after_24h"])
            self.assertGreaterEqual(by_id[first["research_id"]]["research_age_hours"], 24)


if __name__ == "__main__":
    unittest.main()
