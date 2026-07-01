"""
symbols.py
----------
Central catalog of tradable symbols, grouped into the three sub-categories the
dashboard expects:

    a. Indian Index   -> served by the "yfinance" provider
    b. Indian Stocks  -> served by the "yfinance" provider
    c. Crypto         -> served by the "hyperliquid" provider

Each symbol entry is a dict:
    {
        "label":    Human readable name shown in the dropdown,
        "symbol":   The ticker handed to the data provider (e.g. "^NSEI", "BTC"),
        "provider": Which data_source provider handles it ("yfinance"|"hyperliquid"),
        "options":  True if an options chain should be offered for this symbol,
        "strike_step": Strike spacing used when generating the option chain
                       (only meaningful when options=True),
    }

This module is intentionally just *data* + tiny helpers.  Anything that knows how
to *fetch* prices lives in data_source.py.  To expose a new market, add entries
here and make sure the referenced provider exists in data_source.py.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# a. Indian Index
# ---------------------------------------------------------------------------
# Yahoo Finance tickers.  Index strike steps follow the NSE/BSE conventions.
INDIAN_INDICES = [
    {"label": "NIFTY 50",        "symbol": "^NSEI",    "provider": "yfinance", "options": True,  "strike_step": 50},
    {"label": "NIFTY BANK",      "symbol": "^NSEBANK", "provider": "yfinance", "options": True,  "strike_step": 100},
    {"label": "NIFTY FIN SERVICE","symbol": "NIFTY_FIN_SERVICE.NS", "provider": "yfinance", "options": True, "strike_step": 50},
    {"label": "NIFTY MIDCAP 50", "symbol": "^NSEMDCP50","provider": "yfinance", "options": True,  "strike_step": 25},
    {"label": "NIFTY IT",        "symbol": "^CNXIT",   "provider": "yfinance", "options": False, "strike_step": 50},
    {"label": "SENSEX",          "symbol": "^BSESN",   "provider": "yfinance", "options": True,  "strike_step": 100},
    {"label": "BANKEX",          "symbol": "BSE-BANK.BO","provider": "yfinance", "options": True, "strike_step": 100},
    {"label": "INDIA VIX",       "symbol": "^INDIAVIX","provider": "yfinance", "options": False, "strike_step": 0},
]

# ---------------------------------------------------------------------------
# b. Indian Stocks  (NSE, ".NS" suffix on Yahoo Finance)
# ---------------------------------------------------------------------------
# `options=True` marks the liquid NSE F&O names.  strike_step=0 means "derive
# from price" (see suggest_strike_step below).
INDIAN_STOCKS = [
    {"label": "Reliance Industries", "symbol": "RELIANCE.NS",   "options": True},
    {"label": "TCS",                 "symbol": "TCS.NS",        "options": True},
    {"label": "HDFC Bank",           "symbol": "HDFCBANK.NS",   "options": True},
    {"label": "Infosys",             "symbol": "INFY.NS",       "options": True},
    {"label": "ICICI Bank",          "symbol": "ICICIBANK.NS",  "options": True},
    {"label": "State Bank of India", "symbol": "SBIN.NS",       "options": True},
    {"label": "Axis Bank",           "symbol": "AXISBANK.NS",   "options": True},
    {"label": "Kotak Mahindra Bank", "symbol": "KOTAKBANK.NS",  "options": True},
    {"label": "Bharti Airtel",       "symbol": "BHARTIARTL.NS", "options": True},
    {"label": "ITC",                 "symbol": "ITC.NS",        "options": True},
    {"label": "Larsen & Toubro",     "symbol": "LT.NS",         "options": True},
    {"label": "HUL",                 "symbol": "HINDUNILVR.NS", "options": True},
    {"label": "Bajaj Finance",       "symbol": "BAJFINANCE.NS", "options": True},
    {"label": "Maruti Suzuki",       "symbol": "MARUTI.NS",     "options": True},
    {"label": "Asian Paints",        "symbol": "ASIANPAINT.NS", "options": True},
    {"label": "Tata Motors",         "symbol": "TATAMOTORS.NS", "options": True},
    {"label": "Tata Steel",          "symbol": "TATASTEEL.NS",  "options": True},
    {"label": "Wipro",               "symbol": "WIPRO.NS",      "options": True},
    {"label": "Adani Enterprises",   "symbol": "ADANIENT.NS",   "options": True},
    {"label": "Zomato",              "symbol": "ZOMATO.NS",     "options": True},
]

# ---------------------------------------------------------------------------
# c. Crypto  (Hyperliquid perp coins).  This is a *fallback* list; on startup
#    app.py asks Hyperliquid for the live universe and merges it in.  Crypto
#    has no equity-style options chain here.
# ---------------------------------------------------------------------------
CRYPTO = [
    {"label": "BTC", "symbol": "BTC"},
    {"label": "ETH", "symbol": "ETH"},
    {"label": "SOL", "symbol": "SOL"},
    {"label": "ARB", "symbol": "ARB"},
    {"label": "AVAX", "symbol": "AVAX"},
    {"label": "DOGE", "symbol": "DOGE"},
    {"label": "BNB", "symbol": "BNB"},
    {"label": "XRP", "symbol": "XRP"},
    {"label": "MATIC", "symbol": "MATIC"},
    {"label": "LINK", "symbol": "LINK"},
    {"label": "SUI", "symbol": "SUI"},
    {"label": "APT", "symbol": "APT"},
]


def _normalize_index(entry: dict) -> dict:
    return {
        "label": entry["label"],
        "symbol": entry["symbol"],
        "provider": "yfinance",
        "options": entry.get("options", False),
        "strike_step": entry.get("strike_step", 0),
    }


def _normalize_stock(entry: dict) -> dict:
    return {
        "label": entry["label"],
        "symbol": entry["symbol"],
        "provider": "yfinance",
        "options": entry.get("options", False),
        "strike_step": entry.get("strike_step", 0),  # 0 -> derive from price
    }


def _normalize_crypto(entry: dict) -> dict:
    return {
        "label": entry["label"],
        "symbol": entry["symbol"],
        "provider": "hyperliquid",
        "options": False,
        "strike_step": 0,
    }


def build_catalog(extra_crypto: list[str] | None = None) -> dict:
    """Return the full grouped catalog used by the /api/symbols endpoint.

    extra_crypto: optional list of coin names fetched live from Hyperliquid that
                  should be merged into the Crypto group.
    """
    crypto_entries = {c["symbol"]: _normalize_crypto(c) for c in CRYPTO}
    for coin in extra_crypto or []:
        if coin not in crypto_entries:
            crypto_entries[coin] = _normalize_crypto({"label": coin, "symbol": coin})

    return {
        "groups": [
            {"name": "Indian Index",  "items": [_normalize_index(i) for i in INDIAN_INDICES]},
            {"name": "Indian Stocks", "items": [_normalize_stock(s) for s in INDIAN_STOCKS]},
            {"name": "Crypto",        "items": sorted(crypto_entries.values(), key=lambda x: x["label"])},
        ]
    }


# Flat lookup: symbol -> entry.  Rebuilt lazily so live crypto is included.
_LOOKUP: dict[str, dict] = {}


def refresh_lookup(extra_crypto: list[str] | None = None) -> None:
    global _LOOKUP
    _LOOKUP = {}
    for group in build_catalog(extra_crypto)["groups"]:
        for item in group["items"]:
            _LOOKUP[item["symbol"]] = item


def get_symbol_meta(symbol: str) -> dict | None:
    """Look up metadata for a symbol; returns None if unknown.

    Unknown crypto coins (not in the fallback list) are treated as Hyperliquid
    coins so newly listed perps still work without a code change.
    """
    if not _LOOKUP:
        refresh_lookup()
    meta = _LOOKUP.get(symbol)
    if meta:
        return meta
    # Default-route bare alphanumeric tickers to Hyperliquid (crypto perps).
    if symbol and "." not in symbol and "^" not in symbol:
        return {"label": symbol, "symbol": symbol, "provider": "hyperliquid",
                "options": False, "strike_step": 0}
    return None


def provider_for(symbol: str) -> str:
    meta = get_symbol_meta(symbol)
    return meta["provider"] if meta else "yfinance"


def suggest_strike_step(symbol: str, price: float) -> int:
    """Best-effort strike spacing for an option chain.

    Uses the catalog's strike_step when set, otherwise a price-based heuristic
    that mirrors typical NSE strike intervals for equities.
    """
    meta = get_symbol_meta(symbol)
    if meta and meta.get("strike_step"):
        return meta["strike_step"]

    # Price-based heuristic for stocks (NSE-style ladders).
    if price <= 0:
        return 5
    if price < 100:
        return 2.5
    if price < 250:
        return 5
    if price < 500:
        return 10
    if price < 1000:
        return 20
    if price < 2500:
        return 50
    if price < 5000:
        return 100
    return 100
