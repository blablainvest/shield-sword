from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional

from .http import JsonHttpClient
from .models import Candle, CvdStats, Instrument, LongShortRatio, OrderbookStats, Ticker, TradePrint


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


class BybitPublicClient:
    def __init__(self, base_url: Optional[str] = None, http: Optional[JsonHttpClient] = None) -> None:
        self.base_url = (base_url or os.getenv("BYBIT_BASE_URL") or "https://api.bybit.com").rstrip("/")
        self.http = http or JsonHttpClient()

    def _get_result(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        payload = self.http.get_json(self.base_url + path, params)
        if payload.get("retCode") != 0:
            raise RuntimeError("Bybit API error %s: %s" % (payload.get("retCode"), payload.get("retMsg")))
        return payload.get("result") or {}

    def instruments_info(self, category: str = "linear") -> List[Instrument]:
        rows: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            result = self._get_result(
                "/v5/market/instruments-info",
                {"category": category, "limit": 1000, "cursor": cursor},
            )
            rows.extend(result.get("list") or [])
            cursor = result.get("nextPageCursor") or None
            if not cursor:
                break

        instruments: List[Instrument] = []
        for row in rows:
            price_filter = row.get("priceFilter") or {}
            instruments.append(
                Instrument(
                    symbol=str(row.get("symbol") or ""),
                    base_coin=str(row.get("baseCoin") or ""),
                    quote_coin=str(row.get("quoteCoin") or ""),
                    status=str(row.get("status") or ""),
                    contract_type=str(row.get("contractType") or ""),
                    launch_time_ms=_int_or_none(row.get("launchTime")),
                    tick_size=_float(price_filter.get("tickSize"), 0.0) or None,
                )
            )
        return instruments

    def tickers(self, category: str = "linear") -> List[Ticker]:
        result = self._get_result("/v5/market/tickers", {"category": category})
        tickers: List[Ticker] = []
        for row in result.get("list") or []:
            tickers.append(
                Ticker(
                    symbol=str(row.get("symbol") or ""),
                    last_price=_float(row.get("lastPrice")),
                    bid_price=_float(row.get("bid1Price")),
                    ask_price=_float(row.get("ask1Price")),
                    price_24h_pct=_float(row.get("price24hPcnt")),
                    volume_24h=_float(row.get("volume24h")),
                    turnover_24h=_float(row.get("turnover24h")),
                    funding_rate=_float(row.get("fundingRate")),
                    open_interest=_float(row.get("openInterest")),
                    open_interest_value=_float(row.get("openInterestValue")),
                )
            )
        return tickers

    def long_short_ratio(self, symbol: str, period: str = "1h", category: str = "linear") -> Optional[LongShortRatio]:
        result = self._get_result(
            "/v5/market/account-ratio",
            {"category": category, "symbol": symbol, "period": period},
        )
        rows = result.get("list") or []
        if not rows:
            return None

        def timestamp(row: Dict[str, Any]) -> int:
            return _int_or_none(row.get("timestamp")) or 0

        row = max(rows, key=timestamp)
        return LongShortRatio(
            symbol=str(row.get("symbol") or symbol),
            long_ratio=_optional_float(row.get("buyRatio")),
            short_ratio=_optional_float(row.get("sellRatio")),
            timestamp_ms=_int_or_none(row.get("timestamp")),
        )

    def klines(self, symbol: str, interval: str, limit: int = 200, category: str = "linear") -> List[Candle]:
        result = self._get_result(
            "/v5/market/kline",
            {"category": category, "symbol": symbol, "interval": interval, "limit": limit},
        )
        candles = [
            Candle(
                start_ms=int(row[0]),
                open=_float(row[1]),
                high=_float(row[2]),
                low=_float(row[3]),
                close=_float(row[4]),
                volume=_float(row[5]),
                turnover=_float(row[6]),
            )
            for row in (result.get("list") or [])
            if len(row) >= 7
        ]
        return sorted(candles, key=lambda candle: candle.start_ms)

    def recent_trades(self, symbol: str, limit: int = 1000, category: str = "linear") -> List[TradePrint]:
        result = self._get_result(
            "/v5/market/recent-trade",
            {"category": category, "symbol": symbol, "limit": min(max(limit, 1), 1000)},
        )
        trades = [
            TradePrint(
                symbol=str(row.get("symbol") or symbol),
                side=str(row.get("side") or ""),
                price=_float(row.get("price")),
                size=_float(row.get("size")),
                timestamp_ms=_int_or_none(row.get("time")),
            )
            for row in (result.get("list") or [])
            if isinstance(row, dict)
        ]
        return sorted(trades, key=lambda trade: trade.timestamp_ms or 0)

    def recent_trade_cvd(self, symbol: str, limit: int = 1000, category: str = "linear") -> Optional[CvdStats]:
        trades = self.recent_trades(symbol, limit=limit, category=category)
        if not trades:
            return None
        buy_volume = sum(trade.size for trade in trades if trade.side.lower() == "buy")
        sell_volume = sum(trade.size for trade in trades if trade.side.lower() == "sell")
        timestamps = [trade.timestamp_ms for trade in trades if trade.timestamp_ms is not None]
        return CvdStats(
            symbol=symbol,
            cvd_base=buy_volume - sell_volume,
            buy_volume_base=buy_volume,
            sell_volume_base=sell_volume,
            trade_count=len(trades),
            first_timestamp_ms=min(timestamps) if timestamps else None,
            last_timestamp_ms=max(timestamps) if timestamps else None,
        )

    def orderbook(self, symbol: str, limit: int = 50, category: str = "linear") -> OrderbookStats:
        result = self._get_result(
            "/v5/market/orderbook",
            {"category": category, "symbol": symbol, "limit": limit},
        )
        bids = self._levels(result.get("b") or [])
        asks = self._levels(result.get("a") or [])
        if not bids or not asks:
            return OrderbookStats(999.0, 0.0, 0.0, 0.0)

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
        if mid <= 0:
            return OrderbookStats(999.0, 0.0, 0.0, 0.0)

        spread_bps = ((best_ask - best_bid) / mid) * 10000.0
        bid_floor = mid * 0.995
        ask_ceiling = mid * 1.005
        bid_depth = sum(price * size for price, size in bids if price >= bid_floor)
        ask_depth = sum(price * size for price, size in asks if price <= ask_ceiling)
        return OrderbookStats(spread_bps, bid_depth, ask_depth, bid_depth + ask_depth)

    @staticmethod
    def _levels(rows: Iterable[Any]) -> List[tuple]:
        levels = []
        for row in rows:
            if len(row) >= 2:
                price = _float(row[0])
                size = _float(row[1])
                if price > 0 and size > 0:
                    levels.append((price, size))
        return levels
