/* pane.js -- one self-contained chart pane: chart + 3 dropdowns + ticker strip. */
(function (global) {
  "use strict";

  const TF_SECONDS = API.TF_SECONDS;

  function fmtPrice(v) {
    if (v == null || isNaN(v)) return "--";
    if (v >= 1000) return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (v >= 1) return v.toFixed(2);
    return v.toPrecision(5);
  }

  class Pane {
    constructor(id, dashboard, state) {
      this.id = id;
      this.dashboard = dashboard;
      this.symbol = (state && state.symbol) || null;     // underlying ticker
      this.optionToken = (state && state.optionToken) || ""; // "" = underlying
      this.optionLabel = (state && state.optionLabel) || "";
      this.timeframe = (state && state.timeframe) || "1m";

      this.lastPrice = null;
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

      this.symbolSel = this.el.querySelector(".sel-symbol");
      this.optionSel = this.el.querySelector(".sel-option");
      this.tfSel = this.el.querySelector(".sel-timeframe");
      this.chartHost = this.el.querySelector(".chart-host");

      this._populateSymbolSelect();
      this._populateTimeframeSelect();

      this.symbolSel.addEventListener("change", () => this._onSymbolChange());
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
    _populateSymbolSelect() {
      const cat = this.dashboard.catalog;
      this.symbolSel.innerHTML = "";
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Select symbol…";
      this.symbolSel.appendChild(placeholder);

      (cat.groups || []).forEach((group) => {
        const og = document.createElement("optgroup");
        og.label = group.name;
        group.items.forEach((item) => {
          const opt = document.createElement("option");
          opt.value = item.symbol;
          opt.textContent = item.label;
          opt.dataset.hasOptions = item.options ? "1" : "0";
          og.appendChild(opt);
        });
        this.symbolSel.appendChild(og);
      });

      if (this.symbol) this.symbolSel.value = this.symbol;
    }

    _populateTimeframeSelect() {
      this.tfSel.innerHTML = "";
      API.TIMEFRAMES.forEach((tf) => {
        const opt = document.createElement("option");
        opt.value = tf;
        opt.textContent = tf;
        this.tfSel.appendChild(opt);
      });
      this.tfSel.value = this.timeframe;
    }

    async _populateOptionSelect() {
      this.optionSel.innerHTML = "";
      const none = document.createElement("option");
      none.value = "";
      none.textContent = this.symbol ? "Underlying (no option)" : "—";
      this.optionSel.appendChild(none);

      if (!this.symbol) { this.optionSel.disabled = true; return; }

      // Only fetch if catalog says this symbol has options.
      const meta = this.dashboard.symbolMeta(this.symbol);
      if (!meta || !meta.options) {
        this.optionSel.disabled = true;
        this.optionToken = "";
        return;
      }

      this.optionSel.disabled = true; // until loaded
      try {
        const chain = await API.fetchOptions(this.symbol);
        if (!chain.supported || !chain.options || !chain.options.length) {
          this.optionSel.disabled = true;
          return;
        }
        const calls = chain.options.filter((o) => o.type === "CE");
        const puts = chain.options.filter((o) => o.type === "PE");

        const addGroup = (label, list) => {
          const og = document.createElement("optgroup");
          og.label = label;
          list.forEach((o) => {
            const opt = document.createElement("option");
            opt.value = o.token;
            opt.textContent = `${o.strike} ${o.type}${o.atm ? "  ★ATM" : ""}`;
            og.appendChild(opt);
          });
          this.optionSel.appendChild(og);
        };
        addGroup(`Calls (CE)  spot ${chain.spot}`, calls);
        addGroup(`Puts (PE)  ATM ${chain.atm}`, puts);

        this.optionSel.disabled = false;
        if (this.optionToken) this.optionSel.value = this.optionToken;
      } catch (err) {
        console.warn("options load failed", err);
        this.optionSel.disabled = true;
      }
    }

    /* ---------- events ---------- */
    async _onSymbolChange() {
      this.symbol = this.symbolSel.value || null;
      this.optionToken = "";
      this.optionLabel = "";
      await this._populateOptionSelect();
      this._refreshTickerLabel();
      this.lastPrice = null;
      await this.loadData();
      this.dashboard.onPaneChanged();
    }

    async _onOptionChange() {
      this.optionToken = this.optionSel.value || "";
      this.optionLabel = this.optionToken
        ? this.optionSel.options[this.optionSel.selectedIndex].textContent.trim()
        : "";
      this._refreshTickerLabel();
      await this.loadData();
      this.dashboard.onPaneChanged();
    }

    async _onTimeframeChange() {
      this.timeframe = this.tfSel.value;
      const sub60 = TF_SECONDS[this.timeframe] < 60;
      this.chart.applyOptions({ timeScale: { secondsVisible: sub60 } });
      await this.loadData();
      this.dashboard.onPaneChanged();
    }

    _refreshTickerLabel() {
      if (!this.symbol) { this.symbolLabelEl.textContent = "—"; return; }
      const meta = this.dashboard.symbolMeta(this.symbol);
      const base = meta ? meta.label : this.symbol;
      this.symbolLabelEl.textContent = this.optionToken
        ? `${base} · ${this.optionLabel.replace("  ★ATM", " ATM")}`
        : base;
    }

    /* ---------- data ---------- */
    async loadData() {
      if (!this.symbol) { this.series.setData([]); this.candles = []; return; }
      const chartSymbol = this.optionToken || this.symbol;
      const token = ++this.loadToken;
      try {
        const res = await API.fetchCandles(chartSymbol, this.timeframe);
        if (token !== this.loadToken) return; // stale
        this.candles = (res.candles || []).slice();
        this.series.setData(this.candles.map((c) => ({
          time: c.time, open: c.open, high: c.high, low: c.low, close: c.close,
        })));
        this.chart.timeScale().fitContent();
        if (this.candles.length) {
          this.lastPrice = this.candles[this.candles.length - 1].close;
          this._renderPrice(this.lastPrice, 0);
        }
      } catch (err) {
        if (token === this.loadToken) console.warn("candles load failed", chartSymbol, err);
      }
    }

    /* ---------- live ticks ---------- */
    onTick(tick) {
      // Ticks are keyed by underlying symbol (options chart the underlying).
      if (!this.symbol || tick.symbol !== this.symbol) return;

      const price = tick.price;
      const prev = this.lastPrice;
      this._renderPrice(price, prev == null ? 0 : price - prev);
      this._flash(prev == null ? 0 : price - prev);
      this.lastPrice = price;

      this._updateLiveCandle(tick.time, price, tick.volume || 0);
    }

    _updateLiveCandle(timeSec, price, volume) {
      const tf = TF_SECONDS[this.timeframe] || 60;
      const bucket = Math.floor(timeSec / tf) * tf;
      const last = this.candles[this.candles.length - 1];

      if (!last || bucket > last.time) {
        const candle = { time: bucket, open: price, high: price, low: price, close: price, volume };
        this.candles.push(candle);
        this.series.update({ time: bucket, open: price, high: price, low: price, close: price });
      } else if (bucket === last.time) {
        last.high = Math.max(last.high, price);
        last.low = Math.min(last.low, price);
        last.close = price;
        this.series.update({ time: last.time, open: last.open, high: last.high, low: last.low, close: last.close });
      }
    }

    /* ---------- ticker strip ---------- */
    _renderPrice(price, delta) {
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
      // force reflow so re-adding the same class re-triggers the transition
      void this.tickerEl.offsetWidth;
      this.tickerEl.classList.add(cls);
      clearTimeout(this._flashTimer);
      this._flashTimer = setTimeout(() => {
        this.tickerEl.classList.remove("flash-up", "flash-down");
      }, 300);
    }

    /* ---------- lifecycle ---------- */
    async init() {
      if (this.symbol) {
        this.symbolSel.value = this.symbol;
        await this._populateOptionSelect();
        if (this.optionToken) this.optionSel.value = this.optionToken;
      }
      this.tfSel.value = this.timeframe;
      this.chart.applyOptions({ timeScale: { secondsVisible: TF_SECONDS[this.timeframe] < 60 } });
      this._refreshTickerLabel();
      await this.loadData();
    }

    destroy() {
      try { if (this.chart) this.chart.remove(); } catch (_) {}
      if (this.el && this.el.parentNode) this.el.parentNode.removeChild(this.el);
    }

    getState() {
      return {
        symbol: this.symbol,
        optionToken: this.optionToken,
        optionLabel: this.optionLabel,
        timeframe: this.timeframe,
      };
    }
  }

  global.Pane = Pane;
})(window);
