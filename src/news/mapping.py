"""Venue symbol <-> news asset mapping.

Two jobs:

1. Turn a venue symbol (BTCUSD) into an asset code (BTC) for tagged sources
   like CryptoPanic.
2. Tag untagged sources (RSS) by matching keywords in the headline.

`has_news_coverage()` matters more than it looks: roughly 40% of this venue's
tradeable universe is tokenised equities and metals with no crypto-news
coverage at all. Knowing that up front lets the agent gate short-circuit to a
neutral verdict without a network call or a model invocation.
"""

from __future__ import annotations

import re

# Symbols whose asset code is not just the symbol minus its quote currency.
SYMBOL_OVERRIDES: dict[str, str | None] = {
    # Tokenised equities and commodities. Their price drivers are equity/macro
    # news, which crypto feeds do not carry. Explicitly no coverage.
    "SLVONUSD": None,
    "SNDKBUSD": None,
    # Gold-backed tokens: relevant news is macro (rates, USD), not crypto.
    # Excluded until a macro source is wired in.
    "PAXGUSD": None,
    "XAUTUSD": None,
}

_QUOTE_SUFFIX = re.compile(r"(USDT|USDC|USD|INR)$")

# Keyword sets for tagging untagged sources. Order matters only for logging;
# an item may match several assets. Keep these tight — a loose "ada" would
# match "Canada".
ASSET_KEYWORDS: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin", "btc", "satoshi"),
    "ETH": ("ethereum", "ether", "eth", "vitalik"),
    "SOL": ("solana", "sol"),
    "XRP": ("xrp", "ripple"),
    "ADA": ("cardano", "ada"),
    "DOGE": ("dogecoin", "doge"),
    "ONDO": ("ondo",),
}

# Whole-word matching so "sol" does not fire on "solution" and "eth" does not
# fire on "ethics".
_KEYWORD_PATTERNS: dict[str, re.Pattern[str]] = {
    asset: re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keywords) + r")\b", re.I)
    for asset, keywords in ASSET_KEYWORDS.items()
}


def symbol_to_asset(symbol: str) -> str | None:
    """Map a venue symbol to its news asset code, or None if uncovered."""
    upper = symbol.upper()
    if upper in SYMBOL_OVERRIDES:
        return SYMBOL_OVERRIDES[upper]

    asset = _QUOTE_SUFFIX.sub("", upper)
    return asset or None


def has_news_coverage(symbol: str) -> bool:
    """Whether any wired-in source plausibly reports on this symbol."""
    asset = symbol_to_asset(symbol)
    return asset is not None and asset in ASSET_KEYWORDS


def assets_for_text(text: str) -> list[str]:
    """Tag a headline with the assets it mentions.

    Used for sources that do not provide their own asset tags (RSS).
    """
    if not text:
        return []
    return [asset for asset, pattern in _KEYWORD_PATTERNS.items() if pattern.search(text)]


def covered_assets() -> list[str]:
    """Every asset code the pipeline can currently tag."""
    return sorted(ASSET_KEYWORDS)
