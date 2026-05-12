from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional, Sequence
from uuid import uuid4

from .bybit import BybitPublicClient
from .filters import is_excluded_base, tradable_symbol
from .models import Candidate, CvdStats, Instrument, LongShortRatio, PipelineCandidate, PipelineStageResult, ScanRun, Ticker
from .scoring import MarketSnapshot, score_snapshot
from .token_intelligence import (
    MppTokenIntelligenceClient,
    TokenIntelligenceClient,
    fundamentals_stage_payload,
    social_stage_payload,
)


@dataclass
class ScanConfig:
    top: int = 5
    max_symbols: int = 40
    min_turnover_24h: float = 2_000_000.0
    window_hours: int = 24
    workers: int = 8


@dataclass
class ScanReport:
    run: ScanRun
    top_long: List[Candidate]
    top_short_watch: List[Candidate]
    all_candidates: List[PipelineCandidate]
    rejected_candidates: List[PipelineCandidate]
    stage_failures: Dict[str, int]
    scanned_symbols: int
    eligible_symbols: int
    errors: Dict[str, str]

    def to_dict(self) -> Dict[str, object]:
        top_limit = _top_limit(self.run.config)
        top_gainers_pipeline = _pipeline_side_candidates(self.all_candidates, "gainer", top_limit)
        top_losers_pipeline = _pipeline_side_candidates(self.all_candidates, "loser", top_limit)
        return {
            "run": self.run.to_dict(),
            "top_long": [candidate.to_dict() for candidate in self.top_long],
            "top_short_watch": [candidate.to_dict() for candidate in self.top_short_watch],
            "top_gainers_24h": [candidate.to_dict() for candidate in self.top_long],
            "top_losers_24h": [candidate.to_dict() for candidate in self.top_short_watch],
            "top_gainers_pipeline": top_gainers_pipeline,
            "top_losers_pipeline": top_losers_pipeline,
            "top_gainers_24h_pipeline": top_gainers_pipeline,
            "top_losers_24h_pipeline": top_losers_pipeline,
            "all_candidates": [candidate.to_dict() for candidate in self.all_candidates],
            "rejected_candidates": [candidate.to_dict() for candidate in self.rejected_candidates],
            "pipeline_runs": [self.run.to_dict()],
            "stage_failures": self.stage_failures,
            "raw_snapshots": [
                {
                    "symbol": candidate.symbol,
                    "snapshots": [snapshot.to_dict() for snapshot in candidate.raw_snapshots],
                }
                for candidate in self.all_candidates
                if candidate.raw_snapshots
            ],
            "scanned_symbols": self.scanned_symbols,
            "eligible_symbols": self.eligible_symbols,
            "errors": self.errors,
        }

    def write_json(self, path: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


class HypeRadarEngine:
    def __init__(
        self,
        bybit: Optional[BybitPublicClient] = None,
        token_intelligence: Optional[TokenIntelligenceClient] = None,
    ) -> None:
        self.bybit = bybit or BybitPublicClient()
        self.token_intelligence = token_intelligence or MppTokenIntelligenceClient()

    def scan(self, config: ScanConfig) -> ScanReport:
        started_at = _utc_now()
        run_id = str(uuid4())
        instruments = {item.symbol: item for item in self.bybit.instruments_info()}
        tickers = [ticker for ticker in self.bybit.tickers() if ticker.symbol in instruments]
        ticker_by_symbol = {ticker.symbol: ticker for ticker in tickers}
        pipeline_by_symbol: Dict[str, PipelineCandidate] = {}
        eligible: List[Ticker] = []

        for symbol, instrument in instruments.items():
            ticker = ticker_by_symbol.get(symbol)
            pipeline = PipelineCandidate(symbol=symbol, base_coin=instrument.base_coin, quote_coin=instrument.quote_coin)
            pipeline.add_raw("bybit.instrument", asdict(instrument))
            if ticker:
                pipeline.add_raw("bybit.ticker", asdict(ticker))
            market_stage = _market_stage(instrument, ticker, config)
            pipeline.add_stage(market_stage)
            pipeline_by_symbol[symbol] = pipeline
            if ticker and market_stage.status == "pass":
                eligible.append(ticker)

        window_hours = _scan_window_hours(config)
        alt_market_return = _alt_market_return(eligible)
        top_gainers, top_losers, window_metrics = self._select_market_movers(eligible, config)
        selected = _unique_tickers(top_gainers + top_losers)
        self._fill_selected_volume_metrics(selected, window_metrics, window_hours)
        long_short_ratios = {ticker.symbol: self._long_short_ratio(ticker.symbol) for ticker in selected}
        selected_symbols = {ticker.symbol for ticker in selected}
        for ticker in top_gainers:
            _add_initial_selection_stage(
                pipeline_by_symbol[ticker.symbol],
                ticker,
                _selection_bucket_name("gainer", window_hours),
                config.top,
                window_metrics.get(ticker.symbol, {}),
                long_short_ratios.get(ticker.symbol),
            )
        for ticker in top_losers:
            _add_initial_selection_stage(
                pipeline_by_symbol[ticker.symbol],
                ticker,
                _selection_bucket_name("loser", window_hours),
                config.top,
                window_metrics.get(ticker.symbol, {}),
                long_short_ratios.get(ticker.symbol),
            )
        for ticker in eligible:
            if ticker.symbol not in selected_symbols:
                metrics = window_metrics.get(ticker.symbol, {})
                price_change = metrics.get("price_change_window_pct")
                pipeline_by_symbol[ticker.symbol].add_stage(
                    PipelineStageResult(
                        stage="final_ranking",
                        status="skipped",
                        score=round(price_change * 100.0, 4) if price_change is not None else None,
                        reason=f"Eligible, but not in Top-{config.top} {window_hours}h gainers or Top-{config.top} {window_hours}h losers for this run.",
                        metrics={
                            "scan_window_hours": window_hours,
                            "price_change_window_pct": price_change,
                            "price_24h_pct": ticker.price_24h_pct,
                            "selection_rule": f"top_{window_hours}h_gainers_and_losers",
                            "top": config.top,
                        },
                        raw_source={"ticker": asdict(ticker)},
                        blocking=False,
                    )
                )

        candidates: List[Candidate] = []
        errors: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max(1, config.workers)) as executor:
            futures = {
                executor.submit(
                    self._enrich_symbol,
                    ticker,
                    instruments[ticker.symbol],
                    alt_market_return,
                    pipeline_by_symbol[ticker.symbol],
                    long_short_ratios.get(ticker.symbol),
                ): ticker.symbol
                for ticker in selected
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    candidate = future.result()
                    if candidate:
                        candidates.append(candidate)
                except Exception as exc:  # noqa: BLE001 - scanner should degrade per-symbol.
                    errors[symbol] = str(exc)
                    pipeline_by_symbol[symbol].add_stage(
                        PipelineStageResult(
                            stage="market_scan",
                            status="error",
                            score=None,
                            reason=str(exc),
                            metrics={},
                            raw_source={},
                            blocking=True,
                        )
                    )

        candidate_by_symbol = {candidate.symbol: candidate for candidate in candidates}
        top_long = [candidate_by_symbol[ticker.symbol] for ticker in top_gainers if ticker.symbol in candidate_by_symbol]
        top_short = [candidate_by_symbol[ticker.symbol] for ticker in top_losers if ticker.symbol in candidate_by_symbol]
        for candidate in top_long:
            candidate.rank_bucket = _selection_bucket_name("gainer", window_hours)
        for candidate in top_short:
            candidate.rank_bucket = _selection_bucket_name("loser", window_hours)

        all_pipeline_candidates = list(pipeline_by_symbol.values())
        rejected_candidates = [candidate for candidate in all_pipeline_candidates if candidate.is_rejected]
        stage_failures = _stage_failures(all_pipeline_candidates)
        completed_at = _utc_now()
        run = ScanRun(
            run_id=run_id,
            started_at=started_at,
            completed_at=completed_at,
            status="completed" if not errors else "completed_with_errors",
            config=asdict(config),
            summary={
                "eligible_symbols": len(eligible),
                "scanned_symbols": len(selected),
                "total_symbols": len(instruments),
                "rejected_symbols": len(rejected_candidates),
                "errors": len(errors),
                "selection_rule": f"top_{config.top}_{window_hours}h_gainers_and_top_{config.top}_{window_hours}h_losers",
                "scan_window_hours": window_hours,
                "top_gainers": len(top_long),
                "top_losers": len(top_short),
            },
        )

        return ScanReport(
            run=run,
            top_long=top_long,
            top_short_watch=top_short,
            all_candidates=all_pipeline_candidates,
            rejected_candidates=rejected_candidates,
            stage_failures=stage_failures,
            scanned_symbols=len(selected),
            eligible_symbols=len(eligible),
            errors=errors,
        )

    def market_scan(self, config: ScanConfig) -> ScanReport:
        started_at = _utc_now()
        run_id = str(uuid4())
        instruments = {item.symbol: item for item in self.bybit.instruments_info()}
        tickers = [ticker for ticker in self.bybit.tickers() if ticker.symbol in instruments]
        ticker_by_symbol = {ticker.symbol: ticker for ticker in tickers}
        pipeline_by_symbol: Dict[str, PipelineCandidate] = {}
        eligible: List[Ticker] = []

        for symbol, instrument in instruments.items():
            ticker = ticker_by_symbol.get(symbol)
            pipeline = PipelineCandidate(symbol=symbol, base_coin=instrument.base_coin, quote_coin=instrument.quote_coin)
            pipeline.add_raw("bybit.instrument", asdict(instrument))
            if ticker:
                pipeline.add_raw("bybit.ticker", asdict(ticker))
            market_stage = _market_stage(instrument, ticker, config)
            pipeline.add_stage(market_stage)
            pipeline_by_symbol[symbol] = pipeline
            if ticker and market_stage.status == "pass":
                eligible.append(ticker)

        window_hours = _scan_window_hours(config)
        top_gainers, top_losers, window_metrics = self._select_market_movers(eligible, config)
        selected = _unique_tickers(top_gainers + top_losers)
        self._fill_selected_volume_metrics(selected, window_metrics, window_hours)
        long_short_ratios = {ticker.symbol: self._long_short_ratio(ticker.symbol) for ticker in selected}
        selected_symbols = {ticker.symbol for ticker in selected}
        for ticker in top_gainers:
            _add_initial_selection_stage(
                pipeline_by_symbol[ticker.symbol],
                ticker,
                _selection_bucket_name("gainer", window_hours),
                config.top,
                window_metrics.get(ticker.symbol, {}),
                long_short_ratios.get(ticker.symbol),
            )
        for ticker in top_losers:
            _add_initial_selection_stage(
                pipeline_by_symbol[ticker.symbol],
                ticker,
                _selection_bucket_name("loser", window_hours),
                config.top,
                window_metrics.get(ticker.symbol, {}),
                long_short_ratios.get(ticker.symbol),
            )
        for ticker in eligible:
            if ticker.symbol not in selected_symbols:
                metrics = window_metrics.get(ticker.symbol, {})
                price_change = metrics.get("price_change_window_pct")
                pipeline_by_symbol[ticker.symbol].add_stage(
                    PipelineStageResult(
                        stage="final_ranking",
                        status="skipped",
                        score=round(price_change * 100.0, 4) if price_change is not None else None,
                        reason=f"Eligible, but not in Top-{config.top} {window_hours}h gainers or Top-{config.top} {window_hours}h losers for this market scan.",
                        metrics={
                            "scan_window_hours": window_hours,
                            "price_change_window_pct": price_change,
                            "price_24h_pct": ticker.price_24h_pct,
                            "selection_rule": f"top_{window_hours}h_gainers_and_losers",
                            "top": config.top,
                        },
                        raw_source={"ticker": asdict(ticker)},
                        blocking=False,
                    )
                )

        all_pipeline_candidates = list(pipeline_by_symbol.values())
        rejected_candidates = [candidate for candidate in all_pipeline_candidates if candidate.is_rejected]
        run = ScanRun(
            run_id=run_id,
            started_at=started_at,
            completed_at=_utc_now(),
            status="completed",
            config=asdict(config),
            summary={
                "eligible_symbols": len(eligible),
                "scanned_symbols": len(selected),
                "total_symbols": len(instruments),
                "rejected_symbols": len(rejected_candidates),
                "errors": 0,
                "selection_rule": f"top_{config.top}_{window_hours}h_gainers_and_top_{config.top}_{window_hours}h_losers_market_only",
                "scan_window_hours": window_hours,
                "top_gainers": len(top_gainers),
                "top_losers": len(top_losers),
            },
        )
        return ScanReport(
            run=run,
            top_long=[],
            top_short_watch=[],
            all_candidates=all_pipeline_candidates,
            rejected_candidates=rejected_candidates,
            stage_failures=_stage_failures(all_pipeline_candidates),
            scanned_symbols=len(selected),
            eligible_symbols=len(eligible),
            errors={},
        )

    def research_symbol(self, symbol: str, config: ScanConfig) -> PipelineCandidate:
        target = symbol.upper()
        instruments = {item.symbol: item for item in self.bybit.instruments_info()}
        tickers = [ticker for ticker in self.bybit.tickers() if ticker.symbol in instruments]
        ticker_by_symbol = {ticker.symbol: ticker for ticker in tickers}
        instrument = instruments.get(target)
        if not instrument:
            pipeline = PipelineCandidate(symbol=target)
            pipeline.add_stage(
                PipelineStageResult(
                    stage="market_scan",
                    status="error",
                    score=None,
                    reason="Symbol not found in Bybit linear instruments.",
                    metrics={"symbol": target},
                    raw_source={},
                    blocking=True,
                )
            )
            return pipeline

        ticker = ticker_by_symbol.get(target)
        pipeline = PipelineCandidate(symbol=target, base_coin=instrument.base_coin, quote_coin=instrument.quote_coin)
        pipeline.add_raw("bybit.instrument", asdict(instrument))
        if ticker:
            pipeline.add_raw("bybit.ticker", asdict(ticker))
        market_stage = _market_stage(instrument, ticker, config)
        pipeline.add_stage(market_stage)
        if not ticker or market_stage.status != "pass":
            return pipeline

        long_short_ratio = self._long_short_ratio(ticker.symbol)
        _add_initial_selection_stage(
            pipeline,
            ticker,
            "manual_research",
            1,
            self._manual_window_metrics(ticker, _scan_window_hours(config)),
            long_short_ratio,
        )
        eligible_returns = [item.price_24h_pct for item in tickers if item.turnover_24h > 0]
        alt_market_return = mean(eligible_returns) / 24.0 if eligible_returns else 0.0
        self._enrich_symbol(ticker, instrument, alt_market_return, pipeline, long_short_ratio)
        return pipeline

    def _long_short_ratio(self, symbol: str) -> Optional[LongShortRatio]:
        getter = getattr(self.bybit, "long_short_ratio", None)
        if not getter:
            return None
        try:
            return getter(symbol, period="1h")
        except Exception:
            return None

    def _recent_trade_cvd(self, symbol: str) -> Optional[CvdStats]:
        getter = getattr(self.bybit, "recent_trade_cvd", None)
        if not getter:
            return None
        try:
            return getter(symbol, limit=1000)
        except Exception:
            return None

    def _volume_change_24h(self, symbol: str) -> Optional[float]:
        return self._volume_change_window(symbol, 24)

    def _volume_change_window(self, symbol: str, hours: int) -> Optional[float]:
        try:
            candles = self.bybit.klines(symbol, "60", limit=max(2, hours * 2))
        except Exception:
            return None
        if len(candles) < hours * 2:
            return None
        previous_turnover = sum(candle.turnover for candle in candles[-(hours * 2) : -hours])
        current_turnover = sum(candle.turnover for candle in candles[-hours:])
        if previous_turnover <= 0:
            return None
        return (current_turnover - previous_turnover) / previous_turnover

    def _select_market_movers(
        self,
        eligible: Sequence[Ticker],
        config: ScanConfig,
    ) -> tuple[List[Ticker], List[Ticker], Dict[str, Dict[str, Optional[float]]]]:
        window_hours = _scan_window_hours(config)
        metrics: Dict[str, Dict[str, Optional[float]]] = {}
        if window_hours >= 24:
            for ticker in eligible:
                metrics[ticker.symbol] = self._manual_window_metrics(ticker, window_hours)
        else:
            with ThreadPoolExecutor(max_workers=max(1, config.workers)) as executor:
                futures = {
                    executor.submit(self._manual_window_metrics, ticker, window_hours): ticker
                    for ticker in eligible
                }
                for future in as_completed(futures):
                    ticker = futures[future]
                    try:
                        metrics[ticker.symbol] = future.result()
                    except Exception:
                        metrics[ticker.symbol] = {
                            "scan_window_hours": window_hours,
                            "price_change_window_pct": None,
                            "volume_change_window_pct": None,
                            "price_24h_pct": ticker.price_24h_pct,
                            "volume_change_24h_pct": None,
                        }
        rankable = [ticker for ticker in eligible if metrics[ticker.symbol].get("price_change_window_pct") is not None]
        top_gainers = sorted(
            rankable,
            key=lambda ticker: metrics[ticker.symbol]["price_change_window_pct"] or 0.0,
            reverse=True,
        )[: config.top]
        top_losers = sorted(
            rankable,
            key=lambda ticker: metrics[ticker.symbol]["price_change_window_pct"] or 0.0,
        )[: config.top]
        return top_gainers, top_losers, metrics

    def _manual_window_metrics(self, ticker: Ticker, window_hours: int) -> Dict[str, Optional[float]]:
        if window_hours >= 24:
            return {
                "scan_window_hours": 24,
                "price_change_window_pct": ticker.price_24h_pct,
                "volume_change_window_pct": None,
                "price_24h_pct": ticker.price_24h_pct,
                "volume_change_24h_pct": None,
            }
        try:
            candles = self.bybit.klines(ticker.symbol, "60", limit=max(window_hours + 1, window_hours * 2))
        except Exception:
            return {
                "scan_window_hours": window_hours,
                "price_change_window_pct": None,
                "volume_change_window_pct": None,
                "price_24h_pct": ticker.price_24h_pct,
                "volume_change_24h_pct": None,
            }
        price_change = _price_change_from_candles(candles, window_hours)
        volume_change = _volume_change_from_candles(candles, window_hours)
        return {
            "scan_window_hours": window_hours,
            "price_change_window_pct": price_change,
            "volume_change_window_pct": volume_change,
            "price_24h_pct": ticker.price_24h_pct,
            "volume_change_24h_pct": None,
        }

    def _fill_selected_volume_metrics(
        self,
        selected: Sequence[Ticker],
        metrics: Dict[str, Dict[str, Optional[float]]],
        window_hours: int,
    ) -> None:
        for ticker in selected:
            row = metrics.setdefault(ticker.symbol, self._manual_window_metrics(ticker, window_hours))
            if row.get("volume_change_window_pct") is None:
                volume_change = self._volume_change_window(ticker.symbol, window_hours)
                row["volume_change_window_pct"] = volume_change
                if window_hours == 24:
                    row["volume_change_24h_pct"] = volume_change

    def _enrich_symbol(
        self,
        ticker: Ticker,
        instrument: Instrument,
        alt_market_return: float,
        pipeline: PipelineCandidate,
        long_short_ratio: Optional[LongShortRatio] = None,
    ) -> Optional[Candidate]:
        candles = {
            "15": self.bybit.klines(ticker.symbol, "15", limit=96),
            "60": self.bybit.klines(ticker.symbol, "60", limit=200),
            "240": self.bybit.klines(ticker.symbol, "240", limit=120),
            "D": self.bybit.klines(ticker.symbol, "D", limit=30),
        }
        pipeline.add_raw(
            "bybit.klines",
            {
                interval: {
                    "count": len(rows),
                    "first_start_ms": rows[0].start_ms if rows else None,
                    "last_start_ms": rows[-1].start_ms if rows else None,
                    "last_close": rows[-1].close if rows else None,
                }
                for interval, rows in candles.items()
            },
        )
        if len(candles["60"]) < 24 or len(candles["15"]) < 8:
            pipeline.add_stage(
                PipelineStageResult(
                    stage="market_scan",
                    status="fail",
                    score=None,
                    reason="Not enough kline history for 15m/1h anomaly scoring.",
                    metrics={"candles_15m": len(candles["15"]), "candles_1h": len(candles["60"])},
                    raw_source={"klines": {interval: len(rows) for interval, rows in candles.items()}},
                    blocking=True,
                )
            )
            return None
        orderbook = self.bybit.orderbook(ticker.symbol, limit=50)
        pipeline.add_raw("bybit.orderbook_stats", asdict(orderbook))
        cvd = self._recent_trade_cvd(ticker.symbol)
        pipeline.add_raw("bybit.recent_trade_cvd", asdict(cvd) if cvd else {"status": "unavailable"})
        snapshot = MarketSnapshot(
            ticker=ticker,
            orderbook=orderbook,
            candles=candles,
            alt_market_return_1h=alt_market_return,
            long_short_ratio=long_short_ratio,
            cvd=cvd,
        )
        candidate = score_snapshot(snapshot)
        token_data = self._token_intelligence(pipeline)
        _add_context_stages(pipeline, candidate, token_data)
        _add_risk_stages(pipeline, candidate, token_data)
        _enforce_rr_stage(pipeline, candidate)
        pipeline.candidate = candidate
        return candidate

    def _token_intelligence(self, pipeline: PipelineCandidate) -> Optional[Dict[str, object]]:
        if not self.token_intelligence.configured():
            return None
        try:
            token_data = self.token_intelligence.research(pipeline.base_coin or pipeline.symbol.replace("USDT", ""))
            pipeline.add_raw("token_intelligence", token_data)
            return token_data
        except Exception as exc:  # noqa: BLE001 - research must remain usable without external enrichment.
            pipeline.add_raw("token_intelligence.error", {"error": str(exc)})
            return None


def _prefilter_score(ticker: Ticker) -> float:
    abs_move = abs(ticker.price_24h_pct)
    turnover_boost = min(ticker.turnover_24h / 10_000_000.0, 10.0)
    oi_boost = min(ticker.open_interest_value / 5_000_000.0, 8.0)
    return abs_move * 70.0 + turnover_boost + oi_boost


def _alt_market_return(tickers: Sequence[Ticker]) -> float:
    returns = [ticker.price_24h_pct for ticker in tickers if ticker.turnover_24h > 0]
    return mean(returns) / 24.0 if returns else 0.0


def _is_long_bucket_candidate(candidate: Candidate) -> bool:
    if candidate.direction_bias == "LONG":
        return True
    if candidate.theme_lifecycle_stage in {"exhaustion", "distribution"}:
        return False
    return candidate.long_score >= candidate.short_score and candidate.verdict == "WATCH_ONLY"


def _unique_tickers(tickers: Sequence[Ticker]) -> List[Ticker]:
    selected: List[Ticker] = []
    seen = set()
    for ticker in tickers:
        if ticker.symbol not in seen:
            selected.append(ticker)
            seen.add(ticker.symbol)
    return selected


def _add_initial_selection_stage(
    pipeline: PipelineCandidate,
    ticker: Ticker,
    bucket: str,
    top: int,
    window_metrics: Optional[Dict[str, Optional[float]]] = None,
    long_short_ratio: Optional[LongShortRatio] = None,
) -> None:
    if any(stage.stage == "initial_selection" and stage.metrics.get("bucket") == bucket for stage in pipeline.stages):
        return
    window_metrics = window_metrics or {}
    window_hours = int(window_metrics.get("scan_window_hours") or 24)
    price_change_window_pct = window_metrics.get("price_change_window_pct")
    if price_change_window_pct is None:
        price_change_window_pct = ticker.price_24h_pct
    volume_change_window_pct = window_metrics.get("volume_change_window_pct")
    price_24h_pct = window_metrics.get("price_24h_pct")
    if price_24h_pct is None:
        price_24h_pct = ticker.price_24h_pct
    volume_change_24h_pct = window_metrics.get("volume_change_24h_pct")
    pipeline.add_stage(
        PipelineStageResult(
            stage="initial_selection",
            status="pass",
            score=round(price_change_window_pct * 100.0, 4),
            reason="Selected for research as %s by %sh price change." % (bucket, window_hours),
            metrics={
                "bucket": bucket,
                "scan_window_hours": window_hours,
                "price_change_window_pct": price_change_window_pct,
                "volume_change_window_pct": volume_change_window_pct,
                "price_24h_pct": price_24h_pct,
                "turnover_24h": ticker.turnover_24h,
                "volume_change_24h_pct": volume_change_24h_pct,
                "funding_rate": ticker.funding_rate,
                "open_interest": ticker.open_interest,
                "open_interest_value": ticker.open_interest_value,
                "long_ratio": long_short_ratio.long_ratio if long_short_ratio else None,
                "short_ratio": long_short_ratio.short_ratio if long_short_ratio else None,
                "long_short_timestamp_ms": long_short_ratio.timestamp_ms if long_short_ratio else None,
                "selection_rule": f"top_{window_hours}h_gainers_and_losers",
                "top": top,
            },
            raw_source={
                "ticker": asdict(ticker),
                "long_short_ratio": asdict(long_short_ratio) if long_short_ratio else None,
            },
            blocking=False,
        )
    )


def _selection_bucket(candidate: PipelineCandidate) -> Optional[str]:
    for stage in candidate.stages:
        if stage.stage == "initial_selection":
            return stage.metrics.get("bucket")
    return None


def _selection_side(candidate: PipelineCandidate) -> Optional[str]:
    bucket = _selection_bucket(candidate) or ""
    if bucket.endswith("_gainer"):
        return "gainer"
    if bucket.endswith("_loser"):
        return "loser"
    return None


def _top_limit(config: Dict[str, object]) -> int:
    try:
        return max(1, int(config.get("top", 5)))
    except (TypeError, ValueError):
        return 5


def _pipeline_side_candidates(candidates: Sequence[PipelineCandidate], side: str, limit: int) -> List[Dict[str, object]]:
    return [
        candidate.to_dict()
        for candidate in candidates
        if _selection_side(candidate) == side
    ][:limit]


def _selection_bucket_name(side: str, window_hours: int) -> str:
    suffix = "gainer" if side == "gainer" else "loser"
    return f"top_{window_hours}h_{suffix}"


def _scan_window_hours(config: ScanConfig) -> int:
    try:
        value = int(config.window_hours)
    except (TypeError, ValueError):
        value = 24
    return min(24, max(1, value))


def _price_change_from_candles(candles: Sequence[object], hours: int) -> Optional[float]:
    if hours <= 0 or len(candles) < hours + 1:
        return None
    previous = getattr(candles[-hours - 1], "close", None)
    current = getattr(candles[-1], "close", None)
    if previous is None or current is None or previous <= 0:
        return None
    return (current - previous) / previous


def _volume_change_from_candles(candles: Sequence[object], hours: int) -> Optional[float]:
    if hours <= 0 or len(candles) < hours * 2:
        return None
    previous = sum(getattr(candle, "turnover", 0.0) for candle in candles[-(hours * 2) : -hours])
    current = sum(getattr(candle, "turnover", 0.0) for candle in candles[-hours:])
    if previous <= 0:
        return None
    return (current - previous) / previous


def _market_stage(instrument: Instrument, ticker: Optional[Ticker], config: ScanConfig) -> PipelineStageResult:
    if not ticker:
        return PipelineStageResult(
            stage="market_scan",
            status="skipped",
            score=None,
            reason="Instrument has no ticker in current Bybit response.",
            metrics={"status": instrument.status, "contract_type": instrument.contract_type},
            raw_source={"instrument": asdict(instrument)},
            blocking=False,
        )
    passed = tradable_symbol(instrument, ticker, min_turnover_24h=config.min_turnover_24h)
    metrics = {
        "price_24h_pct": ticker.price_24h_pct,
        "turnover_24h": ticker.turnover_24h,
        "volume_24h": ticker.volume_24h,
        "funding_rate": ticker.funding_rate,
        "open_interest_value": ticker.open_interest_value,
        "min_turnover_24h": config.min_turnover_24h,
    }
    if passed:
        return PipelineStageResult(
            stage="market_scan",
            status="pass",
            score=round(_prefilter_score(ticker), 4),
            reason="Symbol is tradable and eligible for high-volatility prefiltering.",
            metrics=metrics,
            raw_source={"instrument": asdict(instrument), "ticker": asdict(ticker)},
            blocking=False,
        )
    failure_reasons = _market_filter_reasons(instrument, ticker, config)
    return PipelineStageResult(
        stage="market_scan",
        status="fail",
        score=round(_prefilter_score(ticker), 4),
        reason="; ".join(failure_reasons) if failure_reasons else "Symbol failed universe/liquidity filters before expensive analysis.",
        metrics={**metrics, "filter_reasons": failure_reasons},
        raw_source={"instrument": asdict(instrument), "ticker": asdict(ticker)},
        blocking=True,
    )


def _market_filter_reasons(instrument: Optional[Instrument], ticker: Optional[Ticker], config: ScanConfig) -> List[str]:
    reasons: List[str] = []
    if not instrument:
        return ["Instrument not found."]
    if instrument.quote_coin.upper() != "USDT":
        reasons.append("Quote coin is not USDT.")
    if instrument.status != "Trading":
        reasons.append(f"Instrument status is {instrument.status}.")
    if instrument.contract_type and instrument.contract_type != "LinearPerpetual":
        reasons.append(f"Contract type is {instrument.contract_type}.")
    if is_excluded_base(instrument.base_coin):
        reasons.append("Base coin is excluded by universe filter.")
    if not ticker:
        reasons.append("Ticker is missing.")
        return reasons
    if ticker.last_price <= 0:
        reasons.append("Last price is missing or zero.")
    if ticker.turnover_24h < config.min_turnover_24h:
        reasons.append(
            f"24h turnover ${ticker.turnover_24h:,.0f} is below minimum ${config.min_turnover_24h:,.0f}."
        )
    if ticker.bid_price > 0 and ticker.ask_price > 0:
        mid = (ticker.bid_price + ticker.ask_price) / 2.0
        spread_bps = ((ticker.ask_price - ticker.bid_price) / mid) * 10000.0 if mid > 0 else 999.0
        if spread_bps > 60.0:
            reasons.append(f"Ticker spread {spread_bps:.1f} bps is above 60.0 bps.")
    return reasons


def _add_context_stages(
    pipeline: PipelineCandidate,
    candidate: Candidate,
    token_data: Optional[Dict[str, object]] = None,
) -> None:
    payload = fundamentals_stage_payload(token_data, candidate.hype_cause)
    pipeline.add_stage(
        PipelineStageResult(
            stage="fundamentals",
            status=str(payload["status"]),
            score=payload["score"],
            reason=str(payload["reason"]),
            metrics=payload["metrics"],
            raw_source=token_data or {},
            blocking=False,
        )
    )
    social_payload = social_stage_payload(token_data, candidate.scores.social_quality)
    pipeline.add_stage(
        PipelineStageResult(
            stage="social_filter",
            status=str(social_payload["status"]),
            score=social_payload["score"],
            reason=str(social_payload["reason"]),
            metrics=social_payload["metrics"],
            raw_source=(token_data or {}).get("lunarcrush", {}) if token_data else {},
            blocking=False,
        )
    )


def _add_risk_stages(
    pipeline: PipelineCandidate,
    candidate: Candidate,
    token_data: Optional[Dict[str, object]] = None,
) -> None:
    manipulation_status = _manipulation_status(candidate.manipulation_score)
    fundamental_stage = next((stage for stage in pipeline.stages if stage.stage == "fundamentals"), None)
    supply_ratio = (fundamental_stage.metrics or {}).get("circulating_supply_ratio") if fundamental_stage else None
    tokenomics_risk = (fundamental_stage.metrics or {}).get("tokenomics_risk_score") if fundamental_stage else None
    intelligence_note = (
        " Публичный контекст CoinGecko/LunarCrush учтен; on-chain анализ намеренно вне scope."
        if token_data
        else " Token intelligence пока недоступен."
    )
    manipulation_reason = (
        _manipulation_reason(candidate.manipulation_score)
        + intelligence_note
    )
    pipeline.add_stage(
        PipelineStageResult(
            stage="manipulation_detector",
            status=manipulation_status,
            score=round(100.0 - candidate.manipulation_score, 2),
            reason=manipulation_reason,
            metrics={
                "manipulation_score": candidate.manipulation_score,
                "late_entry_risk": candidate.late_entry_risk,
                "manipulation_risk_label": _risk_level(candidate.manipulation_score),
                "late_entry_risk_label": _late_entry_level(candidate.late_entry_risk),
                "manipulation_breakdown": candidate.manipulation_breakdown,
                "late_entry_breakdown": candidate.late_entry_breakdown,
                "spread_liquidity_score": candidate.scores.liquidity,
                "candle_volume_concentration": candidate.features.candle_volume_concentration,
                "circulating_supply_ratio": supply_ratio,
                "circulating_supply_warn_threshold": 0.30,
                "tokenomics_risk_score": tokenomics_risk,
                "risk_contributors": _manipulation_contributors(candidate, supply_ratio, tokenomics_risk),
                "supply_risk_policy": "warn if circulating_supply / total_or_max_supply < 0.30",
            },
            raw_source={"scores": candidate.to_dict().get("scores", {}), "features": candidate.to_dict().get("features", {})},
            blocking=candidate.manipulation_score > 82,
        )
    )
    ta_status = "pass" if candidate.verdict in {"LONG_ENTER", "SHORT_ENTER", "LONG_WAIT_PULLBACK", "SHORT_WATCH"} else "warn"
    ta_metrics = dict(candidate.technical_analysis or {})
    ta_metrics.update(
        {
            "strategy_identifier": candidate.strategy_identifier,
            "ta_long": candidate.scores.ta_long,
            "ta_short": candidate.scores.ta_short,
            "rsi_1h": candidate.features.rsi_1h,
            "atr_distance_1h": candidate.features.atr_distance_1h,
            "failed_breakout": candidate.features.failed_breakout,
            "structure_breakdown": candidate.features.structure_breakdown,
        }
    )
    pipeline.add_stage(
        PipelineStageResult(
            stage="technical_analysis",
            status=ta_status,
            score=max(candidate.scores.ta_long, candidate.scores.ta_short),
            reason=candidate.reason_summary,
            metrics=ta_metrics,
            raw_source={"features": candidate.to_dict().get("features", {})},
            blocking=False,
        )
    )


def _enforce_rr_stage(pipeline: PipelineCandidate, candidate: Candidate) -> None:
    rr = candidate.trade_plan.risk_reward
    actionable = candidate.verdict in {"LONG_ENTER", "SHORT_ENTER"}
    if actionable and (rr is None or rr < 3.0):
        candidate.verdict = "SETUP_REJECTED_TA"
        candidate.reason_summary = "Rejected: risk/reward is below required 1:3."
        status = "fail"
        blocking = True
        reason = "Risk/reward below required 1:3; setup cannot be actionable."
    elif rr is not None and rr >= 3.0:
        status = "pass"
        blocking = False
        reason = "Trade plan meets minimum 1:3 risk/reward."
    else:
        status = "skipped"
        blocking = False
        reason = "No actionable trade plan; setup gate not applied."
    pipeline.add_stage(
        PipelineStageResult(
            stage="trade_plan",
            status=status,
            score=rr,
            reason=reason,
            metrics={"risk_reward": rr, "verdict": candidate.verdict},
            raw_source={"trade_plan": candidate.trade_plan.to_dict() if hasattr(candidate.trade_plan, "to_dict") else asdict(candidate.trade_plan)},
            blocking=blocking,
        )
    )
    pipeline.add_stage(
        PipelineStageResult(
            stage="final_ranking",
            status="pass" if candidate.verdict not in {"AVOID", "SETUP_REJECTED_TA"} else "fail",
            score=candidate.opportunity_score,
            reason="Final ranking verdict: %s." % candidate.verdict,
            metrics={
                "long_score": candidate.long_score,
                "short_score": candidate.short_score,
                "opportunity_score": candidate.opportunity_score,
                "rank_bucket": candidate.rank_bucket,
                "strategy_identifier": candidate.strategy_identifier,
            },
            raw_source={"candidate": candidate.to_dict()},
            blocking=candidate.verdict in {"AVOID", "SETUP_REJECTED_TA"},
        )
    )


def _manipulation_reason(score: float) -> str:
    if score > 82:
        return "Критический риск манипуляции/ликвидности."
    if score > 55:
        return "Повышенный риск манипуляции; нужна более сильная валидация."
    return "Критичных признаков манипуляции не найдено."


def _manipulation_status(score: float) -> str:
    if score > 82:
        return "fail"
    if score > 55:
        return "warn"
    return "pass"


def _risk_level(score: float) -> str:
    if score > 82:
        return "высокий"
    if score > 55:
        return "средний"
    return "низкий"


def _late_entry_level(score: float) -> str:
    if score > 75:
        return "движение перегрето"
    if score > 45:
        return "есть риск догонять движение"
    return "вход не выглядит поздним"


def _manipulation_contributors(
    candidate: Candidate,
    supply_ratio: Optional[float],
    tokenomics_risk: Optional[float],
) -> List[str]:
    contributors: List[str] = []
    if candidate.scores.liquidity < 45:
        contributors.append("тонкая ликвидность / слабая глубина")
    if candidate.features.candle_volume_concentration > 0.55:
        contributors.append("объем сконцентрирован в малом числе свечей")
    if abs(candidate.funding_rate) > 0.001:
        contributors.append("перегретый фандинг")
    if supply_ratio is not None and supply_ratio < 0.30:
        contributors.append("низкая циркуляция <30%")
    if tokenomics_risk is not None and tokenomics_risk >= 60:
        contributors.append("повышенный tokenomics risk")
    return contributors[:5] or ["критичных факторов в доступных данных нет"]


def _stage_failures(candidates: Sequence[PipelineCandidate]) -> Dict[str, int]:
    failures: Dict[str, int] = {}
    for candidate in candidates:
        for stage in candidate.stages:
            if stage.status in {"fail", "error"}:
                failures[stage.stage] = failures.get(stage.stage, 0) + 1
    return failures


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
