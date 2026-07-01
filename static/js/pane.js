/* pane.js -- one self-contained chart pane:
   category -> symbol -> (expiry -> strike) + timeframe, chart, ticker, label. */
(function (global) {
  "use strict";

  const TF_SECONDS = API.TF_SECONDS;
  const YEAR_SECONDS = 365.25 * 24 * 3600;

  function fmtPrice(v) {
    if (v == null || isNaN(v)) return "--";
    if (v >= 1000) return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (v >= 1) return v.toFixed(2);
    return v.toPrecision(5);
  }

  /* ---- Black-Scholes (matches data_source.py) for live option premiums ---- */
  function erf(x) {
    const s = x < 0 ? -1 : 1;
    x = Math.abs(x);
    const a1 = 0.254829592, a2 = -0.284496736, a3 = 1.421413741,
          a4 = -1.453152027, a5 = 1.061405429, p = 0.3275911;
    const t = 1 / (1 + p * x);
    const y = 1 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-x * x);
    return s * y;
  }
  function normCdf(x) { return 0.5 * (1 + erf(x / Math.SQRT2)); }
  function bsPrice(S, K, T, r, sigma, call) {
    if (!(S > 0) || !(K > 0)) return 0;
    if (T <= 0 || sigma <= 0) return Math.max(0, call ? S - K : K - S);
    const d1 = (Math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * Math.sqrt(T));
    const d2 = d1 - sigma * Math.sqrt(T);
    if (call) return S * normCdf(d1) - K * Math.exp(-r * T) * normCdf(d2);
    return K * Math.exp(-r * T) * normCdf(-d2) - S * normCdf(-d1);
  }

  class Pane {
    constructor(id, dashboard, state) {
      state = state || {};
      this.id = id;
      this.dashboard = dashboard;
      this.category = state.category || (state.symbol ? dashboard.categoryOf(state.symbol) : null);
      this.symbol = state.symbol || null;         // underlying ticker
      this.expiryKey = state.expiryKey || null;   // selected expiry key
      this.optionToken = state.optionToken || ""; // "" = underlying
      this.optionLabel = state.optionLabel || "";
      this.timeframe = state.timeframe || "1m";

      this.chain = null;        // fetched option chain
      this.optionMeta = null;   // {strike,type,expiry_epoch,sigma,r} for live pricing
      this.lastPrice = null;    // last displayed price (option or underlying)
      this.candles = [];
      this.loadToken = 0;

      this._build();
    }

    /* ---------- DOM ---------- */
    _build() {
      const tpl = document.getElementById("paneTemplate");
      this.el = tpl.content.firstElementChild.cloneNode(true);
      this.el.dataset.paneId = this.id;

      this.tickerEl = this.el.querySelector(".ticker");
      this.symbolLabelEl = this.el.querySelector(".ticker-symbol");
      this.priceEl = this.el.querySelector(".ticker-price");
      this.changeEl = this.el.querySelector(".ticker-change");

      this.categorySel = this.el.querySelector(".sel-category");
      this.symbolSel = this.el.querySelector(".sel-symbol");
      this.expirySel = this.el.querySelector(".sel-expiry");
      this.optionSel = this.el.querySelector(".sel-option");
      this.tfSel = this.el.querySelector(".sel-timeframe");
      this.chartHost = this.el.querySelector(".chart-host");
      this.labelEl = this.el.querySelector(".chart-label");

      this._populateCategorySelect();
      this._populateSymbolSelect(this.category);
      this._populateTimeframeSelect();

      this.categorySel.addEventListener("change", () => this._onCategoryChange());
      this.symbolSel.addEventListener("change", () => this._onSymbolChange());
      this.expirySel.addEventListener("change", () => this._onExpiryChange());
      this.optionSel.addEventListener("change", () => this._onOptionChange());
      this.tfSel.addEventListener("change", () => this._onTimeframeChange());
    }

    mount(parent) {
      parent.appendChild(this.el);
      this._initChart();
    }

    _initChart() {
      this.chart = LightweightCharts.createChart(this.chartHost, {
        autoSize: true,
        layout: { background: { color: "#131722" }, textColor: "#d1d4dc", fontSize: 11 },
        grid: { vertLines: { color: "#1c2230" }, horzLines: { color: "#1c2230" } },
        rightPriceScale: { borderColor: "#2a2f3a" },
        timeScale: { borderColor: "#2a2f3a", timeVisible: true, secondsVisible: false },
        crosshair: { mode: 0 },
      });
      this.series = this.chart.addCandlestickSeries({
        upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
        wickUpColor: "#26a69a", wickDownColor: "#ef5350",
      });
    }

    /* ---------- dropdown population ---------- */
    _populateCategorySelect() {
      this.categorySel.innerHTML = "";
      const ph = document.createElement("option");
      ph.value = ""; ph.textContent = "Category…";
      this.categorySel.appendChild(ph);
      (this.dashboard.catalog.groups || []).forEach((g) => {
        const o = document.createElement("option");
        o.value = g.name; o.textContent = g.name;
        this.categorySel.appendChild(o);
      });
      if (this.category) this.categorySel.value = this.category;
    }

    _populateSymbolSelect(category) {
      this.symbolSel.innerHTML = "";
      const ph = document.createElement("option");
      ph.value = "";
      ph.textContent = category ? "Symbol…" : "Pick a category first";
      this.symbolSel.appendChild(ph);

      const group = (this.dashboard.catalog.groups || []).find((g) => g.name === category);
      if (group) {
        group.items.forEach((item) => {
          const o = document.createElement("option");
          o.value = item.symbol;
          o.textContent = item.label;
          o.dataset.hasOptions = item.options ? "1" : "0";
          this.symbolSel.appendChild(o);
        });
      }
      this.symbolSel.disabled = !category;
      if (this.symbol) this.symbolSel.value = this.symbol;
    }

    _populateTimeframeSelect() {
      this.tfSel.innerHTML = "";
      API.TIMEFRAMES.forEach((tf) => {
        const o = document.createElement("option");
        o.value = tf; o.textContent = tf;
        this.tfSel.appendChild(o);
      });
      this.tfSel.value = this.timeframe;
    }

    _clearOptionControls() {
      this.chain = null;
      this.expirySel.innerHTML = ""; this.expirySel.disabled = true;
      this.optionSel.innerHTML = ""; this.optionSel.disabled = true;
      const none = document.createElement("option");
      none.value = ""; none.textContent = "—";
      this.optionSel.appendChild(none);
    }

    async _loadOptionChain() {
      this._clearOptionControls();
      if (!this.symbol) return;
      const meta = this.dashboard.symbolMeta(this.symbol);
      if (!meta || !meta.options) return;   // crypto / non-F&O -> no options

      let chain;
      try {
        chain = await API.fetchOptions(this.symbol);
      } catch (err) {
        console.warn("options load failed", err);
        return;
      }
      if (!chain || !chain.supported || !chain.expiries || !chain.expiries.length) return;

      this.chain = chain;
      this._populateExpirySelect();
      // Restore or default the expiry, then its strikes.
      if (!this.expiryKey || !chain.expiries.some((e) => e.key === this.expiryKey)) {
        this.expiryKey = chain.expiries[0].key;
      }
      this.expirySel.value = this.expiryKey;
      this._populateStrikeSelect(this._currentExpiry());
    }

    _populateExpirySelect() {
      this.expirySel.innerHTML = "";
      const weekly = this.chain.expiries.filter((e) => e.kind === "weekly");
      const monthly = this.chain.expiries.filter((e) => e.kind === "monthly");
      const addGroup = (label, list) => {
        if (!list.length) return;
        const og = document.createElement("optgroup");
        og.label = label;
        list.forEach((e) => {
          const o = document.createElement("option");
          o.value = e.key; o.textContent = e.label;
          og.appendChild(o);
        });
        this.expirySel.appendChild(og);
      };
      addGroup("Weekly Expiry", weekly);
      addGroup("Monthly Expiry", monthly);
      this.expirySel.disabled = false;
    }

    _currentExpiry() {
      if (!this.chain) return null;
      return this.chain.expiries.find((e) => e.key === this.expiryKey) || this.chain.expiries[0];
    }

    _populateStrikeSelect(expiry) {
      this.optionSel.innerHTML = "";
      const none = document.createElement("option");
      none.value = ""; none.textContent = "Underlying (no option)";
      this.optionSel.appendChild(none);
      if (!expiry) { this.optionSel.disabled = true; return; }

      const calls = expiry.options.filter((o) => o.type === "CE");
      const puts = expiry.options.filter((o) => o.type === "PE");
      const addGroup = (label, list) => {
        const og = document.createElement("optgroup");
        og.label = label;
        list.forEach((o) => {
          const opt = document.createElement("option");
          opt.value = o.token;
          opt.textContent = `${o.strike} ${o.type}${o.atm ? "  \u2605ATM" : ""}`;
          og.appendChild(opt);
        });
        this.optionSel.appendChild(og);
      };
      addGroup(`Calls (CE)  ATM ${this.chain.atm}`, calls);
      addGroup(`Puts (PE)  spot ${this.chain.spot}`, puts);
      this.optionSel.disabled = false;

      // Preserve the selected token if it exists under this expiry.
      if (this.optionToken && expiry.options.some((o) => o.token === this.optionToken)) {
        this.optionSel.value = this.optionToken;
      } else {
        this.optionToken = "";
        this.optionSel.value = "";
      }
    }

    /* ---------- events ---------- */
    async _onCategoryChange() {
      this.category = this.categorySel.value || null;
      this.symbol = null;
      this.expiryKey = null;
      this.optionToken = "";
      this.optionLabel = "";
      this.optionMeta = null;
      this._populateSymbolSelect(this.category);
      this._clearOptionControls();
      this.candles = []; this.series.setData([]);
      this._refreshLabels();
      this.dashboard.onPaneChanged();
    }

    async _onSymbolChange() {
      this.symbol = this.symbolSel.value || null;
      this.expiryKey = null;
      this.optionToken = "";
      this.optionLabel = "";
      this.optionMeta = null;
      this.lastPrice = null;
      await this._loadOptionChain();
      this._refreshLabels();
      await this.loadData();
      this.dashboard.onPaneChanged();
    }

    _onExpiryChange() {
      this.expiryKey = this.expirySel.value || null;
      this._populateStrikeSelect(this._currentExpiry());
      // If the strike no longer applies, we've reverted to underlying; reload.
      this._onOptionChange();
    }

    async _onOptionChange() {
      this.optionToken = this.optionSel.value || "";
      this.optionLabel = this.optionToken
        ? this.optionSel.options[this.optionSel.selectedIndex].textContent.trim()
        : "";
      if (!this.optionToken) this.optionMeta = null;
      this.lastPrice = null;
      this._refreshLabels();
      await this.loadData();
      this.dashboard.onPaneChanged();
    }

    async _onTimeframeChange() {
      this.timeframe = this.tfSel.value;
      this.chart.applyOptions({ timeScale: { secondsVisible: TF_SECONDS[this.timeframe] < 60 } });
      await this.loadData();
      this.dashboard.onPaneChanged();
    }

    /* ---------- labels ---------- */
    _instrumentLabel() {
      if (!this.symbol) return "—";
      const meta = this.dashboard.symbolMeta(this.symbol);
      const base = meta ? meta.label : this.symbol;
      if (this.optionToken) {
        const exp = this._currentExpiry();
        const strikeType = this.optionLabel.replace("  \u2605ATM", " ATM");
        return `${base} ${strikeType}${exp ? " · " + exp.date : ""}`;
      }
      return base;
    }

    _refreshLabels() {
      const txt = this._instrumentLabel();
      this.symbolLabelEl.textContent = txt;
      this.labelEl.textContent = this.symbol ? txt : "";
      this.labelEl.classList.toggle("opt", !!this.optionToken);
    }

    /* ---------- data ---------- */
    async loadData() {
      if (!this.symbol) { this.series.setData([]); this.candles = []; return; }
      const chartSymbol = this.optionToken || this.symbol;
      const token = ++this.loadToken;
      try {
        const res = await API.fetchCandles(chartSymbol, this.timeframe);
        if (token !== this.loadToken) return; // stale response
        this.optionMeta = res.option || null;  // present only for option tokens
        this.candles = (res.candles || []).slice();
        this.series.setData(this.candles.map((c) => ({
          time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
        })));
        this.chart.timeScale().fitContent();
        this._refreshLabels();
        if (this.candles.length) {
          this.lastPrice = this.candles[this.candles.length - 1].close;
          this._renderPrice(this.lastPrice);
        } else {
          this.priceEl.textContent = "--";
        }
      } catch (err) {
        if (token === this.loadToken) console.warn("candles load failed", chartSymbol, err);
      }
    }

    /* ---------- live ticks ---------- */
    onTick(tick) {
      // Ticks arrive keyed by the underlying symbol.
      if (!this.symbol || tick.symbol !== this.symbol) return;

      // For an option, convert the underlying price into a live premium.
      let price = tick.price;
      if (this.optionToken && this.optionMeta) {
        const m = this.optionMeta;
        const T = Math.max(0, (m.expiry_epoch - tick.time) / YEAR_SECONDS);
        price = bsPrice(tick.price, m.strike, T, m.r, m.sigma,
                        String(m.type).toUpperCase() === "CE");
      }

      const prev = this.lastPrice;
      const delta = prev == null ? 0 : price - prev;
      this._renderPrice(price);
      this._flash(delta);
      this.lastPrice = price;
      this._updateLiveCandle(tick.time, price, tick.volume || 0);
    }

    _updateLiveCandle(timeSec, price, volume) {
      const tf = TF_SECONDS[this.timeframe] || 60;
      const bucket = Math.floor(timeSec / tf) * tf;
      const last = this.candles[this.candles.length - 1];

      if (!last || bucket > last.time) {
        this.candles.push({ time: bucket, open: price, high: price, low: price, close: price, volume });
        this.series.update({ time: bucket, open: price, high: price, low: price, close: price });
      } else if (bucket === last.time) {
        last.high = Math.max(last.high, price);
        last.low = Math.min(last.low, price);
        last.close = price;
        this.series.update({ time: last.time, open: last.open, high: last.high, low: last.low, close: last.close });
      }
    }

    /* ---------- ticker strip ---------- */
    _renderPrice(price) {
      this.priceEl.textContent = fmtPrice(price);
      if (this.candles.length) {
        const first = this.candles[0].open;
        const pct = first ? ((price - first) / first) * 100 : 0;
        const sign = pct >= 0 ? "+" : "";
        this.changeEl.textContent = `${sign}${pct.toFixed(2)}%`;
        this.changeEl.classList.toggle("up", pct >= 0);
        this.changeEl.classList.toggle("down", pct < 0);
      }
    }

    _flash(delta) {
      if (delta === 0) return;
      const cls = delta > 0 ? "flash-up" : "flash-down";
      this.tickerEl.classList.remove("flash-up", "flash-down");
      void this.tickerEl.offsetWidth; // reflow so the transition re-triggers
      this.tickerEl.classList.add(cls);
      clearTimeout(this._flashTimer);
      this._flashTimer = setTimeout(() => {
        this.tickerEl.classList.remove("flash-up", "flash-down");
      }, 300);
    }

    /* ---------- lifecycle ---------- */
    async init() {
      if (this.category) this.categorySel.value = this.category;
      this._populateSymbolSelect(this.category);
      if (this.symbol) {
        this.symbolSel.value = this.symbol;
        await this._loadOptionChain();
        if (this.chain && this.expiryKey) this.expirySel.value = this.expiryKey;
        if (this.optionToken) this.optionSel.value = this.optionToken;
      }
      this.tfSel.value = this.timeframe;
      this.chart.applyOptions({ timeScale: { secondsVisible: TF_SECONDS[this.timeframe] < 60 } });
      this._refreshLabels();
      await this.loadData();
    }

    destroy() {
      try { if (this.chart) this.chart.remove(); } catch (_) {}
      if (this.el && this.el.parentNode) this.el.parentNode.removeChild(this.el);
    }

    getState() {
      return {
        category: this.category,
        symbol: this.symbol,
        expiryKey: this.expiryKey,
        optionToken: this.optionToken,
        optionLabel: this.optionLabel,
        timeframe: this.timeframe,
      };
    }
  }

  global.Pane = Pane;
})(window);
