"""
data_source.py
--------------
The single data-access abstraction for the dashboard.

Everything that knows how to talk to an external market (REST candles, live
streams, option chains) lives behind the ``BaseProvider`` interface and is
registered in the ``PROVIDERS`` registry.  The rest of the app (app.py, the
frontend) never imports a broker SDK directly -- it only ever asks the registry
for "the provider that owns symbol X".

------------------------------------------------------------------------------
PLUG-AND-PLAY: adding a new broker (Alpaca / Binance / Zerodha / Polygon / ...)
------------------------------------------------------------------------------
1.  Write a small subclass of ``BaseProvider`` (history is the only required
    method; options + live streaming are optional):

        class BinanceProvider(BaseProvider):
            name = "binance"
            def get_candles(self, symbol, interval, limit=300):
                ...                      # return list[Candle dict]
            # optional:
            def get_quote(self, symbol): ...
            def supports_options(self, symbol): return False
            def start(self, on_tick): ...        # push live ticks
            def subscribe(self, symbol): ...
            def unsubscribe(self, symbol): ...

2.  Register it once at the bottom of this file:

        register_provider(BinanceProvider())

3.  Point some symbols at it in symbols.py via ``"provider": "binance"``.

That's the entire integration surface -- one class, one ``register_provider``
call.  No other file needs to change.

------------------------------------------------------------------------------
Unified data shapes
------------------------------------------------------------------------------
Candle:  {"time": <unix seconds, int>, "open","high","low","close","volume": float}
Tick:    {"provider","symbol","price": float, "time": <unix seconds>, "volume": float}
Option:  {"strike","type":"CE"|"PE","label","token","data":"underlying"|"native"}
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

import symbols as catalog

# NOTE: `requests`, `yfinance`, `pandas` and `websocket` are imported lazily
# inside the methods that need them.  This keeps module import cheap and lets the
# pure-Python parts (symbol catalog, option-strike maths) be used/tested even if
# the network libraries are not installed yet.


# ===========================================================================
# Base interface + registry
# ===========================================================================
class BaseProvider:
    name: str = "base"
    supports_live: bool = False

    # --- history (required) ------------------------------------------------
    def get_candles(self, symbol: str, interval: str, limit: int = 300) -> list[dict]:
        raise NotImplementedError

    # --- latest quote (used by pollers / option-chain spot) ----------------
    def get_quote(self, symbol: str) -> Optional[dict]:
        return None

    # --- options (optional) ------------------------------------------------
    def supports_options(self, symbol: str) -> bool:
        return False

    def get_option_chain(self, symbol: str) -> Optional[dict]:
        return None

    # --- live streaming (optional) -----------------------------------------
    def start(self, on_tick: Callable[[dict], None]) -> None:
        """Begin streaming.  Call on_tick(tick) for every price update."""
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
# Dashboard timeframe -> seconds.  Sub-minute frames are built live in the
# browser from the trade/quote stream, so providers may legitimately return an
# empty history for them.
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
# Hyperliquid (crypto perps): REST candles + live websocket trades
# ===========================================================================
class HyperliquidProvider(BaseProvider):
    name = "hyperliquid"
    supports_live = True

    INFO_URL = "https://api.hyperliquid.xyz/info"
    WS_URL = "wss://api.hyperliquid.xyz/ws"

    # dashboard timeframe -> Hyperliquid candle interval (None = unsupported)
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

    # ---- universe ---------------------------------------------------------
    def fetch_universe(self) -> list[str]:
        import requests
        try:
            r = requests.post(self.INFO_URL, json={"type": "meta"}, timeout=10)
            r.raise_for_status()
            data = r.json()
            return [c["name"] for c in data.get("universe", []) if not c.get("isDelisted")]
        except Exception as exc:  # noqa: BLE001
            print(f"[hyperliquid] universe fetch failed: {exc}")
            return []

    # ---- history ----------------------------------------------------------
    def get_candles(self, symbol: str, interval: str, limit: int = 300) -> list[dict]:
        import requests
        hl_interval = self.INTERVAL_MAP.get(interval)
        if hl_interval is None:
            return []  # sub-minute: built live in the browser
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
        except Exception as exc:  # noqa: BLE001
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
        except Exception:  # noqa: BLE001
            pass
        return None

    # ---- live websocket ---------------------------------------------------
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
        except Exception:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
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
            except Exception:  # noqa: BLE001
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
            except Exception as exc:  # noqa: BLE001
                print(f"[hyperliquid] websocket loop error: {exc}")
            if self._running:
                time.sleep(3)  # reconnect backoff


# ===========================================================================
# yfinance (Indian indices + stocks): REST candles, polled quotes, options
# ===========================================================================
class YFinanceProvider(BaseProvider):
    name = "yfinance"
    supports_live = True  # via internal polling thread

    # dashboard timeframe -> (yfinance native interval, resample rule, period)
    # resample rule of None means use the native interval directly.
    INTERVAL_MAP = {
        "15s": (None, None, None),       # not available on free feed
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

    # ---- history ----------------------------------------------------------
    def get_candles(self, symbol: str, interval: str, limit: int = 300) -> list[dict]:
        import pandas as pd  # local import keeps startup fast
        import yfinance as yf

        yf_interval, resample, period = self.INTERVAL_MAP.get(interval, (None, None, None))
        if yf_interval is None:
            return []  # sub-minute equity history not available; built live

        try:
            df = yf.download(symbol, interval=yf_interval, period=period,
                             auto_adjust=False, progress=False, threads=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[yfinance] download failed for {symbol} {interval}: {exc}")
            return []

        if df is None or df.empty:
            return []

        # yfinance may return a column MultiIndex when a single ticker is given.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()

        if resample:
            df = df.resample(resample).agg({
                "Open": "first", "High": "max", "Low": "min",
                "Close": "last", "Volume": "sum",
            }).dropna()

        idx = df.index
        # Normalize to UTC unix seconds.
        if getattr(idx, "tz", None) is not None:
            ts = idx.tz_convert("UTC")
        else:
            ts = idx.tz_localize("UTC")
        # asi8 = int64 nanoseconds since the UNIX epoch (robust across pandas
        # versions; avoids the deprecated DatetimeIndex.view).
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
            # Use history() rather than fast_info: fast_info is what raises the
            # 'currentTradingPeriod' error on some Yahoo responses.
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


    # ---- options ----------------------------------------------------------
    def supports_options(self, symbol: str) -> bool:
        meta = catalog.get_symbol_meta(symbol)
        return bool(meta and meta.get("options"))

    def get_option_chain(self, symbol: str) -> Optional[dict]:
        """Build a CE/PE chain of 5 strikes above & below the ATM strike.

        yfinance does not expose NSE option chains, so strikes are generated
        around the live spot using NSE-style strike spacing (see
        symbols.suggest_strike_step).  Each option carries data="underlying",
        signalling that price history should fall back to the underlying until a
        broker provider (Zerodha/etc.) supplies real option candles.
        """
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
                    "label": f"{base_label} {strike_disp} {opt_type}",
                    "token": f"{symbol}|{strike_disp}|{opt_type}",
                    "data": "underlying",
                    "atm": (i == 0),
                })
        return {
            "underlying": symbol,
            "spot": round(spot, 2),
            "atm": int(atm) if float(atm).is_integer() else round(atm, 2),
            "step": step,
            "options": options,
        }

    # ---- live (polling) ---------------------------------------------------
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
# Option-token helpers (shared, provider-agnostic)
# ===========================================================================
def parse_option_token(token: str) -> Optional[dict]:
    """Split "UNDERLYING|STRIKE|CE" -> dict. Returns None if not an option."""
    parts = token.split("|")
    if len(parts) != 3:
        return None
    underlying, strike, opt_type = parts
    return {"underlying": underlying, "strike": strike, "type": opt_type}


# ===========================================================================
# Registry wiring -- the only place providers are instantiated.
# ===========================================================================
hyperliquid = HyperliquidProvider()
yfinance_provider = YFinanceProvider()

register_provider(hyperliquid)
register_provider(yfinance_provider)
