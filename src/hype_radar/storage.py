from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .engine import ScanReport
from .models import PipelineCandidate


class RadarStore:
    def __init__(self, path: str = "data/hype_radar.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def save_report(self, report: ScanReport) -> None:
        payload = report.to_dict()
        run = payload["run"]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO scan_runs
                (run_id, started_at, completed_at, status, config_json, summary_json, report_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["run_id"],
                    run["started_at"],
                    run["completed_at"],
                    run["status"],
                    json.dumps(run["config"], ensure_ascii=False),
                    json.dumps(run["summary"], ensure_ascii=False),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            conn.execute("DELETE FROM candidates WHERE run_id = ?", (run["run_id"],))
            conn.execute("DELETE FROM stage_results WHERE run_id = ?", (run["run_id"],))
            conn.execute("DELETE FROM raw_snapshots WHERE run_id = ?", (run["run_id"],))
            for candidate in payload["all_candidates"]:
                final = candidate.get("candidate") or {}
                conn.execute(
                    """
                    INSERT INTO candidates
                    (run_id, symbol, base_coin, quote_coin, final_verdict, failed_stage,
                     is_rejected, long_score, short_score, manipulation_score, late_entry_risk,
                     risk_reward, candidate_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run["run_id"],
                        candidate["symbol"],
                        candidate.get("base_coin", ""),
                        candidate.get("quote_coin", ""),
                        candidate.get("final_verdict", ""),
                        candidate.get("failed_stage"),
                        1 if candidate.get("is_rejected") else 0,
                        final.get("long_score"),
                        final.get("short_score"),
                        final.get("manipulation_score"),
                        final.get("late_entry_risk"),
                        (final.get("trade_plan") or {}).get("risk_reward"),
                        json.dumps(candidate, ensure_ascii=False),
                    ),
                )
                for stage in candidate.get("stages", []):
                    conn.execute(
                        """
                        INSERT INTO stage_results
                        (run_id, symbol, stage, status, score, reason, blocking, metrics_json, raw_source_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run["run_id"],
                            candidate["symbol"],
                            stage.get("stage"),
                            stage.get("status"),
                            stage.get("score"),
                            stage.get("reason"),
                            1 if stage.get("blocking") else 0,
                            json.dumps(stage.get("metrics") or {}, ensure_ascii=False),
                            json.dumps(stage.get("raw_source") or {}, ensure_ascii=False),
                        ),
                    )
                for snapshot in candidate.get("raw_snapshots", []):
                    conn.execute(
                        """
                        INSERT INTO raw_snapshots (run_id, symbol, source, payload_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            run["run_id"],
                            candidate["symbol"],
                            snapshot.get("source"),
                            json.dumps(snapshot.get("payload") or {}, ensure_ascii=False),
                        ),
                    )

    def latest_report(self) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT report_json FROM scan_runs ORDER BY completed_at DESC, started_at DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["report_json"]) if row else None

    def save_research(self, run_id: str, candidate: PipelineCandidate) -> None:
        card = _research_card(candidate.to_dict())
        card["run_id"] = run_id
        card["created_at"] = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO research_cards
                (run_id, symbol, created_at, final_verdict, failed_stage, research_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    candidate.symbol,
                    card["created_at"],
                    card.get("final_verdict", ""),
                    card.get("failed_stage"),
                    json.dumps(card, ensure_ascii=False),
                ),
            )

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, started_at, completed_at, status, config_json, summary_json
                FROM scan_runs
                ORDER BY completed_at DESC, started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "status": row["status"],
                "config": json.loads(row["config_json"]),
                "summary": json.loads(row["summary_json"]),
            }
            for row in rows
        ]

    def get_report(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT report_json FROM scan_runs WHERE run_id = ?", (run_id,)).fetchone()
        return json.loads(row["report_json"]) if row else None

    def get_candidate(self, run_id: str, symbol: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT candidate_json FROM candidates WHERE run_id = ? AND symbol = ?",
                (run_id, symbol),
            ).fetchone()
        return json.loads(row["candidate_json"]) if row else None

    def get_stages(self, run_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol, stage, status, score, reason, blocking, metrics_json, raw_source_json
                FROM stage_results
                WHERE run_id = ?
                ORDER BY symbol, id
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "symbol": row["symbol"],
                "stage": row["stage"],
                "status": row["status"],
                "score": row["score"],
                "reason": row["reason"],
                "blocking": bool(row["blocking"]),
                "metrics": json.loads(row["metrics_json"]),
                "raw_source": json.loads(row["raw_source_json"]),
            }
            for row in rows
        ]

    def latest_run_id(self) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT run_id FROM scan_runs ORDER BY completed_at DESC, started_at DESC LIMIT 1"
            ).fetchone()
        return str(row["run_id"]) if row else None

    def list_research(self, run_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if run_id:
            query = """
                SELECT created_at, research_json FROM research_cards
                WHERE run_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            """
            params = (run_id, limit)
        else:
            query = """
                SELECT created_at, research_json FROM research_cards
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            """
            params = (limit,)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_card_with_created_at(row) for row in rows]

    def get_research(self, run_id: str, symbol: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT created_at, research_json FROM research_cards WHERE run_id = ? AND symbol = ?",
                (run_id, symbol.upper()),
            ).fetchone()
        return _card_with_created_at(row) if row else None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scan_runs (
                    run_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    report_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    base_coin TEXT,
                    quote_coin TEXT,
                    final_verdict TEXT,
                    failed_stage TEXT,
                    is_rejected INTEGER NOT NULL DEFAULT 0,
                    long_score REAL,
                    short_score REAL,
                    manipulation_score REAL,
                    late_entry_risk REAL,
                    risk_reward REAL,
                    candidate_json TEXT NOT NULL,
                    UNIQUE(run_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS stage_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    score REAL,
                    reason TEXT,
                    blocking INTEGER NOT NULL DEFAULT 0,
                    metrics_json TEXT NOT NULL,
                    raw_source_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS raw_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates(run_id);
                CREATE INDEX IF NOT EXISTS idx_stages_run ON stage_results(run_id);
                CREATE INDEX IF NOT EXISTS idx_raw_run ON raw_snapshots(run_id);

                CREATE TABLE IF NOT EXISTS research_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    final_verdict TEXT,
                    failed_stage TEXT,
                    research_json TEXT NOT NULL,
                    UNIQUE(run_id, symbol)
                );

                CREATE INDEX IF NOT EXISTS idx_research_run ON research_cards(run_id);
                """
            )


def _research_card(payload: Dict[str, Any]) -> Dict[str, Any]:
    final = payload.get("candidate") or {}
    trade_plan = final.get("trade_plan") or {}
    scores = final.get("scores") or {}
    features = final.get("features") or {}
    technical_analysis = final.get("technical_analysis") or {}
    stages = payload.get("stages") or []
    setup = _setup_label(final)
    return {
        "run_id": None,
        "symbol": payload.get("symbol"),
        "base_coin": payload.get("base_coin"),
        "quote_coin": payload.get("quote_coin"),
        "final_verdict": payload.get("final_verdict"),
        "failed_stage": payload.get("failed_stage"),
        "summary": _summary(final),
        "why_it_moved": _why_it_moved(final),
        "fundamentals": _stage_summary(stages, "fundamentals"),
        "links": _links(payload.get("symbol") or ""),
        "sentiment": _stage_summary(stages, "social_filter"),
        "manipulation": _stage_summary(stages, "manipulation_detector"),
        "technical_analysis": {
            "stage": _stage_summary(stages, "technical_analysis"),
            "metrics": technical_analysis
            or {
                "rsi_1h": features.get("rsi_1h"),
                "atr_distance_1h": features.get("atr_distance_1h"),
                "failed_breakout": features.get("failed_breakout"),
                "structure_breakdown": features.get("structure_breakdown"),
                "ta_long": scores.get("ta_long"),
                "ta_short": scores.get("ta_short"),
            },
        },
        "strategy_identifier": final.get("strategy_identifier") or "unknown",
        "setup": {
            "label": setup,
            "reason": _setup_reason(final, trade_plan),
            "trade_plan": trade_plan if setup != "No trade setup" else None,
        },
        "pipeline": payload,
    }


def _card_with_created_at(row: sqlite3.Row) -> Dict[str, Any]:
    card = json.loads(row["research_json"])
    card.setdefault("created_at", row["created_at"])
    return card


def _stage_summary(stages: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
    for stage in stages:
        if stage.get("stage") == name:
            return {
                "stage": stage.get("stage"),
                "status": stage.get("status"),
                "reason": stage.get("reason"),
                "metrics": stage.get("metrics") or {},
            }
    return {"stage": name, "status": "skipped", "reason": "Stage has not run yet.", "metrics": {}}


def _links(symbol: str) -> Dict[str, str]:
    base = symbol.replace("USDT", "")
    return {
        "bybit_chart": "https://www.bybit.com/trade/usdt/%s" % symbol,
        "coingecko_search": "https://www.coingecko.com/en/search_redirect?id=%s" % base.lower(),
    }


def _summary(final: Dict[str, Any]) -> List[str]:
    if not final:
        return ["Research did not reach scoring. Check the failed stage and raw market data."]
    return [
        "Selected from 24h movers and researched manually.",
        "Manipulation score: %s; late-entry risk: %s." % (final.get("manipulation_score"), final.get("late_entry_risk")),
        "Lifecycle stage: %s." % final.get("theme_lifecycle_stage"),
    ]


def _why_it_moved(final: Dict[str, Any]) -> List[str]:
    causes = final.get("hype_cause") or []
    if not causes:
        return ["No external catalyst module is configured yet; current explanation is market-data based."]
    return ["Detected market causes: %s." % ", ".join(causes)]


def _setup_label(final: Dict[str, Any]) -> str:
    verdict = final.get("verdict")
    if verdict in {"LONG_ENTER", "LONG_WAIT_PULLBACK"}:
        return "Long setup"
    if verdict in {"SHORT_ENTER", "SHORT_WATCH"}:
        return "Short setup"
    return "No trade setup"


def _setup_reason(final: Dict[str, Any], trade_plan: Dict[str, Any]) -> str:
    rr = trade_plan.get("risk_reward")
    if rr is None:
        return "No actionable setup was produced by the trade plan stage."
    if rr < 3.0:
        return "R:R below 1:3."
    if _setup_label(final) == "No trade setup":
        return final.get("reason_summary") or "No actionable setup."
    return final.get("reason_summary") or "Setup is available; review invalidation and risk."
