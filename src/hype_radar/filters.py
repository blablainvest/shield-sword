from __future__ import annotations

import re
from typing import Optional

from .models import Instrument, Ticker


MAJOR_BASES = {
    "BTC",
    "ETH",
    "SOL",
    "BNB",
    "XRP",
    "DOGE",
    "TON",
    "ADA",
    "AVAX",
    "TRX",
    "DOT",
    "LINK",
    "LTC",
    "BCH",
    "MATIC",
    "POL",
    "OP",
    "ARB",
}

STABLE_BASES = {
    "USDT",
    "USDC",
    "DAI",
    "FDUSD",
    "TUSD",
    "USDE",
    "USDD",
    "PYUSD",
    "EUR",
    "EURI",
}

WRAPPED_OR_STAKED_BASES = {
    "WBTC",
    "WETH",
    "WSTETH",
    "STETH",
    "RETH",
    "CBETH",
    "BETH",
}

LEVERAGED_PATTERN = re.compile(r"(2L|2S|3L|3S|5L|5S|UP|DOWN|BULL|BEAR)$")


def is_excluded_base(base_coin: str) -> bool:
    base = base_coin.upper()
    return (
        base in MAJOR_BASES
        or base in STABLE_BASES
        or base in WRAPPED_OR_STAKED_BASES
        or bool(LEVERAGED_PATTERN.search(base))
    )


def tradable_symbol(
    instrument: Optional[Instrument],
    ticker: Ticker,
    min_turnover_24h: float,
    max_ticker_spread_bps: float = 60.0,
) -> bool:
    if not instrument:
        return False
    if instrument.quote_coin.upper() != "USDT":
        return False
    if instrument.status != "Trading":
        return False
    if instrument.contract_type and instrument.contract_type != "LinearPerpetual":
        return False
    if is_excluded_base(instrument.base_coin):
        return False
    if ticker.last_price <= 0 or ticker.turnover_24h < min_turnover_24h:
        return False
    if ticker.bid_price > 0 and ticker.ask_price > 0:
        mid = (ticker.bid_price + ticker.ask_price) / 2.0
        spread_bps = ((ticker.ask_price - ticker.bid_price) / mid) * 10000.0 if mid > 0 else 999.0
        if spread_bps > max_ticker_spread_bps:
            return False
    return True

