/* api.js -- thin client for the Flask backend + the live SSE feed. */
(function (global) {
  "use strict";

  const TIMEFRAMES = ["15s", "30s", "1m", "3m", "5m", "15m", "30m", "1h", "4h", "1D", "1W", "1M"];

  const TF_SECONDS = {
    "15s": 15, "30s": 30, "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "4h": 14400, "1D": 86400, "1W": 604800, "1M": 2592000,
  };

  async function getJSON(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url} -> ${res.status}`);
    return res.json();
  }

  const API = {
    TIMEFRAMES,
    TF_SECONDS,

    fetchSymbols() {
      return getJSON("/api/symbols");
    },

    fetchCandles(symbol, interval, limit = 400) {
      const q = new URLSearchParams({ symbol, interval, limit });
      return getJSON(`/api/candles?${q.toString()}`);
    },

    fetchOptions(symbol) {
      const q = new URLSearchParams({ symbol });
      return getJSON(`/api/options?${q.toString()}`);
    },

    subscribe(symbols) {
      return fetch("/api/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols }),
      });
    },
  };

  /* ---- Live tick stream (SSE) with auto-reconnect ---- */
  class LiveFeed {
    constructor() {
      this.handlers = new Set();
      this.statusHandlers = new Set();
      this.es = null;
    }

    onTick(fn) { this.handlers.add(fn); }
    onStatus(fn) { this.statusHandlers.add(fn); }

    _setStatus(ok) { this.statusHandlers.forEach((fn) => fn(ok)); }

    connect() {
      if (this.es) this.es.close();
      const es = new EventSource("/api/stream");
      this.es = es;

      es.addEventListener("ready", () => this._setStatus(true));
      es.onopen = () => this._setStatus(true);
      es.onmessage = (ev) => {
        if (!ev.data) return;
        let tick;
        try { tick = JSON.parse(ev.data); } catch (_) { return; }
        this.handlers.forEach((fn) => fn(tick));
      };
      es.onerror = () => {
        this._setStatus(false);
        // EventSource auto-reconnects; nothing else needed.
      };
    }
  }

  API.LiveFeed = LiveFeed;
  global.API = API;
})(window);
