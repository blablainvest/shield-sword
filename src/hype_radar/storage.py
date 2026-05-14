from __future__ import annotations

import json
import re
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

    def save_research(self, run_id: str, candidate: PipelineCandidate) -> Dict[str, Any]:
        card = _research_card(candidate.to_dict())
        card["run_id"] = run_id
        card["created_at"] = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO research_cards
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
            research_id = int(cursor.lastrowid)
            card["research_id"] = research_id
            conn.execute(
                "UPDATE research_cards SET research_json = ? WHERE id = ?",
                (json.dumps(card, ensure_ascii=False), research_id),
            )
        return card

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

    def list_research(self, run_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if run_id:
            query = """
                SELECT id, created_at, research_json FROM research_cards
                WHERE run_id = ?
                ORDER BY created_at DESC, id DESC
            """
            params: tuple[Any, ...] = (run_id,)
        else:
            query = """
                SELECT id, created_at, research_json FROM research_cards
                ORDER BY created_at DESC, id DESC
            """
            params = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (*params, limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_card_with_created_at(row) for row in rows]

    def get_research(self, run_id: str, symbol: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, created_at, research_json FROM research_cards
                WHERE run_id = ? AND symbol = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
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
                    research_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_research_run ON research_cards(run_id);
                CREATE INDEX IF NOT EXISTS idx_research_symbol_created ON research_cards(symbol, created_at DESC);
                """
            )
            self._migrate_research_cards_append_only(conn)

    def _migrate_research_cards_append_only(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'research_cards'"
        ).fetchone()
        table_sql = row["sql"] if row else ""
        if "UNIQUE(run_id, symbol)" not in table_sql:
            return
        conn.executescript(
            """
            ALTER TABLE research_cards RENAME TO research_cards_old;

            CREATE TABLE research_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                created_at TEXT NOT NULL,
                final_verdict TEXT,
                failed_stage TEXT,
                research_json TEXT NOT NULL
            );

            INSERT INTO research_cards
            (id, run_id, symbol, created_at, final_verdict, failed_stage, research_json)
            SELECT id, run_id, symbol, created_at, final_verdict, failed_stage, research_json
            FROM research_cards_old
            ORDER BY id;

            DROP TABLE research_cards_old;

            CREATE INDEX IF NOT EXISTS idx_research_run ON research_cards(run_id);
            CREATE INDEX IF NOT EXISTS idx_research_symbol_created ON research_cards(symbol, created_at DESC);
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
    fundamentals = _stage_summary(stages, "fundamentals")
    sentiment = _stage_summary(stages, "social_filter")
    research_charts = _stage_summary(stages, "research_charts")
    manipulation = _stage_summary(stages, "manipulation_detector")
    technical_stage = _stage_summary(stages, "technical_analysis")
    technical_metrics = dict(
        technical_analysis
        or {
            "rsi_1h": features.get("rsi_1h"),
            "atr_distance_1h": features.get("atr_distance_1h"),
            "failed_breakout": features.get("failed_breakout"),
            "structure_breakdown": features.get("structure_breakdown"),
            "ta_long": scores.get("ta_long"),
            "ta_short": scores.get("ta_short"),
        }
    )
    technical_metrics["decision_relevant_ta"] = _ta_decision_layer(final, _preferred_side(final, fundamentals, research_charts, {})) if final else {}
    return {
        "run_id": None,
        "symbol": payload.get("symbol"),
        "base_coin": payload.get("base_coin"),
        "quote_coin": payload.get("quote_coin"),
        "final_verdict": payload.get("final_verdict"),
        "failed_stage": payload.get("failed_stage"),
        "decision_layer": _decision_layer(final, fundamentals, sentiment, research_charts, manipulation),
        "summary": _summary(final),
        "why_it_moved": _why_it_moved(final),
        "fundamentals": fundamentals,
        "links": _links(payload.get("symbol") or ""),
        "sentiment": sentiment,
        "research_charts": research_charts,
        "manipulation": manipulation,
        "technical_analysis": {
            "stage": technical_stage,
            "metrics": technical_metrics,
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
    card["research_id"] = int(row["id"])
    card["created_at"] = row["created_at"]
    created_at = _parse_datetime(row["created_at"])
    if created_at:
        age_hours = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600
        card["research_age_hours"] = round(max(0.0, age_hours), 2)
        card["is_stale_after_24h"] = age_hours >= 24
    return card


def _parse_datetime(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
        return ["Исследование не дошло до скоринга. Проверь failed stage и raw market data."]
    manipulation = final.get("manipulation_score")
    late_entry = final.get("late_entry_risk")
    lifecycle = final.get("theme_lifecycle_stage")
    return [
        "Риск ликвидности / манипулятивности: %s / 100 — %s. Чем выше число, тем хуже качество движения и ликвидности."
        % (_fmt_score(manipulation), manipulation_level(manipulation)),
        "Риск позднего входа: %s / 100 — %s."
        % (_fmt_score(late_entry), late_entry_level(late_entry)),
        "Фаза движения: %s — %s."
        % (lifecycle_label(lifecycle), lifecycle_reason(lifecycle)),
    ]


def _why_it_moved(final: Dict[str, Any]) -> List[str]:
    causes = final.get("hype_cause") or []
    if not causes:
        return ["Внешний catalyst module пока не настроен; объяснение основано на market-data."]
    labels = [hype_cause_label(cause) for cause in causes]
    return ["Рыночные причины: %s." % ", ".join(labels)]


def _decision_layer(
    final: Dict[str, Any],
    fundamentals: Dict[str, Any],
    sentiment: Dict[str, Any],
    research_charts: Dict[str, Any],
    manipulation: Dict[str, Any],
) -> Dict[str, Any]:
    if not final:
        fundamentals_block = _fundamental_trade_block(fundamentals)
        social_block = _social_trade_block(sentiment, research_charts, fundamentals, "neutral", {})
        ta_block = _ta_trade_block({}, {}, "neutral", "Исследование не дошло до ТА.", {})
        return {
            "verdict": "NO_SCORE",
            "verdict_label": "Нет скоринга",
            "action": "Проверить failed stage.",
            "no_trade_reason": "Исследование не дошло до финального скоринга.",
            "activation_triggers": ["Перезапустить research после восстановления данных."],
            "primary_risk": {"key": "data", "label": "data", "reason": "Нет финального candidate payload."},
            "derivatives": {},
            "fundamentals": _fundamental_decision_layer(fundamentals),
            "ta": {},
            "chart_next_step": _chart_next_step(research_charts, final),
            "blocks": {
                "project": _project_trade_block({}, fundamentals, {}, "NO_SCORE", "neutral"),
                "fundamental": fundamentals_block,
                "social": social_block,
                "ta": ta_block,
            },
            "final_decision": _final_decision_payload(
                "NO_SCORE",
                "neutral",
                "Проверить failed stage.",
                "Исследование не дошло до финального скоринга.",
                ["Перезапустить research после восстановления данных."],
                {},
            ),
        }

    verdict = str(final.get("verdict") or "WATCH_ONLY")
    derivatives = _derivatives_decision_layer(final, "neutral")
    preferred_side = _preferred_side(final, fundamentals, research_charts, derivatives)
    no_trade_reason = _no_trade_reason(final, fundamentals, sentiment, manipulation)
    derivatives = _derivatives_decision_layer(final, preferred_side)
    ta = _ta_decision_layer(final, preferred_side)
    primary_risk = _primary_risk(final, sentiment, derivatives, ta)
    activation_triggers = _activation_triggers(final, sentiment, derivatives, ta, preferred_side)[:2]
    blocks = {
        "project": _project_trade_block(final, fundamentals, derivatives, verdict, preferred_side),
        "fundamental": _fundamental_trade_block(fundamentals),
        "social": _social_trade_block(sentiment, research_charts, fundamentals, preferred_side, derivatives),
        "ta": _ta_trade_block(ta, derivatives, preferred_side, _setup_reason(final, final.get("trade_plan") or {}), final),
    }
    return {
        "verdict": verdict,
        "verdict_label": _verdict_label(verdict),
        "preferred_side": preferred_side,
        "action": _decision_action(verdict),
        "no_trade_reason": no_trade_reason,
        "activation_triggers": activation_triggers,
        "primary_risk": primary_risk,
        "derivatives": derivatives,
        "fundamentals": _fundamental_decision_layer(fundamentals),
        "ta": ta,
        "chart_next_step": _chart_next_step(research_charts, final),
        "blocks": blocks,
        "final_decision": _final_decision_payload(
            verdict,
            preferred_side,
            _decision_action(verdict),
            no_trade_reason,
            activation_triggers,
            final.get("trade_plan") or {},
        ),
    }


def _preferred_side(
    final: Dict[str, Any],
    fundamentals: Optional[Dict[str, Any]] = None,
    research_charts: Optional[Dict[str, Any]] = None,
    derivatives: Optional[Dict[str, Any]] = None,
) -> str:
    long_score = _number(final.get("long_score"))
    short_score = _number(final.get("short_score"))
    scenario = (((research_charts or {}).get("metrics") or {}).get("scenario") or {}).get("code")
    has_fundamental_risk = bool(_fundamental_hard_blockers(((fundamentals or {}).get("metrics") or {})))
    cvd_bias = (derivatives or {}).get("cvd_bias")
    if scenario in {"fake_pump", "exhaustion_late_hype", "insider_pump"} and cvd_bias == "negative":
        if has_fundamental_risk or long_score is None or short_score is None or long_score - short_score <= 8.0:
            return "short"
    if scenario in {"narrative", "early_narrative"} and cvd_bias == "positive" and not has_fundamental_risk:
        return "long"
    if long_score is not None and short_score is not None and abs(short_score - long_score) >= 3.0:
        return "short" if short_score > long_score else "long"

    direction = str(final.get("direction_bias") or "").upper()
    verdict = str(final.get("verdict") or "").upper()
    if verdict in {"LONG_ENTER", "LONG_WAIT_PULLBACK"} or direction == "LONG" and verdict != "WATCH_ONLY":
        return "long"
    if verdict in {"SHORT_ENTER", "SHORT_WATCH"} or direction == "SHORT" and verdict != "WATCH_ONLY":
        return "short"
    return "neutral"


def _verdict_label(verdict: str) -> str:
    return {
        "LONG_ENTER": "Лонг активен",
        "LONG_WAIT_PULLBACK": "Ждать лонг",
        "SHORT_ENTER": "Шорт активен",
        "SHORT_WATCH": "Ждать шорт",
        "WATCH_ONLY": "Наблюдать",
        "AVOID": "Не торговать",
        "NO_SCORE": "Нет скоринга",
    }.get(verdict, verdict)


def _decision_action(verdict: str) -> str:
    if verdict in {"LONG_ENTER", "SHORT_ENTER"}:
        return "Сетап активен: проверять ликвидность, invalidation и риск на сделку."
    if verdict in {"LONG_WAIT_PULLBACK", "SHORT_WATCH"}:
        return "Не входить по рынку; ждать подтверждения и точки с нормальным R:R."
    if verdict == "AVOID":
        return "Не торговать: риск выше допустимого."
    return "Сделки нет; оставить в наблюдении только до появления триггеров."


def _no_trade_reason(
    final: Dict[str, Any],
    fundamentals: Dict[str, Any],
    sentiment: Dict[str, Any],
    manipulation: Dict[str, Any],
) -> str:
    verdict = str(final.get("verdict") or "")
    if verdict in {"LONG_ENTER", "SHORT_ENTER", "LONG_WAIT_PULLBACK", "SHORT_WATCH"}:
        return final.get("reason_summary") or "Сетап есть, но требует проверки исполнения."
    hard_blockers = _fundamental_hard_blockers(fundamentals.get("metrics") or {})
    if hard_blockers:
        return "Фундаментальный hard blocker: %s." % hard_blockers[0].rstrip(".")
    manipulation_score = _number((manipulation.get("metrics") or {}).get("manipulation_score"), final.get("manipulation_score"))
    late_entry = _number((manipulation.get("metrics") or {}).get("late_entry_risk"), final.get("late_entry_risk"))
    social_velocity = _number((sentiment.get("metrics") or {}).get("social_volume_velocity_ratio"))
    if manipulation_score is not None and manipulation_score > 55:
        return "Нет сделки: повышен риск манипулятивности/ликвидности."
    if late_entry is not None and late_entry > 45:
        return "Нет сделки: движение выглядит поздним для входа по текущей цене."
    if social_velocity is not None and social_velocity < 1.0:
        return "Нет сделки: LunarCrush mentions не ускоряются относительно базы."
    return final.get("reason_summary") or "Нет сделки: edge недостаточно силён для плана входа, стопа и целей."


def _activation_triggers(
    final: Dict[str, Any],
    sentiment: Dict[str, Any],
    derivatives: Dict[str, Any],
    ta: Dict[str, Any],
    preferred_side: str,
) -> List[str]:
    metrics = sentiment.get("metrics") or {}
    triggers: List[str] = []
    social_ratio = _number(metrics.get("social_volume_velocity_ratio"))
    if social_ratio is None or social_ratio < 1.35:
        triggers.append("Скорость упоминаний LunarCrush вернется выше 1.35x; для сильного сигнала лучше >2.0x.")

    volume_ratio = _ta_signal_detail(final, "volume_spike", "volume_ratio")
    if volume_ratio is None or volume_ratio < 2.0:
        triggers.append("Объем Bybit за 1ч станет минимум в 2 раза выше базы и подтвердит движение.")

    cvd = _number(derivatives.get("cvd_base"))
    if preferred_side == "long" and (cvd is None or cvd <= 0):
        triggers.append("Для лонга дождаться CVD > 0 и удержания цены выше локального VWAP/структуры.")
    if preferred_side == "short" and (cvd is None or cvd >= 0):
        triggers.append("Для шорта дождаться CVD < 0 и lower-high/слома локальной структуры.")

    if ta.get("decision_score", 0) < 25:
        triggers.append("ТА должен дать торговое подтверждение: слом/возврат структуры, снятие ликвидности или RSI+объем в одну сторону.")

    if not triggers:
        triggers.append("Ждать откат или ретест с R:R >= 1:3 и тем же направлением CVD.")
    return triggers


def _primary_risk(
    final: Dict[str, Any],
    sentiment: Dict[str, Any],
    derivatives: Dict[str, Any],
    ta: Dict[str, Any],
) -> Dict[str, str]:
    candidates: List[tuple[float, str, str, str]] = []
    manipulation = _number(final.get("manipulation_score")) or 0.0
    late = _number(final.get("late_entry_risk")) or 0.0
    social_ratio = _number((sentiment.get("metrics") or {}).get("social_volume_velocity_ratio"))
    volume_ratio = _ta_signal_detail(final, "volume_spike", "volume_ratio")
    if manipulation > 0:
        candidates.append((manipulation, "manipulation", "manipulation", "Ликвидность/манипулятивность ухудшает качество исполнения."))
    if late > 0:
        candidates.append((late, "late_entry", "late entry", "Движение уже могло пройти основную импульсную часть."))
    if derivatives.get("cvd_conflict"):
        candidates.append((70.0, "CVD", "CVD", derivatives.get("cvd_conflict_reason") or "CVD конфликтует с направлением сетапа."))
    if social_ratio is None or social_ratio < 1.0:
        candidates.append((55.0, "social", "social", "Упоминания не ускоряются или LunarCrush history неполная."))
    if volume_ratio is None or volume_ratio < 1.0:
        candidates.append((45.0, "volume", "volume", "Bybit объём не подтверждает импульс."))
    if ta.get("dominant_negative"):
        candidates.append((40.0, "TA", "TA", str(ta.get("dominant_negative"))))
    if not candidates:
        return {"key": "edge", "label": "edge", "reason": "Нет одного доминирующего риска; edge просто недостаточно силён."}
    _, key, label, reason = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
    return {"key": key, "label": label, "reason": reason}


def _derivatives_decision_layer(final: Dict[str, Any], preferred_side: str) -> Dict[str, Any]:
    technical = final.get("technical_analysis") or {}
    derivatives = (technical.get("derivatives_filter") or {}).get("metrics") or {}
    cvd = derivatives.get("cvd") or {}
    cvd_base = _number(cvd.get("cvd_base"))
    buy_volume = _number(cvd.get("buy_volume_base"))
    sell_volume = _number(cvd.get("sell_volume_base"))
    cvd_view = _cvd_view(cvd_base, buy_volume, sell_volume, cvd.get("status"))
    cvd_bias = cvd_view["bias"]
    conflict = (preferred_side == "long" and cvd_bias == "negative") or (preferred_side == "short" and cvd_bias == "positive")
    return {
        "status": (technical.get("derivatives_filter") or {}).get("status") or "unavailable",
        "funding_rate": derivatives.get("funding_rate"),
        "open_interest_value": derivatives.get("open_interest_value"),
        "long_ratio": derivatives.get("long_ratio"),
        "short_ratio": derivatives.get("short_ratio"),
        "cvd_status": cvd.get("status") or "unavailable",
        "cvd_base": cvd_base,
        "cvd_buy_volume": buy_volume,
        "cvd_sell_volume": sell_volume,
        "cvd_buy_pct": cvd_view["buy_pct"],
        "cvd_sell_pct": cvd_view["sell_pct"],
        "cvd_bias": cvd_bias,
        "cvd_status_label": cvd_view["status_label"],
        "cvd_conflict": conflict,
        "cvd_conflict_reason": (
            "CVD отрицательный против лонга." if preferred_side == "long" and conflict else
            "CVD положительный против шорта." if preferred_side == "short" and conflict else
            "CVD не конфликтует с предпочитаемой стороной."
        ),
    }


def _final_decision_payload(
    verdict: str,
    preferred_side: str,
    action: str,
    no_trade_reason: str,
    triggers: List[str],
    trade_plan: Dict[str, Any],
) -> Dict[str, Any]:
    side = preferred_side if preferred_side in {"long", "short"} and verdict not in {"AVOID", "NO_SCORE"} else "watch_only"
    return {
        "action": _verdict_label(verdict),
        "side": side,
        "summary": action,
        "entry_text": trade_plan.get("entry") or (triggers[0] if triggers else "Вход не задан: нет подтвержденного сетапа."),
        "stop_text": trade_plan.get("stop_loss") or "Стоп не рассчитываем без подтвержденного входа.",
        "take_profit_text": trade_plan.get("take_profit_1") or trade_plan.get("take_profit") or "Цели не рассчитываем без подтвержденного входа.",
        "no_trade_reason": no_trade_reason,
    }


def _project_trade_block(
    final: Dict[str, Any],
    fundamentals: Dict[str, Any],
    derivatives: Dict[str, Any],
    verdict: str,
    preferred_side: str,
) -> Dict[str, Any]:
    metrics = fundamentals.get("metrics") or {}
    return {
        "tag": _project_tag(metrics, verdict),
        "status": verdict,
        "status_label": _verdict_label(verdict),
        "project_one_liner": metrics.get("project_brief_ru") or metrics.get("project_summary") or "",
        "cvd_summary": _cvd_summary(derivatives, preferred_side),
        "quick_metrics": {
            "cvd": derivatives.get("cvd_base"),
            "cvd_bias": derivatives.get("cvd_bias"),
            "funding_rate": derivatives.get("funding_rate"),
            "long_ratio": derivatives.get("long_ratio"),
            "short_ratio": derivatives.get("short_ratio"),
        },
    }


def _project_tag(metrics: Dict[str, Any], verdict: str) -> str:
    if verdict == "AVOID":
        return "Не торговать"
    blockers = _fundamental_hard_blockers(metrics)
    if blockers:
        return _fundamental_quality_label(metrics, blockers)
    primary_category = _primary_category(metrics)
    if primary_category:
        return "%s-категория" % primary_category
    tier = str(metrics.get("fdv_tier") or "")
    if tier in {"tiny", "small"}:
        return "Малая капитализация"
    sector = str(metrics.get("sector") or metrics.get("narrative") or "").strip()
    if sector:
        return "%s-сектор" % sector.split()[0]
    return "Исследование завершено"


def _cvd_summary(derivatives: Dict[str, Any], preferred_side: str) -> Dict[str, Any]:
    cvd = _number(derivatives.get("cvd_base"))
    buy_volume = _number(derivatives.get("cvd_buy_volume"))
    sell_volume = _number(derivatives.get("cvd_sell_volume"))
    view = _cvd_view(cvd, buy_volume, sell_volume, derivatives.get("cvd_status"))
    if cvd is None:
        return {
            "label": "Нет данных",
            "status_label": "Нет данных",
            "bias": "unknown",
            "explanation": "Источник не вернул recent trades.",
            "value": None,
            "buy_pct": None,
            "sell_pct": None,
            "source": "bybit_recent_trade",
        }
    bias = derivatives.get("cvd_bias") or view["bias"]
    label = view["label"]
    conflict = derivatives.get("cvd_conflict")
    side_text = _side_label(preferred_side).lower()
    explanation = "Рыночные покупки / рыночные продажи%s." % (
        "; конфликтует со стороной %s" % side_text if conflict else ""
    )
    return {
        "label": label,
        "status_label": view["status_label"],
        "bias": bias,
        "explanation": explanation,
        "value": cvd,
        "buy_pct": view["buy_pct"],
        "sell_pct": view["sell_pct"],
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "source": "bybit_recent_trade",
    }


def _cvd_view(cvd: Optional[float], buy_volume: Optional[float], sell_volume: Optional[float], status: Any = None) -> Dict[str, Any]:
    if status == "unavailable" or cvd is None:
        return {"status_label": "Нет данных", "label": "Нет данных", "bias": "unknown", "buy_pct": None, "sell_pct": None}
    total = (buy_volume or 0.0) + (sell_volume or 0.0)
    buy_pct = safe_pct(buy_volume, total)
    sell_pct = safe_pct(sell_volume, total)
    delta_pct = safe_pct(cvd, total)
    if buy_pct is None and cvd is not None and cvd > 0:
        status_label = "Покупатели доминируют"
        bias = "positive"
    elif buy_pct is None and cvd is not None and cvd < 0:
        status_label = "Продавцы доминируют"
        bias = "negative"
    elif buy_pct is not None and (buy_pct >= 55.0 or (delta_pct is not None and delta_pct >= 10.0)):
        status_label = "Покупатели доминируют"
        bias = "positive"
    elif sell_pct is not None and (sell_pct >= 55.0 or (delta_pct is not None and delta_pct <= -10.0)):
        status_label = "Продавцы доминируют"
        bias = "negative"
    else:
        status_label = "Баланс"
        bias = "neutral"
    ratio = "%s%% / %s%%" % (_fmt_score(buy_pct), _fmt_score(sell_pct)) if buy_pct is not None and sell_pct is not None else "нет долей"
    return {
        "status_label": status_label,
        "label": "%s: %s, CVD %s" % (status_label, ratio, _fmt_signed_number(cvd)),
        "bias": bias,
        "buy_pct": buy_pct,
        "sell_pct": sell_pct,
    }


def safe_pct(value: Optional[float], total: float) -> Optional[float]:
    if value is None or total <= 0:
        return None
    return round((value / total) * 100.0, 2)


def _fmt_signed_number(value: Optional[float]) -> str:
    if value is None:
        return "нет данных"
    prefix = "+" if value > 0 else ""
    abs_value = abs(value)
    if abs_value >= 1_000_000:
        body = "%.1fM" % (value / 1_000_000.0)
    elif abs_value >= 1_000:
        body = "%.1fK" % (value / 1_000.0)
    else:
        body = _fmt_score(value)
    return prefix + body if not body.startswith("-") else body


def _fundamental_decision_layer(stage: Dict[str, Any]) -> Dict[str, Any]:
    metrics = stage.get("metrics") or {}
    categories = metrics.get("categories") or []
    if not isinstance(categories, list):
        categories = [str(categories)]
    return {
        "hard_blockers": _fundamental_hard_blockers(metrics),
        "context_only": [
            item for item in [
                "Sector: %s" % metrics.get("sector") if metrics.get("sector") else None,
                "Chain/ecosystem: %s" % metrics.get("chain_ecosystem") if metrics.get("chain_ecosystem") else None,
                "Categories: %s" % ", ".join(str(item) for item in categories[:5]) if categories else None,
                metrics.get("project_brief_ru") or metrics.get("project_summary"),
            ]
            if item
        ],
        "label": metrics.get("fundamental_label"),
        "reason": metrics.get("fundamental_label_reason"),
    }


def _fundamental_trade_block(stage: Dict[str, Any]) -> Dict[str, Any]:
    metrics = stage.get("metrics") or {}
    blockers = _fundamental_hard_blockers(metrics)
    quality_label = _fundamental_quality_label(metrics, blockers)
    if any(_hard_blocker_is_critical(item) for item in blockers):
        verdict = "blocker"
        trade_impact = ""
    elif blockers or str(stage.get("status") or "") == "warn":
        verdict = "risk"
        trade_impact = ""
    else:
        verdict = "ok"
        trade_impact = ""
    return {
        "verdict": verdict,
        "verdict_label": quality_label,
        "tag": quality_label,
        "status_help": "",
        "summary": metrics.get("project_brief_ru") or metrics.get("project_summary") or "Описание проекта пока недоступно.",
        "blockers": blockers,
        "reasons": _fundamental_reasons(metrics, blockers, verdict),
        "trade_impact": trade_impact,
    }


def _fundamental_tag(metrics: Dict[str, Any], blockers: List[str], verdict: str) -> str:
    quality_label = _fundamental_quality_label(metrics, blockers)
    if quality_label != "Средний фундаментал":
        return quality_label
    primary_category = _primary_category(metrics)
    if primary_category:
        return "%s-категория" % primary_category
    tier = str(metrics.get("fdv_tier") or "")
    if tier in {"tiny", "small"}:
        return "Малая капитализация"
    sector = str(metrics.get("sector") or metrics.get("narrative") or "").strip()
    if sector:
        return "%s-сектор" % sector.split()[0]
    return quality_label


def _primary_category(metrics: Dict[str, Any]) -> Optional[str]:
    value = str(metrics.get("primary_category") or "").strip()
    if value:
        return value
    categories = metrics.get("categories") or []
    if not isinstance(categories, list):
        categories = [str(categories)]
    normalized = [str(item).strip() for item in categories if str(item).strip()]
    meaningful = [item for item in normalized if not _is_noise_category(item)]
    candidates = meaningful or normalized
    priority = [
        ("Meme", ("meme",)),
        ("AI", ("artificial intelligence", "ai")),
        ("RWA", ("real world assets", "rwa")),
        ("Privacy", ("privacy", "zero knowledge", "zk")),
        ("DePIN", ("depin",)),
        ("Infrastructure", ("infrastructure", "modular blockchain", "data availability")),
        ("Gaming/GameFi", ("gaming", "gamefi", "play to earn")),
        ("DeFi", ("defi", "decentralized finance", "dex", "lending", "yield")),
    ]
    for label, terms in priority:
        for category in candidates:
            if any(_category_matches_term(category, term) for term in terms):
                return label
    for category in candidates:
        if not _is_noise_category(category):
            return category
    return normalized[0] if normalized else None


def _category_matches_term(category: str, term: str) -> bool:
    lowered = category.lower()
    if term == "ai":
        return bool(re.search(r"\bai\b", lowered))
    return term in lowered


def _is_noise_category(category: str) -> bool:
    lowered = category.lower()
    return any(term in lowered for term in ("ecosystem", "chain", "airdrop", "portfolio", "made in", "hodler", "binance", "solana", "ethereum", "base", "abstract", "layer 1", "layer 2"))


def _fundamental_quality_label(metrics: Dict[str, Any], blockers: List[str]) -> str:
    score = 50.0
    sector_blob = " ".join(
        str(item)
        for item in [
            metrics.get("primary_category"),
            metrics.get("sector"),
            metrics.get("narrative"),
            ", ".join(str(value) for value in (metrics.get("categories") or []))
            if isinstance(metrics.get("categories"), list)
            else metrics.get("categories"),
        ]
        if item
    ).lower()
    if any(term in sector_blob for term in ("ai", "artificial intelligence", "rwa", "real world", "privacy", "depin")):
        score += 25.0
    if any(term in sector_blob for term in ("gaming", "gamefi", "defi", "meme")):
        score -= 18.0
    supply = _number(metrics.get("circulating_supply_ratio"))
    if supply is not None:
        if supply >= 0.55:
            score += 10.0
        elif supply < 0.30:
            score -= 12.0
    mc_fdv = _number(metrics.get("market_cap_to_fdv_ratio"))
    if mc_fdv is not None:
        if mc_fdv >= 0.45:
            score += 10.0
        elif mc_fdv < 0.20:
            score -= 10.0
    fdv_tier = str(metrics.get("fdv_tier") or "")
    if fdv_tier in {"small", "mid", "large"}:
        score += 5.0
    if fdv_tier in {"tiny", "giant"}:
        score -= 8.0
    tokenomics = _number(metrics.get("tokenomics_risk_score"))
    if tokenomics is not None and tokenomics >= 65:
        score -= 8.0
    critical_blockers = [item for item in blockers if _hard_blocker_is_critical(item)]
    if critical_blockers:
        score -= 12.0
    if any(term in " ".join(blockers).lower() for term in ("scam", "rug", "blacklist")):
        score -= 30.0
    if score >= 68.0:
        return "Сильный фундаментал"
    if score >= 42.0:
        return "Средний фундаментал"
    return "Слабый фундаментал"


def _fundamental_reasons(metrics: Dict[str, Any], blockers: List[str], verdict: str) -> List[str]:
    reasons = [item.rstrip(".") + "." for item in blockers[:2]]
    if reasons:
        return reasons
    tier_label = metrics.get("fdv_tier_label")
    if tier_label:
        reasons.append("Размер проекта: %s." % tier_label)
    ratio = _number(metrics.get("market_cap_to_fdv_ratio"))
    if ratio is not None:
        reasons.append("Доля market cap к FDV: %s%%." % _fmt_score(ratio * 100))
    if not reasons:
        reasons.append("Критичных фундаментальных ограничений не найдено." if verdict == "ok" else "Фундаментал требует осторожности.")
    return reasons[:2]


def _hard_blocker_is_critical(value: str) -> bool:
    lowered = value.lower()
    return any(term in lowered for term in ("unlock", "vesting", "scam", "rug", "blacklist", "fdv", "циркуляция", "supply"))


def _fundamental_hard_blockers(metrics: Dict[str, Any]) -> List[str]:
    blockers: List[str] = []
    unlock_label = str(metrics.get("unlock_risk_label") or "")
    if "есть" in unlock_label.lower():
        blockers.append("Упоминается unlock/vesting: нужна ручная проверка даты и размера.")
    tokenomics = _number(metrics.get("tokenomics_risk_score"))
    if tokenomics is not None and tokenomics >= 65:
        blockers.append("Токеномика требует ручной проверки.")
    supply = _number(metrics.get("circulating_supply_ratio"))
    if supply is not None and supply < 0.30:
        blockers.append("Циркуляция ниже 30% от общего или максимального предложения.")
    fdv_tier = str(metrics.get("fdv_tier") or "")
    if fdv_tier in {"tiny", "giant"}:
        blockers.append("Экстремальный FDV: %s." % (metrics.get("fdv_tier_label") or fdv_tier))
    red_flags = [str(item) for item in (metrics.get("red_flags") or [])]
    for flag in red_flags:
        lowered = flag.lower()
        if any(term in lowered for term in ("scam", "rug", "blacklist")):
            blockers.append(flag)
    return blockers


def _ta_decision_layer(final: Dict[str, Any], preferred_side: str) -> Dict[str, Any]:
    signals = ((final.get("technical_analysis") or {}).get("signals") or {})
    positives: List[Dict[str, Any]] = []
    negatives: List[Dict[str, Any]] = []

    def add(target: List[Dict[str, Any]], key: str, label: str, weight: float) -> None:
        target.append({"key": key, "label": label, "weight": weight})

    structure = _signal_value(signals, "structure_break_hh_hl")
    rsi = _signal_value(signals, "rsi_signal")
    ema = _signal_value(signals, "ema_cross")
    volume_spike = _signal_value(signals, "volume_spike")
    breakout = _signal_value(signals, "breakout_20d_high")
    atr = _signal_value(signals, "atr_volatility_expansion")
    divergence = _signal_value(signals, "rsi_divergence")
    squeeze = _signal_value(signals, "bollinger_squeeze")

    if preferred_side == "long":
        if structure in {"bullish_hh_hl", "bullish_sweep_reclaim"}:
            add(positives, "structure", "Бычья структура или возврат после снятия ликвидности", 30)
        if volume_spike is True:
            add(positives, "volume", "Объем подтверждает движение", 22)
        if breakout is True:
            add(positives, "breakout", "Пробой 20-дневного диапазона", 18)
        if rsi in {"bullish", "oversold"}:
            add(positives, "rsi", "RSI поддерживает лонг-тайминг", 12)
        if ema in {"bullish_cross", "bullish"}:
            add(positives, "ema", "EMA бычья, но это слабый самостоятельный сигнал", 6)
        if structure == "bearish_break":
            add(negatives, "structure", "Медвежий слом структуры сильнее мягких бычьих сигналов", 32)
        if divergence == "bearish":
            add(negatives, "divergence", "Медвежья дивергенция RSI", 18)
        if volume_spike is not True:
            add(negatives, "volume", "Нет подтверждения объемом", 14)
    elif preferred_side == "short":
        if structure == "bearish_break":
            add(positives, "structure", "Медвежий слом структуры", 30)
        if divergence == "bearish":
            add(positives, "divergence", "Медвежья дивергенция RSI", 20)
        if rsi == "overbought":
            add(positives, "rsi", "RSI в перегреве поддерживает ожидание шорта", 16)
        if volume_spike is True:
            add(positives, "volume", "Всплеск объема на экстремуме движения", 12)
        if structure in {"bullish_hh_hl", "bullish_sweep_reclaim"}:
            add(negatives, "structure", "Бычья структура конфликтует с шортом", 30)
        if ema in {"bullish_cross", "bullish"} and structure != "bearish_break":
            add(negatives, "ema", "EMA еще бычья; для шорта нужен сильный слом структуры или CVD", 10)
    else:
        if volume_spike is not True:
            add(negatives, "volume", "Нет подтверждения объемом", 14)

    if atr is True:
        add(positives, "atr", "ATR расширяется: волатильность есть, но направление нужно подтверждать", 8)
    if squeeze is True:
        add(positives, "squeeze", "Сжатие Bollinger: готовиться, но ждать подтверждения направления", 6)

    positive_score = sum(item["weight"] for item in positives)
    negative_score = sum(item["weight"] for item in negatives)
    return {
        "preferred_side": preferred_side,
        "decision_score": round(positive_score - negative_score, 2),
        "positive_score": round(positive_score, 2),
        "negative_score": round(negative_score, 2),
        "positives": positives,
        "negatives": negatives,
        "dominant_positive": positives[0]["label"] if positives else None,
        "dominant_negative": negatives[0]["label"] if negatives else None,
        "summary": _ta_summary(preferred_side, positive_score, negative_score, positives, negatives),
    }


def _ta_summary(preferred_side: str, positive_score: float, negative_score: float, positives: List[Dict[str, Any]], negatives: List[Dict[str, Any]]) -> str:
    if negative_score >= positive_score and negatives:
        return "%s не активен: %s." % (_side_label(preferred_side), negatives[0]["label"])
    if positive_score >= 35:
        return "%s имеет торговое подтверждение." % _side_label(preferred_side)
    if positives:
        return "%s слабый: есть %s, но не хватает сильного структурного/объемного подтверждения." % (_side_label(preferred_side), positives[0]["label"])
    return "%s без торгового подтверждения по ТА." % _side_label(preferred_side)


def _side_label(side: str) -> str:
    return "Лонг" if side == "long" else "Шорт" if side == "short" else "Нейтрально"


def _ta_trade_block(
    ta: Dict[str, Any],
    derivatives: Dict[str, Any],
    preferred_side: str,
    setup_reason: str,
    final: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    score = _number(ta.get("decision_score")) or 0.0
    conflict = bool(derivatives.get("cvd_conflict"))
    if conflict:
        verdict = "conflict"
        label = "Конфликт"
        impact = "Не входить: CVD конфликтует с направлением сделки."
    elif score >= 25:
        verdict = "long_confirmed" if preferred_side == "long" else "short_confirmed"
        label = "Лонг подтвержден" if preferred_side == "long" else "Шорт подтвержден"
        impact = "ТА допускает сделку только при выполнении условий входа."
    else:
        verdict = "no_confirmation"
        label = "Нет подтверждения"
        impact = "ТА не дает достаточно сильной точки входа."
    supports = [str(item.get("label")) for item in ta.get("positives") or [] if item.get("label")]
    conflicts = [str(item.get("label")) for item in ta.get("negatives") or [] if item.get("label")]
    if conflict:
        conflicts.insert(0, str(derivatives.get("cvd_conflict_reason") or "CVD конфликтует с направлением."))
    return {
        "verdict": verdict,
        "verdict_label": label,
        "tag": _ta_tag(verdict, preferred_side),
        "strategy_label": _ta_tag(verdict, preferred_side),
        "cvd_summary": _cvd_summary(derivatives, preferred_side),
        "summary": ta.get("summary") or "ТА пока не дала отдельного вывода.",
        "trend_summary": _ta_trend_summary(final or {}),
        "levels_summary": _ta_levels_summary(final or {}),
        "indicator_summary": _ta_indicator_summary(final or {}),
        "setup": _ta_setup_payload(final or {}),
        "conditions": _ta_entry_conditions(preferred_side, derivatives),
        "supports": supports[:3],
        "conflicts": conflicts[:3],
        "entry_conditions": _ta_entry_conditions(preferred_side, derivatives),
        "invalidation": setup_reason if setup_reason and "Нет сетапа" not in setup_reason else "Инвалидация: отменить идею при сломе локальной структуры против выбранной стороны.",
        "technical_context": _ta_technical_context(final or {}),
        "trade_map": _ta_trade_map(preferred_side, setup_reason, final or {}),
        "trade_impact": impact,
        "terms": [
            "CVD — разница рыночных покупок и продаж; положительный CVD поддерживает лонг, отрицательный поддерживает шорт.",
            "ATR — текущая волатильность; помогает оценить расстояние до стопа.",
            "RSI — индикатор перегрева/перепроданности, сам по себе не является входом.",
        ],
    }


def _ta_tag(verdict: str, preferred_side: str) -> str:
    if verdict == "conflict":
        return "Конфликт сигналов"
    if verdict == "long_confirmed":
        return "Лонг после ретеста"
    if verdict == "short_confirmed":
        return "Шорт от структуры"
    return "Нет подтверждения"


def _ta_entry_conditions(preferred_side: str, derivatives: Dict[str, Any]) -> List[str]:
    conditions = []
    cvd = _number(derivatives.get("cvd_base"))
    if preferred_side == "short":
        conditions.append("Дождаться lower-high или слома локальной структуры вниз.")
        if cvd is None or cvd >= 0:
            conditions.append("CVD должен показать доминирование продавцов.")
    elif preferred_side == "long":
        conditions.append("Дождаться возврата/ретеста уровня без провала структуры.")
        if cvd is None or cvd <= 0:
            conditions.append("CVD должен показать доминирование покупателей.")
    else:
        conditions.append("Сначала выбрать сторону: дождаться либо lower-high/слома вниз, либо возврата структуры вверх.")
        conditions.append("CVD и объем должны подтвердить выбранное направление.")
    return conditions


def _ta_technical_context(final: Dict[str, Any]) -> Dict[str, str]:
    technical = final.get("technical_analysis") or {}
    mtf = technical.get("multi_timeframe") or {}
    levels = technical.get("levels") or {}
    if mtf:
        tf4 = mtf.get("240") or {}
        tf1 = mtf.get("60") or {}
        tf15 = mtf.get("15") or {}
        return {
            "structure": "%s тренд: %s; %s структура: %s" % (
                tf4.get("label") or "4ч",
                _trend_ru(tf4.get("trend")),
                tf1.get("label") or "1ч",
                _structure_mtf_ru(tf1.get("structure")),
            ),
            "rsi": _rsi_mtf_text(tf1, tf15),
            "rsi_divergence": _divergence_mtf_text(mtf),
            "levels": _levels_mtf_text(levels),
            "volume": _volume_mtf_text(tf15, tf1),
            "atr": _atr_mtf_text(tf15, tf1),
        }
    signals = (technical.get("signals") or {})
    structure = _signal_value(signals, "structure_break_hh_hl")
    rsi = _signal_value(signals, "rsi_signal")
    divergence = _signal_value(signals, "rsi_divergence")
    breakout = _signal_value(signals, "breakout_20d_high")
    volume = _signal_value(signals, "volume_spike")
    atr = _signal_value(signals, "atr_volatility_expansion")
    return {
        "structure": _structure_signal_ru(structure),
        "rsi": _rsi_signal_ru(rsi),
        "rsi_divergence": _divergence_signal_ru(divergence),
        "levels": _level_signal_ru(breakout, structure),
        "volume": "есть всплеск объема" if volume is True else "нет подтверждения объемом" if volume is False else "объем не оценен",
        "atr": "ATR расширяется: стоп считать шире обычного" if atr is True else "ATR без расширения" if atr is False else "ATR не оценен",
    }


def _ta_trade_map(preferred_side: str, setup_reason: str, final: Dict[str, Any]) -> Dict[str, str]:
    technical = final.get("technical_analysis") or {}
    setup = technical.get("trade_setup") or {}
    if setup:
        if setup.get("status") == "candidate" and setup.get("risk_reward") is not None:
            take_profits = setup.get("take_profits") or []
            return {
                "entry": "ТВХ %s: %s" % (_side_label(str(setup.get("side") or preferred_side)).lower(), _fmt_price(setup.get("entry"))),
                "stop": "SL: %s" % _fmt_price(setup.get("stop_loss")),
                "take_profit": "TP: %s; R:R %s" % (_fmt_price(take_profits[0] if take_profits else None), _fmt_score(setup.get("risk_reward"))),
                "checklist": "Перед входом: дождаться ретеста на младшем ТФ, подтверждения объема и CVD в сторону сделки.",
                "invalidation_note": str(setup.get("reason") or ""),
            }
        return {
            "entry": "Нет активной ТВХ: %s" % (setup.get("reason") or "уровни или структура не дают R:R >= 1:3."),
            "stop": "SL не фиксируем без активной ТВХ.",
            "take_profit": "TP не фиксируем без цели с R:R >= 1:3.",
            "checklist": "Перед входом: старший ТФ, уровень, RSI/дивергенция, объем и CVD должны совпасть.",
            "invalidation_note": str(setup.get("reason") or ""),
        }
    trade_plan = final.get("trade_plan") or {}
    if preferred_side == "short":
        fallback_entry = "Ждать lower-high, слом локальной поддержки и отрицательный CVD."
        fallback_stop = "SL выше lower-high или зоны возврата цены над сломанной структурой."
        fallback_tp = "TP у ближайшей поддержки/зоны ликвидности; сетап активен только при R:R >= 1:3."
    elif preferred_side == "long":
        fallback_entry = "Ждать ретест уровня, удержание структуры и положительный CVD."
        fallback_stop = "SL ниже ретеста/свипа с учетом ATR."
        fallback_tp = "TP у локального high/зоны ликвидности; сетап активен только при R:R >= 1:3."
    else:
        fallback_entry = "Сначала выбрать сторону: структура, объем и CVD должны совпасть."
        fallback_stop = "SL ставить за локальный экстремум после подтвержденной ТВХ."
        fallback_tp = "TP строить от ближайшей поддержки/сопротивления; без ТВХ цели не фиксируем."
    return {
        "entry": trade_plan.get("entry") or fallback_entry,
        "stop": trade_plan.get("stop_loss") or fallback_stop,
        "take_profit": trade_plan.get("take_profit_1") or trade_plan.get("take_profit") or fallback_tp,
        "checklist": "Перед входом: RSI/дивергенции, структура старшего ТФ, локальные поддержка/сопротивление, объем и CVD.",
        "invalidation_note": setup_reason or "",
    }


def _ta_setup_payload(final: Dict[str, Any]) -> Dict[str, Any]:
    technical = final.get("technical_analysis") or {}
    setup = technical.get("trade_setup") or {}
    if not isinstance(setup, dict):
        return {}
    return setup


def _ta_trend_summary(final: Dict[str, Any]) -> str:
    mtf = ((final.get("technical_analysis") or {}).get("multi_timeframe") or {})
    if not mtf:
        return ""
    tf4 = mtf.get("240") or {}
    tf1 = mtf.get("60") or {}
    return "%s тренд: %s; %s структура: %s." % (
        tf4.get("label") or "4ч",
        _trend_ru(tf4.get("trend")),
        tf1.get("label") or "1ч",
        _structure_mtf_ru(tf1.get("structure")),
    )


def _ta_levels_summary(final: Dict[str, Any]) -> str:
    levels = ((final.get("technical_analysis") or {}).get("levels") or {})
    return _levels_mtf_text(levels)


def _ta_indicator_summary(final: Dict[str, Any]) -> str:
    mtf = ((final.get("technical_analysis") or {}).get("multi_timeframe") or {})
    if not mtf:
        return ""
    return "%s; %s; %s." % (_rsi_mtf_text(mtf.get("60") or {}, mtf.get("15") or {}), _divergence_mtf_text(mtf), _atr_mtf_text(mtf.get("15") or {}, mtf.get("60") or {}))


def _trend_ru(value: Any) -> str:
    return {
        "uptrend": "восходящий",
        "downtrend": "нисходящий",
        "range": "боковик",
    }.get(str(value or ""), "нет данных")


def _structure_mtf_ru(value: Any) -> str:
    return {
        "higher_high_higher_low": "HH/HL, бычья структура",
        "lower_high_lower_low": "LH/LL, медвежья структура",
        "range": "диапазон",
    }.get(str(value or ""), "нет данных")


def _rsi_mtf_text(tf1: Dict[str, Any], tf15: Dict[str, Any]) -> str:
    rows = []
    for data in (tf1, tf15):
        rsi_payload = data.get("rsi") or {}
        value = rsi_payload.get("value")
        if value is not None:
            rows.append("RSI %s: %s, %s" % (rsi_payload.get("timeframe") or data.get("label") or "ТФ", _fmt_score(value), rsi_payload.get("zone") or "нет зоны"))
    return "; ".join(rows) if rows else "RSI: нет данных"


def _divergence_mtf_text(mtf: Dict[str, Dict[str, Any]]) -> str:
    parts = []
    for key in ("240", "60", "15"):
        data = mtf.get(key) or {}
        divergence = data.get("divergence") or {}
        label = divergence.get("timeframe") or data.get("label") or key
        value = str(divergence.get("value") or "none")
        parts.append("RSI divergence %s: %s" % (label, _divergence_signal_ru(value)))
    return "; ".join(parts) if parts else "RSI divergence: нет данных"


def _levels_mtf_text(levels: Dict[str, Any]) -> str:
    supports = [value for value in levels.get("supports") or [] if value is not None]
    resistances = [value for value in levels.get("resistances") or [] if value is not None]
    strong = levels.get("strong_levels") or []
    support = supports[-1] if supports else None
    resistance = resistances[0] if resistances else None
    suffix = ""
    if strong:
        suffix = "; сильный уровень: %s" % _fmt_price((strong[0] or {}).get("price"))
    if support is None and resistance is None:
        return "Уровни: нет числовых support/resistance"
    return "Поддержка: %s; сопротивление: %s%s" % (_fmt_price(support), _fmt_price(resistance), suffix)


def _volume_mtf_text(tf15: Dict[str, Any], tf1: Dict[str, Any]) -> str:
    for data in (tf15, tf1):
        volume = data.get("volume") or {}
        if volume.get("ratio") is not None:
            return "Объем %s: %sx к базе" % (data.get("label") or volume.get("timeframe") or "ТФ", _fmt_score(volume.get("ratio")))
    return "Объем: нет данных"


def _atr_mtf_text(tf15: Dict[str, Any], tf1: Dict[str, Any]) -> str:
    for data in (tf15, tf1):
        atr_payload = data.get("atr") or {}
        if atr_payload.get("value") is not None:
            return "ATR %s: %s (%s%% цены)" % (atr_payload.get("timeframe") or data.get("label") or "ТФ", _fmt_price(atr_payload.get("value")), _fmt_score(atr_payload.get("pct")))
    return "ATR: нет данных"


def _fmt_price(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "нет данных"
    if abs(number) >= 100:
        return ("%.2f" % number).rstrip("0").rstrip(".")
    if abs(number) >= 1:
        return ("%.4f" % number).rstrip("0").rstrip(".")
    return ("%.8f" % number).rstrip("0").rstrip(".")


def _structure_signal_ru(value: Any) -> str:
    return {
        "bearish_break": "слом структуры вниз",
        "bullish_hh_hl": "структура higher-high / higher-low",
        "bullish_sweep_reclaim": "снятие ликвидности и возврат",
    }.get(str(value), "структура: нет подтвержденного HH/HL или LH/LL")


def _rsi_signal_ru(value: Any) -> str:
    return {
        "bullish": "RSI поддерживает лонг",
        "bearish": "RSI поддерживает шорт",
        "overbought": "RSI в перегреве",
        "oversold": "RSI в перепроданности",
    }.get(str(value), "RSI нейтрален")


def _divergence_signal_ru(value: Any) -> str:
    return {"bearish": "медвежья", "bullish": "бычья", "none": "нет"}.get(str(value), "нет")


def _level_signal_ru(breakout: Any, structure: Any) -> str:
    if breakout is True:
        return "пробит 20D high; вход только после ретеста"
    if structure == "bearish_break":
        return "lower-high как зона входа, ближайшая поддержка как первая цель"
    if structure in {"bullish_hh_hl", "bullish_sweep_reclaim"}:
        return "ретест локальной поддержки как зона входа"
    return "ждать понятный локальный high/low перед расчетом ТВХ"


def _chart_next_step(stage: Dict[str, Any], final: Dict[str, Any]) -> str:
    scenario = ((stage.get("metrics") or {}).get("scenario") or {})
    code = scenario.get("code")
    preferred_side = _preferred_side(final, {}, stage, {}) if final else "neutral"
    if code in {"narrative", "organic_growth", "strong_signal"}:
        return "Соцсигнал ведет рынок: ждать объем Bybit выше 2.0x к базе и CVD в сторону %s." % _side_label(preferred_side)
    if code in {"fake_pump", "exhaustion_late_hype"}:
        return "Соцсигнал запаздывает: не догонять; для short ждать lower-high и CVD < 0."
    if code == "insider_pump":
        return "Цена/объём идут раньше соцсетей: торговать только после ретеста, без FOMO."
    if code == "insufficient_social_data":
        return "LunarCrush hourly history неполная: опираться на Bybit volume/CVD и не повышать conviction из соцблока."
    if preferred_side == "short":
        return "Смешанная картина: для шорта ждать lower-high, объем продавца >1.5x к базе и CVD < 0."
    return "Смешанная картина: для лонга ждать возврата объема >2.0x к базе, CVD > 0 и удержания структуры."


def _social_trade_block(
    sentiment: Dict[str, Any],
    research_charts: Dict[str, Any],
    fundamentals: Dict[str, Any],
    preferred_side: str,
    derivatives: Dict[str, Any],
) -> Dict[str, Any]:
    social_metrics = sentiment.get("metrics") or {}
    chart_metrics = research_charts.get("metrics") or {}
    scenario = chart_metrics.get("scenario") or {}
    fundamental_metrics = fundamentals.get("metrics") or {}
    velocity = _number(social_metrics.get("social_volume_velocity_ratio"))
    price_change = _number(fundamental_metrics.get("price_change_24h"))
    status = _social_status(scenario.get("code"), velocity, price_change)
    scenario_ru = _scenario_ru(status["code"])
    if scenario.get("code") in {"narrative", "early_narrative"} and velocity is not None and velocity >= 1.35:
        verdict = "ok"
        impact = "Социальный импульс можно учитывать, если Bybit объем подтвердит движение."
    elif status["code"] in {"growth_without_social_confirmation", "correction_without_social_panic", "late_hype"}:
        verdict = "risk"
        impact = "Соцблок не дает самостоятельного входа: ждать синхронизации с объемом и структурой."
    elif velocity is not None and velocity < 1.0:
        verdict = "risk"
        impact = "Упоминания ниже базы: соцблок не усиливает сделку."
    else:
        verdict = "watch"
        impact = "Соцблок нейтрален: ждать синхронизации упоминаний, цены и объема."
    posts = _structured_social_posts(fundamental_metrics)
    return {
        "verdict": verdict,
        "verdict_label": {"ok": "Подтверждает", "risk": "Риск", "watch": "Наблюдать"}[verdict],
        "status_code": status["code"],
        "status_label": scenario_ru["label"],
        "status_reason": status["reason"],
        "tag": scenario_ru["label"],
        "scenario_label_ru": scenario_ru["label"],
        "summary": status["reason"] or scenario_ru["summary"],
        "chart_explanation": (
            "Все линии нормализованы к шкале 0-100 внутри выбранного окна; это не цена в USDT и не абсолютный объем. "
            "Горизонтальная ось — часы. Маркеры M/P/V показывают первый значимый всплеск упоминаний, цены и объема."
        ),
        "metrics_explanation": {
            "mentions": "Упоминания — число постов/упоминаний LunarCrush за период.",
            "baseline": "База — обычный уровень упоминаний, с которым сравниваем текущий час.",
            "velocity": "Скорость — во сколько раз текущие упоминания выше или ниже базы.",
            "window": "Окно — период сравнения, обычно 1 час для velocity и до 48 часов для графика.",
        },
        "translated_posts": [str(item.get("translated_ru") or item.get("original_text")) for item in posts[:5] if item.get("translated_ru") or item.get("original_text")],
        "top_posts": posts[:5],
        "top_posts_summary_ru": fundamental_metrics.get("top_posts_summary_ru") or "",
        "alt_rank": _alt_rank_payload(fundamental_metrics),
        "trade_impact": _social_next_step(research_charts) if chart_metrics else impact,
        "velocity_ratio": velocity,
        "velocity_level": _velocity_level(velocity),
        "current_mentions": social_metrics.get("social_volume_current") or social_metrics.get("social_volume_24h"),
        "baseline_mentions": social_metrics.get("social_volume_baseline"),
        "baseline_label": "База упоминаний",
        "window": social_metrics.get("social_volume_timeframe") or "%sч" % chart_metrics.get("window_hours") if chart_metrics.get("window_hours") else None,
        "window_label": "Окно замера",
        "preferred_side": preferred_side,
    }


def _social_status(code: Any, velocity: Optional[float], price_change: Optional[float]) -> Dict[str, str]:
    base = str(code or "mixed")
    velocity_text = "нет данных" if velocity is None else "%.2fx к базе" % velocity
    price_text = "нет данных" if price_change is None else "%+.2f%% за 24ч" % price_change
    if base == "early_narrative":
        return {"code": "early_social_signal", "reason": "Упоминания растут раньше цены/объема; цена %s, скорость упоминаний %s." % (price_text, velocity_text)}
    if base == "narrative":
        return {"code": "confirmed_narrative", "reason": "Упоминания пришли первыми, затем цена и объем подтвердили движение; цена %s, скорость %s." % (price_text, velocity_text)}
    if base == "insider_pump":
        return {"code": "price_before_social", "reason": "Цена двинулась раньше публичного внимания; цена %s, скорость упоминаний %s." % (price_text, velocity_text)}
    if base == "exhaustion_late_hype":
        return {"code": "late_hype", "reason": "Соцсети догоняют уже прошедшее движение; цена %s, скорость упоминаний %s." % (price_text, velocity_text)}
    if base == "fake_pump" and price_change is not None and price_change < 0:
        return {"code": "correction_without_social_panic", "reason": "Цена корректируется без соцпаники; цена %s, скорость упоминаний %s." % (price_text, velocity_text)}
    if base == "fake_pump":
        return {"code": "growth_without_social_confirmation", "reason": "Цена растет без нормального подтверждения упоминаниями и объемом; цена %s, скорость %s." % (price_text, velocity_text)}
    if base == "insufficient_social_data":
        return {"code": "insufficient_social_data", "reason": "LunarCrush не дал достаточную часовую историю; цена %s." % price_text}
    if base == "insufficient_market_data":
        return {"code": "insufficient_market_data", "reason": "Bybit-истории недостаточно для честного сравнения цены, объема и упоминаний."}
    return {"code": "mixed", "reason": "Нет чистого лидерства соцсетей, цены или объема; цена %s, скорость упоминаний %s." % (price_text, velocity_text)}


def _alt_rank_payload(metrics: Dict[str, Any]) -> Dict[str, Any]:
    value = _number(metrics.get("alt_rank"))
    return {
        "value": int(value) if value is not None else None,
        "captured_at": metrics.get("alt_rank_captured_at"),
        "source": metrics.get("alt_rank_source") or ("lunarcrush" if value is not None else "нет данных"),
    }


def _structured_social_posts(metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
    structured = metrics.get("top_posts_structured") or []
    if isinstance(structured, list) and structured:
        return [item for item in structured if isinstance(item, dict)]
    originals = metrics.get("top_posts") or []
    translated = metrics.get("top_posts_ru") or []
    posts: List[Dict[str, Any]] = []
    for index, original in enumerate(originals[:5]):
        posts.append({
            "original_text": str(original),
            "translated_ru": str(translated[index]) if index < len(translated) else "",
            "creator": "",
            "url": "",
        })
    return posts


def _velocity_level(value: Optional[float]) -> str:
    if value is None:
        return "нет данных"
    if value >= 1.75:
        return "высокая"
    if value >= 1.05:
        return "умеренная"
    return "низкая"


def _social_next_step(stage: Dict[str, Any]) -> str:
    scenario = ((stage.get("metrics") or {}).get("scenario") or {})
    code = scenario.get("code")
    if code in {"narrative", "early_narrative"}:
        return "Соцсигнал можно учитывать только после подтверждения объёмом Bybit и ретеста."
    if code == "fake_pump":
        return "Не торговать только по соцсигналу: определить, это рост без подтверждения или коррекция без паники."
    if code == "exhaustion_late_hype":
        return "Не догонять движение: соцсигнал запаздывает."
    if code == "insider_pump":
        return "Цена пришла раньше публичного внимания: ждать ретест, не входить по FOMO."
    if code == "insufficient_social_data":
        return "LunarCrush history неполная: соцблок не повышает уверенность."
    return "Соцкартина смешанная: ждать согласованного роста упоминаний, цены и объёма."


def _scenario_ru(code: Any) -> Dict[str, str]:
    return {
        "early_social_signal": {
            "label": "Соцсигнал раньше рынка",
            "summary": "Упоминания растут раньше цены и объема. Это watch-сигнал до подтверждения рынком.",
        },
        "confirmed_narrative": {
            "label": "Нарратив подтвержден",
            "summary": "Упоминания пришли первыми, затем цена и объем подтвердили движение.",
        },
        "price_before_social": {
            "label": "Цена раньше соцсетей",
            "summary": "Цена двинулась раньше публичного внимания; вход только после ретеста.",
        },
        "growth_without_social_confirmation": {
            "label": "Рост без соцподтверждения",
            "summary": "Цена растет без нормального подтверждения упоминаниями и объемом.",
        },
        "correction_without_social_panic": {
            "label": "Коррекция без соцпаники",
            "summary": "Цена корректируется, а упоминания не ускоряются; это не памп, а слабая social-реакция.",
        },
        "late_hype": {
            "label": "Поздний хайп",
            "summary": "Соцсети догоняют уже прошедшее движение. Риск позднего входа высокий.",
        },
        "early_narrative": {
            "label": "Соцсигнал раньше рынка",
            "summary": "Упоминания растут раньше цены и объема. Это сигнал для наблюдения, вход только после подтверждения рынком.",
        },
        "narrative": {
            "label": "Нарратив подтвержден",
            "summary": "Упоминания пришли первыми, затем цена и объем подтвердили движение.",
        },
        "exhaustion_late_hype": {
            "label": "Поздний хайп",
            "summary": "Цена уже прошла движение, а соцсети догоняют. Риск позднего входа высокий.",
        },
        "fake_pump": {
            "label": "Рост без соцподтверждения",
            "summary": "Цена двинулась без нормального подтверждения объемом и соцсетями.",
        },
        "insider_pump": {
            "label": "Цена раньше соцсетей",
            "summary": "Цена и объем сдвинулись раньше публичного внимания. Нужен ретест, без FOMO.",
        },
        "insufficient_social_data": {
            "label": "Мало соцданных",
            "summary": "LunarCrush не дал достаточную часовую историю; соцблок не повышает уверенность.",
        },
        "insufficient_market_data": {
            "label": "Мало рыночных данных",
            "summary": "Bybit-истории недостаточно для честного сравнения цены, объема и упоминаний.",
        },
        "mixed": {
            "label": "Смешанная картина",
            "summary": "Нет чистого лидерства соцсетей, цены или объема. Нужны дополнительные подтверждения.",
        },
    }.get(str(code or "mixed"), {
        "label": "Смешанная картина",
        "summary": "Нет чистого лидерства соцсетей, цены или объема. Нужны дополнительные подтверждения.",
    })


def _ta_signal_detail(final: Dict[str, Any], signal_name: str, detail_name: str) -> Optional[float]:
    signal = (((final.get("technical_analysis") or {}).get("signals") or {}).get(signal_name) or {})
    return _number(signal.get(detail_name))


def _signal_value(signals: Dict[str, Any], name: str) -> Any:
    signal = signals.get(name)
    return signal.get("value") if isinstance(signal, dict) else None


def _number(*values: Any) -> Optional[float]:
    for value in values:
        try:
            if value in (None, ""):
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _fmt_score(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "нет данных"
    return ("%.2f" % number).rstrip("0").rstrip(".")


def manipulation_level(value: Any) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "нет данных"
    if score > 82:
        return "высокий"
    if score > 55:
        return "средний"
    return "низкий"


def late_entry_level(value: Any) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "нет данных"
    if score > 75:
        return "движение перегрето"
    if score > 45:
        return "есть риск догонять движение"
    return "вход не выглядит поздним"


def lifecycle_label(value: Any) -> str:
    return {
        "early_discovery": "раннее обнаружение",
        "acceleration": "ускорение",
        "mainstream_hype": "массовый разгон",
        "exhaustion": "истощение движения",
        "distribution": "распределение / ломается структура",
    }.get(str(value or ""), "нет данных")


def lifecycle_reason(value: Any) -> str:
    return {
        "early_discovery": "движение еще не выглядит перегретым.",
        "acceleration": "цена или объем ускоряются относительно базы.",
        "mainstream_hype": "движение уже заметно рынку, риск позднего входа выше.",
        "exhaustion": "есть признаки выдыхания импульса; лонг с текущих опаснее.",
        "distribution": "структура ломается или риск позднего входа экстремальный.",
    }.get(str(value or ""), "фаза не определена.")


def hype_cause_label(value: Any) -> str:
    return {
        "volume_spike": "всплеск объема",
        "market_anomaly": "аномальное движение цены",
        "mainstream_hype": "сильное 24ч движение",
        "manipulative": "повышенный риск манипулятивности",
        "market_watch": "рыночное наблюдение без сильного триггера",
    }.get(str(value or ""), str(value or "нет данных"))


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
    return final.get("reason_summary") or "Сетап есть; проверить инвалидацию и риск."
