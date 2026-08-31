# MOMENTUM SCREENER

Multi-factor swing & intraday signal engine.
Signals: ADX · RSI · MACD · RVOL · ATR · Bollinger Squeeze · EMA structure

---

## INSTALL

```bash
pip install yfinance pandas tabulate colorama flask flask-cors
```

---

## TERMINAL MODE

```bash
# Default watchlist (FLEX, AMD, JBL, COCO, MPC)
python momentum_screener.py

# Custom tickers
python momentum_screener.py FLEX AMD NVDA MSFT GOOGL

# Preset watchlists
python momentum_screener.py --preset ai
python momentum_screener.py --preset momentum
python momentum_screener.py --preset energy
python momentum_screener.py --preset mag7
python momentum_screener.py --preset penny_vol
python momentum_screener.py --preset penny_bio
python momentum_screener.py --preset penny_tech

# Auto-refresh every 5 minutes
python momentum_screener.py --watch
python momentum_screener.py --preset ai --watch
```

---

## API SERVER MODE (connects to HTML frontend)

```bash
# Start on default port 5000
python momentum_screener.py --serve

# Start on custom port (use if 5000 is taken — common on Mac)
python momentum_screener.py --serve --port 8080
```

### API Endpoints

| Method | URL | Description |
|--------|-----|-------------|
| GET | `/api/quote?ticker=FLEX` | Single ticker — fetch + analyze |
| GET | `/api/regime` | SPY / QQQ / VIX regime data |
| GET | `/api/scan?tickers=FLEX,AMD,JBL` | Bulk scan, returns ranked JSON |
| GET | `/api/scan?preset=watchlist` | Scan a named preset |

### Test the API
```bash
curl "http://localhost:5000/api/quote?ticker=FLEX"
curl "http://localhost:5000/api/scan?preset=watchlist"
curl "http://localhost:5000/api/regime"
```

---

## HTML FRONTEND

### Option A — localhost (recommended, no CORS issues)
```bash
# In the folder containing momentum-screener.html
python -m http.server 8080
# Then open: http://localhost:8080/momentum-screener.html
```

### Option B — connect to Python backend
1. Start the server: `python momentum_screener.py --serve`
2. Open `momentum-screener.html` in any editor
3. Change **line 1** of the `<script>` block:
```js
const USE_PYTHON_API = true;   // was false
```
4. If using a custom port, also update:
```js
const PYTHON_API_BASE = 'http://localhost:8080/api';
```

---

## WHEN TO RUN

| Window | Time (ET) | Action |
|--------|-----------|--------|
| ✅ Best | 9:30 – 10:30 AM | Enter breakouts. Highest volume. |
| ⚠ Avoid | 11:30 AM – 2:00 PM | Midday chop. No new entries. |
| ✅ Good | 3:00 – 4:00 PM | Power hour. Trail stops or exit. |

---

## SIGNAL THRESHOLDS

| Signal | Bullish Condition |
|--------|------------------|
| ADX | > 25 (trend exists) · > 35 (strong) |
| RSI | 55–70 (momentum building, not exhausted) |
| RVOL | > 1.5x average · > 2x institutional |
| MACD | Positive histogram |
| vs 20 EMA | Price above, within 8% (not extended) |
| BB Squeeze | Bands at multi-week low → explosion setup |

---

## PENNY STOCK RULES

> Only trade pennies when ALL of the following are true:

- Regime is **RISK ON** (SPY + QQQ both green, VIX < 20)
- RVOL **> 2x** (real volume, not noise)
- ADX **> 25** (trend has structure)
- **Size down 50%+** vs normal position
- **No overnight holds** — exit same day
- Stop loss is **mandatory** — use ATR-based stop shown on card

---

## CONFIDENCE SCORE

| Score | Meaning |
|-------|---------|
| 65–95 | High conviction — enter on open or power hour |
| 45–64 | Moderate — wait for RVOL confirmation |
| 10–44 | Low — observe only, skip |

---

## FILES

```
momentum_screener.py      ← Python CLI + API server
momentum-screener.html    ← Browser frontend
README.md                 ← This file
```

---

*Not financial advice. Always use stop-losses. Past performance does not predict future results.*