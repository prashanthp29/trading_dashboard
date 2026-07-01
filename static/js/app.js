/* app.js -- dashboard controller: grid, panes, persistence, live feed routing. */
(function () {
  "use strict";

  const STORAGE_KEY = "trading-dashboard.v1";
  const VALID_COUNTS = [1, 2, 4, 6, 8];

  // Sensible starting layout the first time the page is opened.
  const DEFAULT_PANES = [
    { symbol: "BTC", timeframe: "1m" },
    { symbol: "ETH", timeframe: "1m" },
    { symbol: "^NSEI", timeframe: "5m" },
    { symbol: "RELIANCE.NS", timeframe: "5m" },
    { symbol: "SOL", timeframe: "1m" },
    { symbol: "^NSEBANK", timeframe: "5m" },
    { symbol: "DOGE", timeframe: "1m" },
    { symbol: "AVAX", timeframe: "1m" },
  ];

  class Dashboard {
    constructor() {
      this.catalog = { groups: [] };
      this.metaMap = {};
      this.panes = [];
      this.count = 4;

      this.gridEl = document.getElementById("grid");
      this.countSel = document.getElementById("chartCount");
      this.connEl = document.getElementById("connStatus");

      this.feed = new API.LiveFeed();
      this._subTimer = null;
      this._saveTimer = null;
    }

    async start() {
      this.catalog = await API.fetchSymbols();
      this._buildMetaMap();

      const saved = this._load();
      this.count = VALID_COUNTS.includes(saved.count) ? saved.count : 4;
      this.savedPanes = saved.panes || [];

      this.countSel.value = String(this.count);
      this.countSel.addEventListener("change", () => {
        const c = parseInt(this.countSel.value, 10);
        this.setCount(c);
        this._save();
      });

      this.feed.onStatus((ok) => this._setConn(ok));
      this.feed.onTick((tick) => this._routeTick(tick));
      this.feed.connect();

      await this.render();
      this._updateSubscriptions();
    }

    _buildMetaMap() {
      this.metaMap = {};
      (this.catalog.groups || []).forEach((g) =>
        g.items.forEach((it) => { this.metaMap[it.symbol] = it; })
      );
    }

    symbolMeta(symbol) {
      return this.metaMap[symbol] || null;
    }

    categoryOf(symbol) {
      if (!symbol) return null;
      const g = (this.catalog.groups || []).find((grp) =>
        grp.items.some((it) => it.symbol === symbol));
      return g ? g.name : null;
    }

    /* ---------- grid / panes ---------- */
    async render() {
      // Tear down existing panes.
      this.panes.forEach((p) => p.destroy());
      this.panes = [];
      this.gridEl.innerHTML = "";
      this.gridEl.dataset.count = String(this.count);

      const states = [];
      for (let i = 0; i < this.count; i++) {
        states.push(this.savedPanes[i] || DEFAULT_PANES[i] || {});
      }

      for (let i = 0; i < this.count; i++) {
        const pane = new Pane(i, this, states[i]);
        pane.mount(this.gridEl);
        this.panes.push(pane);
      }
      // Initialise (load data) after mount so charts size correctly.
      await Promise.all(this.panes.map((p) => p.init()));
    }

    async setCount(count) {
      if (!VALID_COUNTS.includes(count)) return;
      // Preserve the state of existing panes before re-rendering.
      this.savedPanes = this._collectStates(Math.max(count, this.panes.length));
      this.count = count;
      await this.render();
      this._updateSubscriptions();
    }

    _collectStates(n) {
      const states = [];
      for (let i = 0; i < n; i++) {
        if (this.panes[i]) states.push(this.panes[i].getState());
        else states.push(this.savedPanes[i] || DEFAULT_PANES[i] || {});
      }
      return states;
    }

    /* ---------- live feed routing ---------- */
    _routeTick(tick) {
      for (const pane of this.panes) pane.onTick(tick);
    }

    _setConn(ok) {
      this.connEl.classList.toggle("online", ok);
      this.connEl.classList.toggle("offline", !ok);
      this.connEl.textContent = ok ? "live" : "reconnecting";
    }

    onPaneChanged() {
      this._save();
      this._updateSubscriptions();
    }

    _updateSubscriptions() {
      // Debounce: collect the union of underlying symbols across panes.
      clearTimeout(this._subTimer);
      this._subTimer = setTimeout(() => {
        const symbols = [...new Set(this.panes.map((p) => p.symbol).filter(Boolean))];
        API.subscribe(symbols).catch((e) => console.warn("subscribe failed", e));
      }, 250);
    }

    /* ---------- persistence ---------- */
    _save() {
      clearTimeout(this._saveTimer);
      this._saveTimer = setTimeout(() => {
        const data = { count: this.count, panes: this._collectStates(this.count) };
        try { localStorage.setItem(STORAGE_KEY, JSON.stringify(data)); } catch (_) {}
      }, 150);
    }

    _load() {
      try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (raw) return JSON.parse(raw);
      } catch (_) {}
      return { count: 4, panes: [] };
    }
  }

  // Wait for the (async, fallback-loaded) chart library before starting so the
  // page never hangs blank; show a clear message if it can't be loaded at all.
  function whenChartsReady(cb) {
    const started = Date.now();
    (function check() {
      if (window.LightweightCharts) return cb();
      if (Date.now() - started > 20000) {
        document.getElementById("grid").innerHTML =
          '<div style="padding:24px;color:#ef5350;line-height:1.5">' +
          "Could not load the charting library — your network may be blocking the CDN.<br>" +
          "Fix: download <code>lightweight-charts.standalone.production.js</code> into " +
          "<code>static/js/vendor/</code> and refresh (see README).</div>";
        return;
      }
      setTimeout(check, 100);
    })();
  }

  window.addEventListener("DOMContentLoaded", () => {
    whenChartsReady(() => {
      const dash = new Dashboard();
      dash.start().catch((err) => {
        console.error("dashboard failed to start", err);
        document.getElementById("grid").innerHTML =
          '<div style="padding:20px;color:#ef5350">Failed to start. Is the Flask server running? See console.</div>';
      });
    });
  });
})();
