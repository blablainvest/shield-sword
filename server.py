from __future__ import annotations

import json
import mimetypes
import os
import re
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from hype_radar.engine import HypeRadarEngine, ScanConfig
from hype_radar.storage import RadarStore


_STORE_PATH = os.environ.get("HYPE_RADAR_STORE", "/tmp/hype_radar.sqlite3")
_STORE = RadarStore(_STORE_PATH)
_ENGINE = HypeRadarEngine()
_STATIC_ROOT = Path(__file__).parent / "src" / "hype_radar" / "web"


def app(environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/")
    query = parse_qs(environ.get("QUERY_STRING", ""))

    try:
        if method == "GET" and path.startswith("/api/"):
            return _api_get(path, query, start_response)
        if method == "POST" and path == "/api/scan/run":
            return _scan_run(query, start_response)
        if method == "POST" and path == "/api/research/run":
            return _research_run(query, start_response)
        if method == "POST" and path == "/api/search/run":
            return _search_run(query, start_response)
        if method in {"GET", "HEAD"}:
            return _static(path, start_response, include_body=method == "GET")
        return _json({"error": "Not found"}, start_response, HTTPStatus.NOT_FOUND)
    except Exception as exc:  # Vercel should return JSON instead of a blank function error.
        return _json({"error": str(exc)}, start_response, HTTPStatus.INTERNAL_SERVER_ERROR)


def _api_get(path: str, query: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
    if path == "/api/scan/latest":
        report = _STORE.latest_report()
        if not report:
            return _json({"error": "No scan has been saved yet."}, start_response, HTTPStatus.NOT_FOUND)
        return _json(report, start_response)
    if path == "/api/runs":
        return _json({"runs": _STORE.list_runs(limit=_int_query(query, "limit", 50))}, start_response)
    if path == "/api/research":
        return _json(
            {"research": _STORE.list_research(run_id=_optional_first(query, "run_id"), limit=_int_query(query, "limit", 100))},
            start_response,
        )
    if path.startswith("/api/research/"):
        parts = path.split("/")
        if len(parts) < 5:
            return _json({"error": "Expected /api/research/{run_id}/{symbol}."}, start_response, HTTPStatus.BAD_REQUEST)
        card = _STORE.get_research(unquote(parts[3]), unquote(parts[4]))
        if not card:
            return _json({"error": "Research card not found."}, start_response, HTTPStatus.NOT_FOUND)
        return _json(card, start_response)
    if path.startswith("/api/runs/"):
        report = _STORE.get_report(unquote(path.rsplit("/", 1)[-1]))
        if not report:
            return _json({"error": "Run not found."}, start_response, HTTPStatus.NOT_FOUND)
        return _json(report, start_response)
    if path.startswith("/api/candidates/"):
        parts = path.split("/")
        if len(parts) < 5:
            return _json({"error": "Expected /api/candidates/{run_id}/{symbol}."}, start_response, HTTPStatus.BAD_REQUEST)
        candidate = _STORE.get_candidate(unquote(parts[3]), unquote(parts[4]))
        if not candidate:
            return _json({"error": "Candidate not found."}, start_response, HTTPStatus.NOT_FOUND)
        return _json(candidate, start_response)
    if path.startswith("/api/stages/"):
        return _json({"stages": _STORE.get_stages(unquote(path.rsplit("/", 1)[-1]))}, start_response)
    return _json({"error": "Not found"}, start_response, HTTPStatus.NOT_FOUND)


def _scan_run(query: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
    config = ScanConfig(
        top=_int_query(query, "top", 5),
        max_symbols=_int_query(query, "max_symbols", 40),
        min_turnover_24h=_volume_query(query),
        window_hours=_int_query(query, "window_hours", 24),
        workers=_int_query(query, "workers", 8),
    )
    report = _ENGINE.market_scan(config)
    _STORE.save_report(report)
    return _json(report.to_dict(), start_response)


def _research_run(query: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
    symbol = _optional_first(query, "symbol")
    if not symbol:
        return _json({"error": "Missing symbol."}, start_response, HTTPStatus.BAD_REQUEST)
    try:
        symbol = normalize_symbol_from_query(symbol)
    except ValueError as exc:
        return _json({"error": str(exc)}, start_response, HTTPStatus.BAD_REQUEST)
    return _run_symbol_research(symbol, query, start_response)


def _search_run(query: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
    value = _optional_first(query, "query") or _optional_first(query, "input") or _optional_first(query, "url")
    if not value:
        return _json({"error": "Введите тикер или ссылку Bybit."}, start_response, HTTPStatus.BAD_REQUEST)
    try:
        symbol = normalize_symbol_from_query(value)
    except ValueError as exc:
        return _json({"error": str(exc)}, start_response, HTTPStatus.BAD_REQUEST)
    return _run_symbol_research(symbol, query, start_response)


def _run_symbol_research(
    symbol: str,
    query: Dict[str, Any],
    start_response: Callable[..., Any],
) -> Iterable[bytes]:
    config = ScanConfig(
        top=1,
        max_symbols=1,
        min_turnover_24h=_volume_query(query),
        window_hours=_int_query(query, "window_hours", 24),
        workers=1,
    )
    candidate = _ENGINE.research_symbol(symbol, config)
    run_id = _STORE.latest_run_id() or "manual"
    _STORE.save_research(run_id, candidate)
    return _json(_STORE.get_research(run_id, candidate.symbol) or candidate.to_dict(), start_response)


def _static(path: str, start_response: Callable[..., Any], include_body: bool = True) -> Iterable[bytes]:
    target = _STATIC_ROOT / ("index.html" if path in {"", "/"} else path.lstrip("/"))
    target = target.resolve()
    root = _STATIC_ROOT.resolve()
    if root not in target.parents and target != root:
        return _json({"error": "Forbidden"}, start_response, HTTPStatus.FORBIDDEN)
    if not target.exists() or not target.is_file():
        target = root / "index.html"
    data = target.read_bytes() if include_body else b""
    headers = [
        ("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream"),
        ("Content-Length", str(len(data))),
        ("Cache-Control", "no-store"),
    ]
    start_response("%s %s" % (HTTPStatus.OK.value, HTTPStatus.OK.phrase), headers)
    return [data]


def _json(
    payload: Dict[str, Any],
    start_response: Callable[..., Any],
    status: HTTPStatus = HTTPStatus.OK,
) -> Iterable[bytes]:
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    start_response(
        "%s %s" % (status.value, status.phrase),
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(data))),
        ],
    )
    return [data]


def _first(query: Dict[str, Any], key: str, default: str) -> str:
    values = query.get(key)
    if not values:
        return default
    return str(values[0])


def _optional_first(query: Dict[str, Any], key: str) -> Optional[str]:
    values = query.get(key)
    if not values:
        return None
    return str(values[0])


def _int_query(query: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(_first(query, key, str(default)))
    except ValueError:
        return default


def _volume_query(query: Dict[str, Any]) -> float:
    return float(_first(query, "min_volume", _first(query, "min_turnover", "2000000")))


def normalize_symbol_from_query(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Введите тикер или ссылку Bybit.")

    parsed = urlparse(text)
    token = _symbol_from_bybit_url(parsed) if parsed.scheme or parsed.netloc else text
    symbol = _normalize_symbol_token(token)
    if not symbol:
        raise ValueError("Некорректный тикер. Используйте Bybit linear USDT тикер, например BTCUSDT или BTC.")
    return symbol


_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,30}$")
_KNOWN_NON_USDT_QUOTES = ("USDC", "USD", "EUR", "TRY")


def _symbol_from_bybit_url(parsed: Any) -> str:
    host = parsed.netloc.lower()
    if host and host != "bybit.com" and not host.endswith(".bybit.com"):
        raise ValueError("Поддерживаются только ссылки Bybit trade.")

    candidates = []
    parts = [unquote(part).strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.lower() for part in parts]
    for index, part in enumerate(lowered[:-1]):
        if part == "usdt":
            candidates.append(parts[index + 1])
    candidates.extend(part for part in parts if part.upper().endswith("USDT"))
    query = parse_qs(parsed.query)
    for key in ("symbol", "contract", "pair"):
        candidates.extend(query.get(key, []))

    for candidate in candidates:
        normalized = _normalize_symbol_token(candidate)
        if normalized:
            return normalized
    raise ValueError("Не удалось найти USDT тикер в ссылке Bybit.")


def _normalize_symbol_token(value: str) -> str:
    token = str(value or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(token):
        return ""
    if token.endswith("USDT"):
        return token if len(token) > 4 else ""
    if token.endswith(_KNOWN_NON_USDT_QUOTES):
        return ""
    return f"{token}USDT"
