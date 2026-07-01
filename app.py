"""
app.py
------
Lightweight Flask server for the trading dashboard.

Responsibilities (kept deliberately thin -- all market knowledge lives in
data_source.py):

    GET  /                 -> the dashboard page
    GET  /api/symbols      -> grouped symbol catalog (Indian Index/Stocks/Crypto)
    GET  /api/candles      -> historical OHLC for a symbol+timeframe
    GET  /api/options      -> CE/PE option chain (5 strikes either side of ATM)
    POST /api/subscribe    -> set the symbols we want live updates for
    GET  /api/stream       -> Server-Sent-Events stream of live ticks
"""

from __future__ import annotations

import datetime as _dt
import json
import queue
import threading
import time

from flask import Flask, Response, jsonify, render_template, request

import data_source as ds
import symbols as catalog

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Live fan-out: a queue per connected SSE client.
# ---------------------------------------------------------------------------
_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()

# Symbols we currently want live updates for (union across all panes).
_desired: set[str] = set()
_desired_lock = threading.Lock()

def broadcast(tick: dict) -> None:
    """Push a tick to every connected SSE client (drop if a client is slow)."""
    msg = json.dumps(tick)
    with _clients_lock:
        for q in _clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

def _resolve_underlying(symbol: str) -> str:
    """Option tokens resolve to their underlying symbol."""
    opt = ds.parse_option_token(symbol)
    return opt["underlying"] if opt else symbol

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/symbols")
def api_symbols():
    return jsonify(catalog.build_catalog(extra_crypto=app.config.get("CRYPTO_UNIVERSE", [])))

@app.route("/api/candles")
def api_candles():
    symbol = request.args.get("symbol", "").strip()
    interval = request.args.get("interval", "1m").strip()
    limit = int(request.args.get("limit", 400))
    if not symbol:
        return jsonify({"error": "symbol required"}), 400

    opt = ds.parse_option_token(symbol)
    if not opt:
        # Plain symbol (index / stock / crypto).
        provider = ds.get_provider(symbol)
        return jsonify({
            "symbol": symbol, "underlying": symbol, "is_option": False,
            "interval": interval, "candles": provider.get_candles(symbol, interval, limit),
            "option": None, "note": None,
        })

    # Option token -> chart a theoretical Black-Scholes premium built from the
    # underlying's OHLC (no free NSE option-price feed exists).
    underlying = opt["underlying"]
    provider = ds.get_provider(underlying)
    base = provider.get_candles(underlying, interval, limit)

    try:
        strike = float(opt["strike"])
    except (TypeError, ValueError):
        strike = 0.0

    # Resolve the expiry timestamp (fall back to the nearest weekly expiry).
    expiry_epoch = None
    if opt.get("expiry"):
        try:
            expiry_epoch = ds._expiry_epoch(_dt.date.fromisoformat(opt["expiry"]))
        except Exception:
            expiry_epoch = None
    if expiry_epoch is None:
        exps = ds.build_expiries(n_weekly=1, n_monthly=0)
        expiry_epoch = exps[0]["epoch"] if exps else int(time.time()) + 7 * 86400

    tf_sec = ds.TIMEFRAME_SECONDS.get(interval, 60)
    sigma = ds.historical_volatility(base, tf_sec)
    candles = ds.build_option_candles(base, strike, opt["type"], expiry_epoch, sigma)

    return jsonify({
        "symbol": symbol,
        "underlying": underlying,
        "is_option": True,
        "interval": interval,
        "candles": candles,
        "option": {
            "strike": strike,
            "type": opt["type"],
            "expiry": opt.get("expiry"),
            "expiry_epoch": expiry_epoch,
            "sigma": round(sigma, 4),
            "r": ds.RISK_FREE_RATE,
        },
        "note": "theoretical Black-Scholes premium derived from the underlying",
    })

@app.route("/api/options")
def api_options():
    symbol = request.args.get("symbol", "").strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    provider = ds.get_provider(symbol)
    if not provider.supports_options(symbol):
        return jsonify({"underlying": symbol, "options": [], "supported": False})
    chain = provider.get_option_chain(symbol)
    if not chain:
        return jsonify({"underlying": symbol, "options": [], "supported": True,
                        "error": "could not build chain (no spot price)"})
    chain["supported"] = True
    return jsonify(chain)

@app.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    """Frontend posts the full set of symbols it wants live. We diff & route."""
    body = request.get_json(silent=True) or {}
    wanted_raw = body.get("symbols", [])
    wanted = {_resolve_underlying(s) for s in wanted_raw if s}

    with _desired_lock:
        to_add = wanted - _desired
        to_remove = _desired - wanted
        _desired.clear()
        _desired.update(wanted)

    for sym in to_add:
        ds.get_provider(sym).subscribe(sym)
    for sym in to_remove:
        ds.get_provider(sym).unsubscribe(sym)

    return jsonify({"subscribed": sorted(wanted)})

# Built from chr(10) instead of newline escapes so the source survives copy-paste.
_NL = chr(10)

@app.route("/api/stream")
def api_stream():
    def gen():
        q: queue.Queue = queue.Queue(maxsize=1000)
        with _clients_lock:
            _clients.append(q)
        try:
            yield "event: ready" + _NL + "data: {}" + _NL + _NL
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield "data: " + msg + _NL + _NL
                except queue.Empty:
                    yield ": keepalive" + _NL + _NL
        finally:
            with _clients_lock:
                if q in _clients:
                    _clients.remove(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
def bootstrap() -> None:
    universe = ds.hyperliquid.fetch_universe()
    app.config["CRYPTO_UNIVERSE"] = universe
    catalog.refresh_lookup(extra_crypto=universe)
    print(f"[startup] {len(universe)} crypto coins from Hyperliquid")

    ds.hyperliquid.start(broadcast)
    ds.yfinance_provider.start(broadcast)
    print("[startup] live providers running")

if __name__ == "__main__":
    bootstrap()
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
