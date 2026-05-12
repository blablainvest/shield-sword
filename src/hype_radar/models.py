from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Candle:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


@dataclass(frozen=True)
class Ticker:
    symbol: str
    last_price: float
    bid_price: float
    ask_price: float
    price_24h_pct: float
    volume_24h: float
    turnover_24h: float
    funding_rate: float
    open_interest: float
    open_interest_value: float


@dataclass(frozen=True)
class LongShortRatio:
    symbol: str
    long_ratio: Optional[float]
    short_ratio: Optional[float]
    timestamp_ms: Optional[int]


@dataclass(frozen=True)
class TradePrint:
    symbol: str
    side: str
    price: float
    size: float
    timestamp_ms: Optional[int]


@dataclass(frozen=True)
class CvdStats:
    symbol: str
    cvd_base: float
    buy_volume_base: float
    sell_volume_base: float
    trade_count: int
    first_timestamp_ms: Optional[int]
    last_timestamp_ms: Optional[int]


@dataclass(frozen=True)
class Instrument:
    symbol: str
    base_coin: str
    quote_coin: str
    status: str
    contract_type: str
    launch_time_ms: Optional[int]
    tick_size: Optional[float]


@dataclass(frozen=True)
class OrderbookStats:
    spread_bps: float
    depth_bid_usdt_50bps: float
    depth_ask_usdt_50bps: float
    depth_total_usdt_50bps: float


@dataclass
class FeatureSet:
    return_15m: float = 0.0
    return_1h: float = 0.0
    return_4h: float = 0.0
    return_24h: float = 0.0
    z_return_1h: float = 0.0
    z_return_4h: float = 0.0
    volume_growth_1h: float = 1.0
    turnover_growth_1h: float = 1.0
    candle_volume_concentration: float = 0.0
    rsi_1h: float = 50.0
    atr_pct_1h: float = 0.0
    atr_distance_1h: float = 0.0
    vwap_distance_pct_1h: float = 0.0
    volume_declining_on_highs: bool = False
    failed_breakout: bool = False
    structure_breakdown: bool = False


@dataclass
class ScoreBreakdown:
    market_anomaly: float = 0.0
    volume_quality: float = 0.0
    liquidity: float = 0.0
    derivatives_health: float = 0.0
    catalyst_freshness: float = 5.0
    social_quality: float = 5.0
    ta_long: float = 0.0
    ta_short: float = 0.0
    relative_strength: float = 0.0
    manipulation_penalty: float = 0.0
    late_entry_penalty: float = 0.0


@dataclass
class TradePlan:
    entry: Optional[float] = None
    safer_entry: Optional[float] = None
    invalidation: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    take_profit_3: Optional[float] = None
    risk_reward: Optional[float] = None
    risk_note: str = ""


@dataclass
class Candidate:
    symbol: str
    direction_bias: str
    verdict: str
    rank_bucket: str
    long_score: float
    short_score: float
    opportunity_score: float
    manipulation_score: float
    late_entry_risk: float
    confidence: float
    theme_lifecycle_stage: str
    hype_cause: List[str]
    reason_summary: str
    trade_plan: TradePlan
    scores: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    features: FeatureSet = field(default_factory=FeatureSet)
    price_24h_pct: float = 0.0
    turnover_24h: float = 0.0
    funding_rate: float = 0.0
    strategy_identifier: str = "unknown"
    technical_analysis: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return data


@dataclass
class PipelineStageResult:
    stage: str
    status: str
    score: Optional[float]
    reason: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    raw_source: Dict[str, Any] = field(default_factory=dict)
    blocking: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RejectionReason:
    stage: str
    reason: str
    blocking: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RawSourceSnapshot:
    source: str
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineCandidate:
    symbol: str
    base_coin: str = ""
    quote_coin: str = ""
    candidate: Optional[Candidate] = None
    stages: List[PipelineStageResult] = field(default_factory=list)
    rejection_reasons: List[RejectionReason] = field(default_factory=list)
    raw_snapshots: List[RawSourceSnapshot] = field(default_factory=list)

    @property
    def final_verdict(self) -> str:
        if self.candidate:
            return self.candidate.verdict
        for stage in reversed(self.stages):
            if stage.status == "fail":
                return "REJECTED"
            if stage.status == "error":
                return "ERROR"
        return "SKIPPED"

    @property
    def failed_stage(self) -> Optional[str]:
        for stage in self.stages:
            if stage.status in {"fail", "error"}:
                return stage.stage
        return None

    @property
    def is_rejected(self) -> bool:
        return self.failed_stage is not None or self.final_verdict in {"AVOID", "SETUP_REJECTED_TA"}

    def add_stage(self, stage: PipelineStageResult) -> None:
        self.stages.append(stage)
        if stage.status in {"fail", "error"} or stage.blocking:
            self.rejection_reasons.append(RejectionReason(stage=stage.stage, reason=stage.reason, blocking=stage.blocking))

    def add_raw(self, source: str, payload: Dict[str, Any]) -> None:
        self.raw_snapshots.append(RawSourceSnapshot(source=source, payload=payload))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "base_coin": self.base_coin,
            "quote_coin": self.quote_coin,
            "final_verdict": self.final_verdict,
            "failed_stage": self.failed_stage,
            "is_rejected": self.is_rejected,
            "candidate": self.candidate.to_dict() if self.candidate else None,
            "stages": [stage.to_dict() for stage in self.stages],
            "rejection_reasons": [reason.to_dict() for reason in self.rejection_reasons],
            "raw_snapshots": [snapshot.to_dict() for snapshot in self.raw_snapshots],
        }


@dataclass
class ScanRun:
    run_id: str
    started_at: str
    completed_at: Optional[str]
    status: str
    config: Dict[str, Any]
    summary: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
