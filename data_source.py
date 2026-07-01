"""
data_source.py
--------------
The single data-access abstraction for the dashboard.

PLUG-AND-PLAY: add a broker by subclassing BaseProvider + register_provider(...).
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import threading
import time
from typing import Callable, Optional

import symbols as catalog

# `requests`, `yfinance`, `pandas`, `websocket` are imported lazily where used.

# ===========================================================================
# Base interface + registry
# ===========================================================================
class BaseProvider:
    name: str = "base"
    supports_live: bool = False

    def get_candles(self, symbol: str, interval: str, limit: int = 300) -> list[dict]:
        raise NotImplementedError

    def get_quote(self, symbol: str) -> Optional[dict]:
        return None

    def supports_options(self, symbol: str) -> bool:
        return False

    def get_option_chain(self, symbol: str) -> Optional[dict]:
        return None

    def start(self, on_tick: Callable[[dict], None]) -> None:
        pass

    def stop(self) -> None:
        pass

    def subscribe(self, symbol: str) -> None:
        pass

    def unsubscribe(self, symbol: str) -> None:
        pass

PROVIDERS: dict[str, BaseProvider] = {}

def register_provider(provider: BaseProvider) -> None:
    PROVIDERS[provider.name] = provider

def get_provider(symbol: str) -> BaseProvider:
    name = catalog.provider_for(symbol)
    return PROVIDERS[name]

# ===========================================================================
# Shared helpers
# ===========================================================================
TIMEFRAME_SECONDS = {
    "15s": 15, "30s": 30,
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1D": 86400, "1W": 604800, "1M": 2592000,
}

def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(value / step) * step

# ===========================================================================
# Options math: expiries, Black-Scholes pricing, synthetic option candles.
# ===========================================================================
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
RISK_FREE_RATE = 0.065

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def black_scholes(S: float, K: float, T: float, r: float, sigma: float, call: bool) -> float:
    """Black-Scholes premium. Falls back to intrinsic value at/after expiry."""
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

def _last_weekday_of_month(year: int, month: int, weekday: int) -> _dt.date:
    """Last given weekday (Mon=0..Sun=6) of a month -- used for monthly expiry."""
    if month == 12:
        nxt = _dt.date(year + 1, 1, 1)
    else:
        nxt = _dt.date(year, month + 1, 1)
    d = nxt - _dt.timedelta(days=1)
    while d.weekday() != weekday:
        d -= _dt.timedelta(days=1)
    return d

def _expiry_epoch(d: _dt.date) -> int:
    """Unix seconds at 15:30 IST (market close) on the expiry date."""
    dt = _dt.datetime(d.year, d.month, d.day, 15, 30, tzinfo=IST)
    return int(dt.timestamp())

_EXPIRY_WEEKDAY = 3  # NSE expiry = Thursday

def build_expiries(n_weekly: int = 4, n_monthly: int = 2) -> list[dict]:
    """Next n_weekly Thursdays + last-Thursday of the next n_monthly months."""
    today = _dt.datetime.now(IST).date()

    weeklies: list[_dt.date] = []
    d = today
    while len(weeklies) < n_weekly:
        if d.weekday() == _EXPIRY_WEEKDAY and d >= today:
            weeklies.append(d)
        d += _dt.timedelta(days=1)

    monthlies: list[_dt.date] = []
    y, m = today.year, today.month
    while len(monthlies) < n_monthly:
        exp = _last_weekday_of_month(y, m, _EXPIRY_WEEKDAY)
        if exp >= today:
            monthlies.append(exp)
        m += 1
        if m > 12:
            m = 1
            y += 1

    out = []
    for i, d in enumerate(weeklies):
        out.append({
            "key": d.isoformat(),
            "kind": "weekly",
            "date": d.isoformat(),
            "epoch": _expiry_epoch(d),
            "label": f"{d.strftime('%d %b %Y')} (Weekly {i + 1})",
        })
    for d in monthlies:
        out.append({
            "key": "M-" + d.isoformat(),
            "kind": "monthly",
            "date": d.isoformat(),
            "epoch": _expiry_epoch(d),
            "label": f"{d.strftime('%b %Y')} (Monthly)",
        })
    return out

def historical_volatility(candles: list[dict], tf_seconds: int, default: float = 0.20) -> float:
    """Annualised close-to-close volatility from candles, clamped to a sane band."""
    closes = [c["close"] for c in candles if c.get("close", 0) > 0]
    if len(closes) < 5:
        return default
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] > 0]
    if len(rets) < 4:
        return default
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    per_candle = math.sqrt(var)
    periods_per_year = (365.25 * 24 * 3600) / max(tf_seconds, 1)
    vol = per_candle * math.sqrt(periods_per_year)
    return max(0.08, min(vol, 1.5))

def build_option_candles(underlying: list[dict], strike: float, opt_type: str,
                         expiry_epoch: int, sigma: float,
                         r: float = RISK_FREE_RATE) -> list[dict]:
    """Transform underlying OHLC into theoretical option OHLC via Black-Scholes."""
    call = opt_type.upper() in ("CE", "C", "CALL")
    year = 365.25 * 24 * 3600
    out = []
    for c in underlying:
        T = max(0.0, (expiry_epoch - c["time"]) / year)
        o = black_scholes(c["open"], strike, T, r, sigma, call)
        cl = black_scholes(c["close"], strike, T, r, sigma, call)
        p_hi = black_scholes(c["high"], strike, T, r, sigma, call)
        p_lo = black_scholes(c["low"], strike, T, r, sigma, call)
        hi = max(o, cl, p_hi if call else p_lo)
        lo = min(o, cl, p_lo if call else p_hi)
        out.append({
            "time": c["time"],
            "open": round(o, 2), "high": round(hi, 2),
            "low": round(lo, 2), "close": round(cl, 2),
            "volume": c.get("volume", 0),
        })
    return out

# ===========================================================================
# Hyperliquid (crypto perps): REST candles + live websocket trades
# ===========================================================================
class HyperliquidProvider(BaseProvider):
    name = "hyperliquid"
    supports_live = True

    INFO_URL = "https://api.hyperliquid.xyz/info"
    WS_URL = "wss://api.hyperliquid.xyz/ws"

    INTERVAL_MAP = {
        "15s": None, "30s": None,
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1h", "4h": "4h", "1D": "1d", "1W": "1w", "1M": "1M",
    }

    def __init__(self) -> None:
        self._ws = None
        self._ws_thread: Optional[threading.Thread] = None
        self._on_tick: Optional[Callable[[dict], None]] = None
        self._subs: set[str] = set()
        self._lock = threading.Lock()
        self._running = False
        self._last_price: dict[str, float] = {}

    def fetch_universe(self) -> list[str]:
        import requests
        try:
            r = requests.post(self.INFO_URL, json={"type": "meta"}, timeout=10)
            r.raise_for_status()
            data = r.json()
            return [c["name"] for c in data.get("universe", []) if not c.get("isDelisted")]
        except Exception as exc:
            print(f"[hyperliquid] universe fetch failed: {exc}")
            return []

    def get_candles(self, symbol: str, interval: str, limit: int = 300) -> list[dict]:
        import requests
        hl_interval = self.INTERVAL_MAP.get(interval)
        if hl_interval is None:
            return []
        tf_sec = TIMEFRAME_SECONDS.get(interval, 60)
        end = int(time.time() * 1000)
        start = end - tf_sec * 1000 * (limit + 1)
        try:
            r = requests.post(
                self.INFO_URL,
                json={"type": "candleSnapshot",
                      "req": {"coin": symbol, "interval": hl_interval,
                              "startTime": start, "endTime": end}},
                timeout=10,
            )
            r.raise_for_status()
            rows = r.json() or []
        except Exception as exc:
            print(f"[hyperliquid] candles failed for {symbol} {interval}: {exc}")
            return []

        candles = []
        for row in rows:
            candles.append({
                "time": int(row["t"] // 1000),
                "open": float(row["o"]),
                "high": float(row["h"]),
                "low": float(row["l"]),
                "close": float(row["c"]),
                "volume": float(row.get("v", 0) or 0),
            })
        return candles[-limit:]

    def get_quote(self, symbol: str) -> Optional[dict]:
        import requests
        if symbol in self._last_price:
            return {"price": self._last_price[symbol], "time": int(time.time())}
        try:
            r = requests.post(self.INFO_URL, json={"type": "allMids"}, timeout=10)
            r.raise_for_status()
            mids = r.json() or {}
            if symbol in mids:
                return {"price": float(mids[symbol]), "time": int(time.time())}
        except Exception:
            pass
        return None

    def start(self, on_tick: Callable[[dict], None]) -> None:
        self._on_tick = on_tick
        self._running = True
        self._ws_thread = threading.Thread(target=self._run_ws, daemon=True)
        self._ws_thread.start()

    def stop(self) -> None:
        self._running = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def subscribe(self, symbol: str) -> None:
        with self._lock:
            if symbol in self._subs:
                return
            self._subs.add(symbol)
        self._send_sub(symbol, subscribe=True)

    def unsubscribe(self, symbol: str) -> None:
        with self._lock:
            if symbol not in self._subs:
                return
            self._subs.discard(symbol)
        self._send_sub(symbol, subscribe=False)

    def _send_sub(self, symbol: str, subscribe: bool) -> None:
        if not self._ws:
            return
        method = "subscribe" if subscribe else "unsubscribe"
        try:
            self._ws.send(json.dumps({
                "method": method,
                "subscription": {"type": "trades", "coin": symbol},
            }))
        except Exception as exc:
            print(f"[hyperliquid] {method} {symbol} failed: {exc}")

    def _run_ws(self) -> None:
        import websocket  # websocket-client

        def on_open(ws):
            print("[hyperliquid] websocket connected")
            with self._lock:
                current = list(self._subs)
            for coin in current:
                ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "trades", "coin": coin},
                }))

        def on_message(ws, message):
            try:
                payload = json.loads(message)
            except Exception:
                return
            if payload.get("channel") != "trades":
                return
            for trade in payload.get("data", []):
                try:
                    coin = trade["coin"]
                    price = float(trade["px"])
                    ts = int(trade.get("time", time.time() * 1000)) // 1000
                    size = float(trade.get("sz", 0) or 0)
                except (KeyError, ValueError, TypeError):
                    continue
                self._last_price[coin] = price
                if self._on_tick:
                    self._on_tick({
                        "provider": self.name,
                        "symbol": coin,
                        "price": price,
                        "time": ts,
                        "volume": size,
                    })

        def on_error(ws, error):
            print(f"[hyperliquid] websocket error: {error}")

        def on_close(ws, *_):
            print("[hyperliquid] websocket closed")

        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    self.WS_URL,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as exc:
                print(f"[hyperliquid] websocket loop error: {exc}")
            if self._running:
                time.sleep(3)

# ===========================================================================
# yfinance (Indian indices + stocks): REST candles, polled quotes, options
# ===========================================================================
class YFinanceProvider(BaseProvider):
    name = "yfinance"
    supports_live = True

    INTERVAL_MAP = {
        "15s": (None, None, None),
        "30s": (None, None, None),
        "1m":  ("1m", None, "5d"),
        "3m":  ("1m", "3min", "5d"),
        "5m":  ("5m", None, "1mo"),
        "15m": ("15m", None, "2mo"),
        "30m": ("30m", None, "2mo"),
        "1h":  ("60m", None, "6mo"),
        "4h":  ("60m", "4h", "1y"),
        "1D":  ("1d", None, "3y"),
        "1W":  ("1wk", None, "5y"),
        "1M":  ("1mo", None, "10y"),
    }

    def __init__(self, poll_interval: float = 5.0) -> None:
        self._on_tick: Optional[Callable[[dict], None]] = None
        self._subs: set[str] = set()
        self._lock = threading.Lock()
        self._running = False
        self._poll_interval = poll_interval
        self._thread: Optional[threading.Thread] = None
        self._last_price: dict[str, float] = {}

    def get_candles(self, symbol: str, interval: str, limit: int = 300) -> list[dict]:
        import pandas as pd
        import yfinance as yf

        yf_interval, resample, period = self.INTERVAL_MAP.get(interval, (None, None, None))
        if yf_interval is None:
            return []

        try:
            df = yf.Ticker(symbol).history(interval=yf_interval, period=period,
                                           auto_adjust=False)
        except Exception as exc:
            print(f"[yfinance] history failed for {symbol} {interval}: {exc}")
            return []

        if df is None or df.empty:
            return []

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()

        if resample:
            df = df.resample(resample).agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }).dropna()

        idx = df.index
        if getattr(idx, "tz", None) is not None:
            ts = idx.tz_convert("UTC")
        else:
            ts = idx.tz_localize("UTC")
        epochs = (ts.asi8 // 1_000_000_000).tolist()

        candles = []
        for t, (_, row) in zip(epochs, df.iterrows()):
            candles.append({
                "time": int(t),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": float(row["Volume"]),
            })
        return candles[-limit:]

    def get_quote(self, symbol: str) -> Optional[dict]:
        import yfinance as yf
        try:
            t = yf.Ticker(symbol)
            # Use history() rather than fast_info (fast_info raises
            # 'currentTradingPeriod' on some Yahoo responses).
            price = None
            for period, interval in (("1d", "1m"), ("5d", "5m"), ("1mo", "1d")):
                try:
                    hist = t.history(period=period, interval=interval)
                except Exception:
                    continue
                if hist is not None and not hist.empty:
                    price = float(hist["Close"].dropna().iloc[-1])
                    break
            if price is None:
                return None
            self._last_price[symbol] = price
            return {"price": price, "time": int(time.time())}
        except Exception as exc:
            print(f"[yfinance] quote failed for {symbol}: {exc}")
            return None

    def supports_options(self, symbol: str) -> bool:
        meta = catalog.get_symbol_meta(symbol)
        return bool(meta and meta.get("options"))

    def get_option_chain(self, symbol: str) -> Optional[dict]:
        """Expiry-grouped CE/PE chain: for each expiry, 5 strikes each side of ATM.
        Token = "UNDERLYING|YYYY-MM-DD|STRIKE|CE"."""
        if not self.supports_options(symbol):
            return None

        quote = self.get_quote(symbol)
        if not quote:
            return None
        spot = quote["price"]
        step = catalog.suggest_strike_step(symbol, spot)
        atm = _round_to_step(spot, step)
        meta = catalog.get_symbol_meta(symbol) or {}
        base_label = meta.get("label", symbol)

        expiries = []
        for e in build_expiries(n_weekly=4, n_monthly=2):
            options = []
            for i in range(-5, 6):
                strike = atm + i * step
                if strike <= 0:
                    continue
                strike_disp = int(strike) if float(strike).is_integer() else round(strike, 2)
                for opt_type in ("CE", "PE"):
                    options.append({
                        "strike": strike_disp,
                        "type": opt_type,
                        "atm": (i == 0),
                        "token": f"{symbol}|{e['date']}|{strike_disp}|{opt_type}",
                        "label": f"{base_label} {e['date']} {strike_disp} {opt_type}",
                    })
            expiries.append({**e, "options": options})

        return {
            "underlying": symbol,
            "spot": round(spot, 2),
            "atm": int(atm) if float(atm).is_integer() else round(atm, 2),
            "step": step,
            "expiries": expiries,
        }

    def start(self, on_tick: Callable[[dict], None]) -> None:
        self._on_tick = on_tick
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def subscribe(self, symbol: str) -> None:
        with self._lock:
            self._subs.add(symbol)

    def unsubscribe(self, symbol: str) -> None:
        with self._lock:
            self._subs.discard(symbol)

    def _poll_loop(self) -> None:
        while self._running:
            with self._lock:
                current = list(self._subs)
            for symbol in current:
                quote = self.get_quote(symbol)
                if quote and self._on_tick:
                    self._on_tick({
                        "provider": self.name,
                        "symbol": symbol,
                        "price": quote["price"],
                        "time": quote["time"],
                        "volume": 0.0,
                    })
            time.sleep(self._poll_interval)

# ===========================================================================
# Option-token helpers
# ===========================================================================
def parse_option_token(token: str) -> Optional[dict]:
    """Parse an option token; returns None for plain symbols.
    Formats: UNDERLYING|YYYY-MM-DD|STRIKE|CE  (current)
             UNDERLYING|STRIKE|CE             (legacy)."""
    parts = token.split("|")
    if len(parts) == 4:
        underlying, expiry, strike, opt_type = parts
        return {"underlying": underlying, "expiry": expiry,
                "strike": strike, "type": opt_type}
    if len(parts) == 3:
        underlying, strike, opt_type = parts
        return {"underlying": underlying, "expiry": None,
                "strike": strike, "type": opt_type}
    return None

# ===========================================================================
# Registry wiring -- the only place providers are instantiated.
# ===========================================================================
hyperliquid = HyperliquidProvider()
yfinance_provider = YFinanceProvider()

register_provider(hyperliquid)
register_provider(yfinance_provider)
