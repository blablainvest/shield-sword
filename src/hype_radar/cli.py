from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Iterable

from .engine import HypeRadarEngine, ScanConfig, ScanReport
from .models import Candidate
from .server import serve
from .storage import RadarStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hype-radar", description="Bybit Altcoin Hype Radar")
    subparsers = parser.add_subparsers(dest="command")
    scan = subparsers.add_parser("scan", help="Run the high-volatility read-only scanner")
    scan.add_argument("--top", type=int, default=5, help="Number of symbols per bucket")
    scan.add_argument("--max-symbols", type=int, default=40, help="Number of prefiltered symbols to enrich")
    scan.add_argument("--min-volume", "--min-turnover", dest="min_volume_24h", type=float, default=2_000_000.0, help="Minimum 24h traded volume in USDT")
    scan.add_argument("--workers", type=int, default=8, help="Parallel symbol workers")
    scan.add_argument("--format", choices=["json", "text"], default="json", help="Output format")
    scan.add_argument("--output", help="Optional path for JSON report")
    scan.add_argument("--save-db", action="store_true", help="Persist scan into SQLite history")
    scan.add_argument("--db", default="data/hype_radar.sqlite3", help="SQLite database path")
    server = subparsers.add_parser("serve", help="Run local dashboard and API server")
    server.add_argument("--host", default="127.0.0.1", help="Host to bind")
    server.add_argument("--port", type=int, default=8765, help="Port to bind")
    server.add_argument("--db", default="data/hype_radar.sqlite3", help="SQLite database path")
    return parser


def main(argv: Iterable[str] = None) -> int:
    load_dotenv_file()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "serve":
        serve(host=args.host, port=args.port, store_path=args.db)
        return 0
    if args.command != "scan":
        parser.print_help()
        return 2

    config = ScanConfig(
        top=args.top,
        max_symbols=args.max_symbols,
        min_turnover_24h=args.min_volume_24h,
        workers=args.workers,
    )
    report = HypeRadarEngine().scan(config)
    if args.save_db:
        RadarStore(args.db).save_report(report)
    if args.output:
        report.write_json(args.output)

    if args.format == "text":
        print_text_report(report)
    else:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


def load_dotenv_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            item = line.strip()
            if not item or item.startswith("#") or "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")


def print_text_report(report: ScanReport) -> None:
    print("Bybit Altcoin Hype Radar")
    print(
        "run=%s eligible=%s scanned=%s rejected=%s errors=%s"
        % (
            report.run.run_id,
            report.eligible_symbols,
            report.scanned_symbols,
            len(report.rejected_candidates),
            len(report.errors),
        )
    )
    print("")
    print("Top 24h Gainers")
    print_candidates(report.top_long)
    print("")
    print("Top 24h Losers")
    print_candidates(report.top_short_watch)
    if report.errors:
        print("")
        print("Symbol errors: %s" % ", ".join(sorted(report.errors)[:12]))


def print_candidates(candidates: Iterable[Candidate]) -> None:
    for index, candidate in enumerate(candidates, start=1):
        plan = candidate.trade_plan
        print(
            "%s. %-14s %-18s long=%5.1f short=%5.1f manip=%5.1f late=%5.1f stage=%s"
            % (
                index,
                candidate.symbol,
                candidate.verdict,
                candidate.long_score,
                candidate.short_score,
                candidate.manipulation_score,
                candidate.late_entry_risk,
                candidate.theme_lifecycle_stage,
            )
        )
        print("   %s" % candidate.reason_summary)
        if plan.entry is not None:
            print(
                "   entry=%s safer=%s sl=%s tp1=%s tp2=%s rr=%s"
                % (plan.entry, plan.safer_entry, plan.stop_loss, plan.take_profit_1, plan.take_profit_2, plan.risk_reward)
            )


if __name__ == "__main__":
    sys.exit(main())
