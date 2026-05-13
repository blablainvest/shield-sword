from __future__ import annotations

import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .engine import HypeRadarEngine, ScanConfig
from .storage import RadarStore


class RadarServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        store_path: str = "data/hype_radar.sqlite3",
    ) -> None:
        self.host = host
        self.port = port
        self.store = RadarStore(store_path)
        self.engine = HypeRadarEngine()

    def serve_forever(self) -> None:
        owner = self

        class Handler(RadarRequestHandler):
            server_owner = owner

        httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        print("Щит и Меч dashboard: http://%s:%s" % (self.host, self.port))
        httpd.serve_forever()


class RadarRequestHandler(BaseHTTPRequestHandler):
    server_owner: RadarServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._handle_api_get(parsed.path, parse_qs(parsed.query))
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/scan/run":
            self._handle_scan_run(parse_qs(parsed.query))
            return
        if parsed.path == "/api/research/run":
            self._handle_research_run(parse_qs(parsed.query))
            return
        if parsed.path == "/api/search/run":
            self._handle_search_run(parse_qs(parsed.query))
            return
        self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def _handle_api_get(self, path: str, query: Dict[str, Any]) -> None:
        store = self.server_owner.store
        if path == "/api/scan/latest":
            report = store.latest_report()
            if not report:
                self._json({"error": "No scan has been saved yet."}, HTTPStatus.NOT_FOUND)
                return
            self._json(report)
            return
        if path == "/api/runs":
            self._json({"runs": store.list_runs(limit=_int_query(query, "limit", 50))})
            return
        if path == "/api/research":
            self._json({"research": store.list_research(run_id=_optional_first(query, "run_id"), limit=_optional_int_query(query, "limit"))})
            return
        if path.startswith("/api/research/"):
            parts = path.split("/")
            if len(parts) < 5:
                self._json({"error": "Expected /api/research/{run_id}/{symbol}."}, HTTPStatus.BAD_REQUEST)
                return
            run_id = unquote(parts[3])
            symbol = unquote(parts[4])
            card = store.get_research(run_id, symbol)
            if not card:
                self._json({"error": "Research card not found."}, HTTPStatus.NOT_FOUND)
                return
            self._json(card)
            return
        if path.startswith("/api/runs/"):
            run_id = unquote(path.rsplit("/", 1)[-1])
            report = store.get_report(run_id)
            if not report:
                self._json({"error": "Run not found."}, HTTPStatus.NOT_FOUND)
                return
            self._json(report)
            return
        if path.startswith("/api/candidates/"):
            parts = path.split("/")
            if len(parts) < 5:
                self._json({"error": "Expected /api/candidates/{run_id}/{symbol}."}, HTTPStatus.BAD_REQUEST)
                return
            run_id = unquote(parts[3])
            symbol = unquote(parts[4])
            candidate = store.get_candidate(run_id, symbol)
            if not candidate:
                self._json({"error": "Candidate not found."}, HTTPStatus.NOT_FOUND)
                return
            self._json(candidate)
            return
        if path.startswith("/api/stages/"):
            run_id = unquote(path.rsplit("/", 1)[-1])
            self._json({"stages": store.get_stages(run_id)})
            return
        self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def _handle_scan_run(self, query: Dict[str, Any]) -> None:
        config = ScanConfig(
            top=_int_query(query, "top", 5),
            max_symbols=_int_query(query, "max_symbols", 40),
            min_turnover_24h=_volume_query(query),
            window_hours=_int_query(query, "window_hours", 24),
            workers=_int_query(query, "workers", 8),
        )
        report = self.server_owner.engine.market_scan(config)
        self.server_owner.store.save_report(report)
        self._json(report.to_dict())

    def _handle_research_run(self, query: Dict[str, Any]) -> None:
        symbol = _optional_first(query, "symbol")
        if not symbol:
            self._json({"error": "Missing symbol."}, HTTPStatus.BAD_REQUEST)
            return
        try:
            symbol = normalize_symbol_from_query(symbol)
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        card = self._run_symbol_research(symbol, query)
        self._json(card)

    def _handle_search_run(self, query: Dict[str, Any]) -> None:
        value = _optional_first(query, "query") or _optional_first(query, "input") or _optional_first(query, "url")
        if not value:
            self._json({"error": "Missing query."}, HTTPStatus.BAD_REQUEST)
            return
        try:
            symbol = normalize_symbol_from_query(value)
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        card = self._run_symbol_research(symbol, query)
        self._json(card)

    def _run_symbol_research(self, symbol: str, query: Dict[str, Any]) -> Dict[str, Any]:
        config = ScanConfig(
            top=1,
            max_symbols=1,
            min_turnover_24h=_volume_query(query),
            window_hours=_int_query(query, "window_hours", 24),
            workers=1,
        )
        candidate = self.server_owner.engine.research_symbol(symbol, config)
        run_id = self.server_owner.store.latest_run_id() or "manual"
        card = self.server_owner.store.save_research(run_id, candidate)
        return card

    def _serve_static(self, path: str) -> None:
        static_root = Path(__file__).parent / "web"
        target = static_root / ("index.html" if path in {"", "/"} else path.lstrip("/"))
        target = target.resolve()
        if static_root.resolve() not in target.parents and target != static_root.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.exists() or not target.is_file():
            target = static_root / "index.html"
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


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


def _optional_int_query(query: Dict[str, Any], key: str) -> Optional[int]:
    value = _optional_first(query, key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _volume_query(query: Dict[str, Any]) -> float:
    return float(_first(query, "min_volume", _first(query, "min_turnover", "2000000")))


def normalize_symbol_from_query(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("Введите тикер или ссылку Bybit.")

    parsed = urlparse(text)
    token = ""
    if parsed.scheme or parsed.netloc:
        token = _symbol_from_bybit_url(parsed)
    else:
        token = text

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


def serve(host: str = "127.0.0.1", port: int = 8765, store_path: str = "data/hype_radar.sqlite3") -> None:
    RadarServer(host=host, port=port, store_path=store_path).serve_forever()
