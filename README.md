# Trading Dashboard

A local, web-based multi-pane trading dashboard built with
[Lightweight Charts](https://tradingview.github.io/lightweight-charts/),
a thin Flask backend, and a fully pluggable data layer.

- **Crypto** prices stream live from the **Hyperliquid** public websocket.
- **Indian indices & stocks** are pulled via **yfinance**.
- Runs **entirely on your machine** — nothing is deployed, and the default data
  sources need **no API keys**.

---

## Features

- **Split-screen grid** with a chart-count selector at the top: **1, 2, 4, 6, 8**
  (capped at 8). The grid auto-arranges:
  - 1 → full screen
  - 2 → side-by-side
  - 4 → 2×2
  - 6 → 3×2
  - 8 → 4×2
- **Layout persists** across page refreshes (chart count *and* every pane's
  symbol / option / timeframe are saved to `localStorage`).
- **Independent controls per pane** — three dropdowns each:
  1. **Symbol**, grouped into three sub-categories:
     - **Indian Index** (NIFTY 50, NIFTY BANK, SENSEX, …)
     - **Indian Stocks** (RELIANCE, TCS, HDFCBANK, …)
     - **Crypto** (BTC, ETH, SOL, … — the live Hyperliquid universe is merged in
       automatically on startup)
  2. **Options** — if the selected symbol has options, this populates the CE & PE
     contracts for **5 strikes above and below the current (ATM) strike**, with
     strike spacing chosen per instrument (NIFTY 50, BANK NIFTY 100, etc.).
  3. **Timeframe** — `15s, 30s, 1m, 3m, 5m, 15m, 30m, 1h, 4h, 1D, 1W, 1M`.
- **Ticker strip** on each pane that **flashes green on upticks / red on
  downticks** and shows the live price + session change %.

---

## Architecture

```
app.py            Flask server: serves the page + REST endpoints + SSE live stream
data_source.py    THE pluggable data layer (all market access lives here)
symbols.py        Symbol catalog (Indian Index / Indian Stocks / Crypto)
templates/
  index.html      Page shell + Lightweight Charts (loaded from CDN)
static/
  css/styles.css  Grid layouts + dark theme + flash animations
  js/api.js       Backend client + SSE LiveFeed
  js/pane.js      One self-contained chart pane (chart + 3 dropdowns + ticker)
  js/app.js       Dashboard: grid, panes, persistence, tick routing
```

Live data flow:

```
Hyperliquid websocket ─┐
                       ├─► broadcast() ─► SSE (/api/stream) ─► browser ─► panes
yfinance poll loop    ─┘
```

The browser builds the *forming* candle from the live tick stream, which is also
how sub-minute timeframes (15s / 30s) are produced.

### Adding a new broker (Alpaca / Binance / Zerodha / Polygon / …)

The entire integration surface is **one class + one registration** in
`data_source.py`:

```python
class BinanceProvider(BaseProvider):
    name = "binance"

    def get_candles(self, symbol, interval, limit=300):
        ...                       # return [{"time","open","high","low","close","volume"}, ...]

    # optional:
    def get_quote(self, symbol): ...
    def supports_options(self, symbol): return False
    def start(self, on_tick): ...         # push live ticks via on_tick(...)
    def subscribe(self, symbol): ...
    def unsubscribe(self, symbol): ...

register_provider(BinanceProvider())
```

Then point some symbols at it in `symbols.py` with `"provider": "binance"`.
No other file needs to change.

---

## Running locally

> Requires Python 3.10+ and internet access (for Hyperliquid + yfinance and the
> Lightweight Charts CDN script).

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Then open <http://127.0.0.1:5000> in your browser.

---

## Notes & limitations (read this)

- **Indian options data:** yfinance does **not** expose NSE option chains, so the
  Options dropdown is populated with *generated* ATM ± 5 strikes using NSE-style
  strike spacing. Because there's no free option-price feed, selecting an option
  **charts the underlying** (the response is flagged `is_option: true`,
  `note: "charting underlying …"`). Wire in a broker provider (e.g. Zerodha) to
  get real option candles — that's exactly the plug-and-play extension point
  above.
- **Sub-minute timeframes (15s / 30s):** neither Hyperliquid nor yfinance serve
  sub-minute history, so these panes start empty and **build candles live** from
  the incoming tick stream.
- **yfinance intraday limits:** Yahoo restricts how far back intraday data goes
  (e.g. 1-minute data ≈ last 7 days). yfinance quotes are slightly delayed and
  are polled every ~5s; this is intended for local/personal use, not HFT.
- **Hyperliquid** native candle intervals are used where available; `4h` for
  yfinance is resampled from 60-minute bars, and `3m` from 1-minute bars.


---

## Troubleshooting

**Blank page / charts never appear.** The page loads the Lightweight Charts
library from a CDN (with automatic fallback between unpkg and jsDelivr). If your
network blocks both, the grid shows a message instead of hanging. To run fully
offline, download the library once and self-host it:

1. Save this file:
   `https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js`
2. Put it at: `static/js/vendor/lightweight-charts.standalone.production.js`
3. Hard-refresh the browser (Ctrl+F5). The app already tries this local path last.

**Server log shows only `GET /` and no `/static/...` requests.** That means the
browser never fetched the CSS/JS — almost always the chart library CDN hanging
(see above) or an `index.html` that wasn't saved under `templates/`.

**`SyntaxError: unterminated string literal`.** A copy-paste artifact where `\n`
turned into real line breaks. Prefer cloning this repo over copy-pasting files.
