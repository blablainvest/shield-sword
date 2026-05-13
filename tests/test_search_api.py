import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hype_radar.engine import ScanConfig
from hype_radar.models import PipelineCandidate, PipelineStageResult
from hype_radar.server import RadarRequestHandler, normalize_symbol_from_query


class FakeSearchEngine:
    def __init__(self):
        self.calls = []

    def research_symbol(self, symbol, config):
        self.calls.append((symbol, config))
        candidate = PipelineCandidate(symbol=symbol, base_coin=symbol.removesuffix("USDT"), quote_coin="USDT")
        candidate.add_stage(
            PipelineStageResult(
                stage="market_scan",
                status="pass",
                score=1.0,
                reason="ok",
                metrics={"price_change_window_pct": 0.12},
            )
        )
        return candidate


class FakeSearchStore:
    def __init__(self):
        self.saved = []

    def latest_run_id(self):
        return "scan-run-1"

    def save_research(self, run_id, candidate):
        card = {
            "run_id": run_id,
            "research_id": len(self.saved) + 1,
            "symbol": candidate.symbol,
            "pipeline": candidate.to_dict(),
        }
        self.saved.append((run_id, candidate.symbol, card))
        return card


class FakeOwner:
    def __init__(self):
        self.engine = FakeSearchEngine()
        self.store = FakeSearchStore()


class SearchApiTest(unittest.TestCase):
    def test_normalize_ticker_and_bybit_url(self):
        cases = {
            "BTC": "BTCUSDT",
            "BTCUSDT": "BTCUSDT",
            "b3usdt": "B3USDT",
            "https://www.bybit.com/trade/usdt/BTCUSDT": "BTCUSDT",
            "https://www.bybit.com/en/trade/usdt/b3usdt?foo=bar": "B3USDT",
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(normalize_symbol_from_query(query), expected)

    def test_normalize_rejects_invalid_input(self):
        for query in [
            "",
            "   ",
            "BTC/USDT",
            "https://www.bybit.com/trade/usdt/",
            "BTC-USDT",
            "BTCUSDC",
            "https://fakebybit.com/trade/usdt/BTCUSDT",
        ]:
            with self.subTest(query=query):
                with self.assertRaises(ValueError):
                    normalize_symbol_from_query(query)

    def test_search_run_normalizes_symbol_and_saves_research(self):
        owner = FakeOwner()
        base_url, server = self._start_server(owner)
        try:
            payload = self._post_json(f"{base_url}/api/search/run?query=BTC&min_volume=1000000&window_hours=4")
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(payload["symbol"], "BTCUSDT")
        self.assertEqual(owner.engine.calls[0][0], "BTCUSDT")
        self.assertIsInstance(owner.engine.calls[0][1], ScanConfig)
        self.assertEqual(owner.engine.calls[0][1].window_hours, 4)
        self.assertEqual(owner.store.saved[0][0], "scan-run-1")
        self.assertEqual(owner.store.saved[0][1], "BTCUSDT")

    def test_search_run_returns_400_for_invalid_input(self):
        owner = FakeOwner()
        base_url, server = self._start_server(owner)
        try:
            with self.assertRaises(HTTPError) as caught:
                self._post_json(f"{base_url}/api/search/run?query=BTC-USDT")
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(caught.exception.code, 400)
        payload = json.loads(caught.exception.read().decode("utf-8"))
        self.assertIn("error", payload)
        self.assertEqual(owner.engine.calls, [])

    def _start_server(self, owner):
        class Handler(RadarRequestHandler):
            server_owner = owner

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        return f"http://{host}:{port}", server

    def _post_json(self, url):
        request = Request(url, method="POST")
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
