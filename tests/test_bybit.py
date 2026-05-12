import unittest

from hype_radar.bybit import BybitPublicClient


class FakeHttp:
    def get_json(self, url, params):
        self.url = url
        self.params = params
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {"symbol": "BTCUSDT", "side": "Sell", "price": "100.0", "size": "1.5", "time": "1000"},
                    {"symbol": "BTCUSDT", "side": "Buy", "price": "101.0", "size": "2.0", "time": "2000"},
                    {"symbol": "BTCUSDT", "side": "Buy", "price": "102.0", "size": "0.25", "time": "3000"},
                ]
            },
        }


class FakeOpenInterestHttp:
    def get_json(self, url, params):
        self.url = url
        self.params = params
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {"openInterest": "12", "timestamp": "2000"},
                    {"openInterest": "10", "timestamp": "1000"},
                ]
            },
        }


class BybitClientTests(unittest.TestCase):
    def test_recent_trade_cvd_is_computed_from_public_trade_sides(self):
        http = FakeHttp()
        client = BybitPublicClient(base_url="https://example.test", http=http)

        cvd = client.recent_trade_cvd("BTCUSDT", limit=1000)

        self.assertEqual(http.url, "https://example.test/v5/market/recent-trade")
        self.assertEqual(http.params["symbol"], "BTCUSDT")
        self.assertEqual(cvd.trade_count, 3)
        self.assertEqual(cvd.buy_volume_base, 2.25)
        self.assertEqual(cvd.sell_volume_base, 1.5)
        self.assertEqual(cvd.cvd_base, 0.75)
        self.assertEqual(cvd.first_timestamp_ms, 1000)
        self.assertEqual(cvd.last_timestamp_ms, 3000)

    def test_open_interest_uses_public_hourly_endpoint(self):
        http = FakeOpenInterestHttp()
        client = BybitPublicClient(base_url="https://example.test", http=http)

        rows = client.open_interest("BTCUSDT", interval="1h", limit=24)

        self.assertEqual(http.url, "https://example.test/v5/market/open-interest")
        self.assertEqual(http.params["intervalTime"], "1h")
        self.assertEqual(http.params["limit"], 24)
        self.assertEqual([row["timestamp"] for row in rows], ["1000", "2000"])


if __name__ == "__main__":
    unittest.main()
