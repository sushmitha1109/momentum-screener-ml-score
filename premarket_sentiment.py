#!/usr/bin/env python3
"""
PRE-MARKET SENTIMENT SCREENER
==============================
Scans overnight/early-morning news via Finnhub, scores a curated watchlist
for bullish sentiment, confirms with Alpaca pre-market price data,
and outputs tradeable signals with entry/stop/TP levels.

Can auto-submit to your existing paper_trader.py.

Usage:
  python premarket_sentiment.py --scan            # display results
  python premarket_sentiment.py --auto-trade      # scan + submit to Alpaca
  python premarket_sentiment.py --json            # JSON output
  python premarket_sentiment.py --top 3           # top N only
  python premarket_sentiment.py --scan --min-bull 60  # stricter filter

Setup:
  export FINNHUB_API_KEY=your_key          # free → https://finnhub.io/register
  export ALPACA_API_KEY=your_key           # free → https://alpaca.markets
  export ALPACA_SECRET_KEY=your_secret     # (optional: for pre-market prices)

Cron (run at 8:30 AM ET every weekday):
  30 8 * * 1-5 cd /path/to/project && python premarket_sentiment.py --auto-trade >> /tmp/sentiment.log 2>&1
"""

import os
import sys
import json
import time
import re
import argparse
import requests
from datetime import datetime, timedelta, timezone

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FINNHUB_KEY   = os.environ.get("FINNHUB_API_KEY", "")
ALPACA_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")

FINNHUB_BASE = "https://finnhub.io/api/v1"
ALPACA_BASE  = "https://paper-api.alpaca.markets/v2"

NEWS_HOURS_BACK       = 14        # how many hours of news to scan
DEFAULT_TOP_N         = 5
FINNHUB_CALL_INTERVAL = 1.2       # seconds between calls (stay under 60/min)

# Sentiment filters — tweak these
MIN_BULLISH_PCT     = 50          # min % of bullish news
MIN_SENTIMENT_SCORE = 0.05        # min Finnhub sentimentScore (-1 to +1)
MIN_NEWS_COUNT      = 1           # min articles to consider a ticker

# Pre-market filters (0 = disabled, useful if you want price confirmation)
MIN_PREMARKET_CHG_PCT = 0.0       # min pre-market % gain
MIN_PREMARKET_VOL     = 0         # min pre-market share volume

# Ranking weights (should sum to 100)
W_SENTIMENT   = 30
W_BULLISH_PCT = 25
W_NEWS_COUNT  = 15
W_PREMKT_CHG  = 30

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TICKER UNIVERSE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Kept small on purpose — Finnhub's free tier is 60 calls/min, and each
# candidate ticker costs a /news-sentiment + /stock/profile2 call. Scanning
# the full S&P 500 burns through the quota for little benefit since only a
# handful of names show up in overnight news anyway. Same universe as the
# momentum screener's MAGIC_UNIVERSE["us"] + watchlist presets.

DEFAULT_UNIVERSE = list(dict.fromkeys([
    "NVDA","AMD","MSFT","AAPL","AMZN","META","GOOGL","TSLA","NFLX",
    "PLTR","SMCI","FLEX","JBL","DELL","HPE","ARM","AVGO",
    "MARA","RIOT","CLSK","BITF","HIVE","WULF",
    "JPM","GS","BAC","XOM","CVX","MPC","COP",
    "CRWD","SNOW","MDB","NET","DDOG","ZS","HUBS",
    "COCO","BABA",
]))


def get_universe_tickers(custom=None, max_size=60):
    """Return the ticker universe to scan: a custom list if provided
    (capped to max_size to protect the free-tier API quota), otherwise
    the curated DEFAULT_UNIVERSE."""
    if custom:
        cleaned = list(dict.fromkeys(t.strip().upper() for t in custom if t.strip()))
        return cleaned[:max_size]
    return list(DEFAULT_UNIVERSE)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FINNHUB CLIENT (rate-limited)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_last_call = 0.0


def _finnhub(path, params=None):
    global _last_call
    if not FINNHUB_KEY:
        raise ValueError(
            "FINNHUB_API_KEY not set. Get a free key at https://finnhub.io/register"
        )
    elapsed = time.time() - _last_call
    if elapsed < FINNHUB_CALL_INTERVAL:
        time.sleep(FINNHUB_CALL_INTERVAL - elapsed)

    p = {"token": FINNHUB_KEY, **(params or {})}
    r = requests.get(FINNHUB_BASE + path, params=p, timeout=15)
    _last_call = time.time()

    if r.status_code == 429:
        print("    ⚠ Rate-limited, waiting 6s...", flush=True)
        time.sleep(6)
        r = requests.get(FINNHUB_BASE + path, params=p, timeout=15)
        _last_call = time.time()

    if not r.ok:
        try:
            err = r.json().get("error", r.text[:200])
        except Exception:
            err = r.text[:200]
        raise RuntimeError(f"Finnhub {path}: {err}")

    return r.json()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ALPACA CLIENT (graceful — returns None if not configured)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _alpaca(path, params=None):
    if not ALPACA_KEY or not ALPACA_SECRET:
        return None
    try:
        r = requests.get(
            ALPACA_BASE + path,
            headers={
                "APCA-API-KEY-ID": ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            params=params,
            timeout=10,
        )
        if not r.ok:
            return None
        return r.json()
    except Exception:
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TIMEZONE HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _now_et():
    utc = datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return utc.astimezone(ZoneInfo("America/New_York"))
    except ImportError:
        return utc - timedelta(hours=5)


def _et_offset():
    """Return UTC offset string for ET, e.g. '-05:00' or '-04:00'."""
    et = _now_et()
    offset = et.utcoffset()
    total_secs = int(offset.total_seconds())
    sign = "-" if total_secs < 0 else "+"
    total_secs = abs(total_secs)
    h, rem = divmod(total_secs, 3600)
    m, _ = divmod(rem, 60)
    return f"{sign}{h:02d}:{m:02d}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# KEYWORD-BASED SENTIMENT (fallback when Finnhub sentiment API unavailable)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_BULLISH = {
    "surge","jump","beat","beats","rise","rising","upgrade","upgraded","buy",
    "growth","profit","profits","record","rally","bullish","strong","gain",
    "gains","soar","soars","climb","boost","outperform","raise","raised",
    "above","exceeded","exceeds","higher","highest","boom","accelerate",
    "upside","uptrend","breakthrough","partnership","deal","wins","won",
    "approval","approved","launch","launched","expansion","expand","demand",
    "optimistic","overweight","buy rating","strong buy","upgrade","upgraded",
    "dividend","hike","hikes","target raised","price target","buying",
    "revenue growth","eps","earnings beat","topped","surpass","surpassed",
}

_BEARISH = {
    "drop","drops","fall","falls","miss","missed","cut","cuts","downgrade",
    "downgraded","sell","loss","losses","decline","declining","bearish",
    "weak","plunge","crash","slump","warning","risk","below","lower",
    "lowest","layoff","layoffs","fired","sued","investigation","fraud",
    "debt","bankrupt","recall","delayed","delay","pessimistic","underweight",
    "sell rating","strong sell","short","shortage","deficit","tumble",
    "plummet","downgrade","downgraded","earnings miss","missed estimates",
    "revenue decline","negative","concern","concerns","fear","warning",
}


def _keyword_score(text):
    """Return -1.0 to +1.0 based on keyword hits."""
    words = set(re.findall(r"\b\w+\b", text.lower()))
    bull = len(words & _BULLISH)
    bear = len(words & _BEARISH)
    total = bull + bear
    if total == 0:
        return 0.0
    return (bull - bear) / total


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# NEWS FETCHING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_news():
    """Fetch general market news from Finnhub, filter to last N hours."""
    data = _finnhub("/news", {"category": "general"})
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=NEWS_HOURS_BACK)).timestamp()

    items = []
    for n in data:
        dt = n.get("datetime", 0)
        if dt < cutoff:
            continue
        related_raw = n.get("related", "") or ""
        related = [t.strip().upper() for t in related_raw.split(",") if t.strip()]
        items.append({
            "headline": n.get("headline", ""),
            "summary": n.get("summary", ""),
            "source": n.get("source", ""),
            "datetime": dt,
            "url": n.get("url", ""),
            "related": related,
        })
    return items


def find_candidate_tickers(news, universe):
    """Extract unique universe tickers mentioned in news."""
    seen = set()
    out = []
    for item in news:
        for t in item["related"]:
            norm = t.replace(".", "-")
            if norm in universe and norm not in seen:
                seen.add(norm)
                out.append(norm)
        # Also check $TICKER pattern in headline
        for m in re.findall(r"\$([A-Z]{1,5})", item["headline"]):
            if m in universe and m not in seen:
                seen.add(m)
                out.append(m)
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SENTIMENT SCORING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_finnhub_sentiment(ticker):
    """Call Finnhub /news-sentiment. Returns dict or None."""
    try:
        data = _finnhub("/news-sentiment", {"symbol": ticker})
        news_list = []
        for n in data.get("news", []):
            news_list.append({
                "headline": n.get("headline", ""),
                "source": n.get("source", ""),
                "datetime": n.get("datetime", 0),
                "sentiment_label": n.get("sentiment", "Neutral"),
                "sentiment_score": float(n.get("sentimentScore", 0)),
            })
        return {
            "bullish_pct": float(data.get("bullishPercent", 0)),
            "bearish_pct": float(data.get("bearishPercent", 0)),
            "sentiment_score": float(data.get("sentimentScore", 0)),
            "buzz": float(data.get("buzz", 0)),
            "news_count": len(news_list),
            "news": news_list,
            "source": "finnhub",
        }
    except Exception:
        return None


def score_ticker(ticker, news_items):
    """
    Score a ticker: try Finnhub sentiment API first, fall back to keywords.
    Returns scored dict or None.
    """
    # ── Attempt 1: Finnhub sentiment endpoint ──
    fs = fetch_finnhub_sentiment(ticker)
    if fs and fs["news_count"] >= MIN_NEWS_COUNT:
        return {"ticker": ticker, **fs}

    # ── Attempt 2: keyword scoring on news we already have ──
    ticker_news = [n for n in news_items if ticker in n["related"]]
    if not ticker_news:
        for n in news_items:
            if re.search(rf"\b{ticker}\b", n["headline"].upper()) or f"${ticker}" in n["headline"]:
                ticker_news.append(n)
    if not ticker_news:
        return None

    scored = []
    for n in ticker_news:
        text = n["headline"] + " " + n.get("summary", "")
        s = _keyword_score(text)
        scored.append({
            "headline": n["headline"],
            "source": n["source"],
            "datetime": n["datetime"],
            "sentiment_label": "Positive" if s > 0.1 else "Negative" if s < -0.1 else "Neutral",
            "sentiment_score": round(s, 3),
        })

    avg = sum(x["sentiment_score"] for x in scored) / len(scored) if scored else 0
    bull_n = sum(1 for x in scored if x["sentiment_score"] > 0)
    bull_pct = bull_n / len(scored) * 100 if scored else 0

    return {
        "ticker": ticker,
        "bullish_pct": round(bull_pct, 1),
        "bearish_pct": round(100 - bull_pct, 1),
        "sentiment_score": round(avg, 3),
        "buzz": len(scored),
        "news_count": len(scored),
        "news": scored,
        "source": "keyword_fallback",
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# COMPANY PROFILE (name + market cap)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_profile_cache = {}

def fetch_profile(ticker):
    """Fetch company name and market cap from Finnhub."""
    if ticker in _profile_cache:
        return _profile_cache[ticker]
    try:
        data = _finnhub("/stock/profile2", {"symbol": ticker})
        _profile_cache[ticker] = {
            "name": data.get("name", ticker),
            "market_cap": data.get("marketCapitalization", 0),
            "exchange": data.get("exchange", ""),
            "industry": data.get("finnhubIndustry", ""),
        }
    except Exception:
        _profile_cache[ticker] = {"name": ticker, "market_cap": 0, "exchange": "", "industry": ""}
    return _profile_cache[ticker]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PRE-MARKET DATA (Alpaca)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def fetch_premarket(ticker):
    """Fetch today's pre-market bars from Alpaca (4 AM – 9:30 AM ET)."""
    et = _now_et()
    today = et.strftime("%Y-%m-%d")
    offset = _et_offset()

    data = _alpaca(f"/stocks/{ticker}/bars", {
        "start": f"{today}T04:00:00{offset}",
        "end": f"{today}T09:30:00{offset}",
        "timeframe": "5Min",
        "limit": 120,
        "adjustment": "raw",
        "feed": "sip",
        "session": "extended",
    })

    if not data or "bars" not in data or not data["bars"]:
        return None

    bars = data["bars"]
    opens   = [b["o"] for b in bars]
    highs   = [b["h"] for b in bars]
    lows    = [b["l"] for b in bars]
    closes  = [b["c"] for b in bars]
    vols    = [b["v"] for b in bars]

    total_vol = sum(vols)
    first_o   = opens[0]
    last_c    = closes[-1]
    hi        = max(highs)
    lo        = min(lows)

    tp = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
    vwap = sum(t * v for t, v in zip(tp, vols)) / total_vol if total_vol else last_c
    chg = ((last_c - first_o) / first_o * 100) if first_o > 0 else 0

    return {
        "open": first_o, "high": hi, "low": lo, "close": last_c,
        "vwap": round(vwap, 2), "volume": total_vol,
        "change_pct": round(chg, 2), "bars": len(bars),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIDENCE SCORING & TRADE LEVELS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def calc_confidence(sent, pm):
    """Composite confidence 0-100 for ranking."""
    s = max(0, min(100, (sent["sentiment_score"] + 1) / 2 * 100))
    b = sent["bullish_pct"]
    n = min(100, sent["news_count"] * 20)

    if pm:
        pc = min(100, max(0, pm["change_pct"] * 15))     # 3% → 45, 7% → 100
        pv = min(100, pm["volume"] / 100000 * 8)          # 500K → 40, 1.2M → 100
        p = (pc * 0.6 + pv * 0.4)
    else:
        p = 45  # slight penalty for missing pre-market data

    return round(
        (s * W_SENTIMENT + b * W_BULLISH_PCT + n * W_NEWS_COUNT + p * W_PREMKT_CHG)
        / (W_SENTIMENT + W_BULLISH_PCT + W_NEWS_COUNT + W_PREMKT_CHG),
        1,
    )


def calc_levels(pm):
    """Calculate entry, stop, TP1, TP2 from pre-market data."""
    if not pm or pm["close"] <= 0:
        return {"entry": 0, "stop": 0, "tp1": 0, "tp2": 0, "rr": 0}

    entry = pm["close"]
    rng   = pm["high"] - pm["low"]

    if rng > 0:
        stop = pm["low"] - rng * 0.15          # just below PM low
    else:
        stop = entry * 0.985                    # 1.5% default stop

    risk = entry - stop
    if risk <= 0:
        risk = entry * 0.01
        stop = entry - risk

    tp1 = entry + risk * 1.5
    tp2 = entry + risk * 2.5
    rr  = round((tp1 - entry) / risk, 1)

    return {
        "entry": round(entry, 2), "stop": round(stop, 2),
        "tp1": round(tp1, 2), "tp2": round(tp2, 2), "rr": rr,
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN SCAN
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def scan(top_n=DEFAULT_TOP_N, min_bull=MIN_BULLISH_PCT, min_score=MIN_SENTIMENT_SCORE, universe=None):
    """Orchestrate: news → tickers → sentiment → pre-market → rank."""
    print(f"\n  Loading ticker universe...", flush=True)
    universe = set(get_universe_tickers(universe))
    print(f"  {len(universe)} tickers in universe", flush=True)

    # 1. News
    print(f"  Fetching news (last {NEWS_HOURS_BACK}h)...", flush=True)
    news = fetch_news()
    print(f"  {len(news)} articles found", flush=True)
    if not news:
        print("\n  ✗ No recent news. Check your FINNHUB_API_KEY.\n")
        return []

    # 2. Candidate tickers
    candidates = find_candidate_tickers(news, universe)
    print(f"  {len(candidates)} universe tickers mentioned", flush=True)
    if not candidates:
        print("\n  ✗ No universe tickers in today's news.\n")
        return []

    # 3. Score sentiment
    print(f"  Scoring sentiment ({len(candidates)} tickers)...", flush=True)
    scored = []
    for i, t in enumerate(candidates, 1):
        r = score_ticker(t, news)
        if r:
            scored.append(r)
        if i % 10 == 0 or i == len(candidates):
            print(f"    {i}/{len(candidates)}", flush=True)

    bullish = [s for s in scored if s["bullish_pct"] >= min_bull and s["sentiment_score"] >= min_score]
    print(f"  {len(bullish)} pass filter (>={min_bull}% bullish, score>={min_score})", flush=True)
    if not bullish:
        print("\n  ✗ No bullish tickers today.\n")
        return []

    # 4. Company profiles (name + market cap)
    print(f"  Fetching company profiles...", flush=True)
    for s in bullish:
        prof = fetch_profile(s["ticker"])
        s["name"] = prof["name"]
        s["market_cap_bn"] = round(prof["market_cap"] / 1e9, 1) if prof["market_cap"] else 0

    # 5. Pre-market data
    if ALPACA_KEY and ALPACA_SECRET:
        print(f"  Fetching pre-market data...", flush=True)
        for s in bullish:
            pm = fetch_premarket(s["ticker"])
            s["premarket"] = pm
            tag = f"{pm['change_pct']:+.2f}%  vol={pm['volume']:,}" if pm else "no data"
            print(f"    {s['ticker']:6} {tag}", flush=True)
    else:
        print(f"  Skipped pre-market (Alpaca keys not set)", flush=True)
        for s in bullish:
            s["premarket"] = None

    # 6. Build & rank signals
    signals = []
    for s in bullish:
        pm = s["premarket"]
        if pm and MIN_PREMARKET_CHG_PCT > 0 and pm["change_pct"] < MIN_PREMARKET_CHG_PCT:
            continue
        if pm and MIN_PREMARKET_VOL > 0 and pm["volume"] < MIN_PREMARKET_VOL:
            continue

        levels = calc_levels(pm)
        conf   = calc_confidence(s, pm)
        top_hl = sorted(
            s.get("news", []),
            key=lambda x: x.get("sentiment_score", 0) or 0,
            reverse=True,
        )[:3]

        signals.append({
            "ticker": s["ticker"],
            "name": s.get("name", s["ticker"]),
            "market_cap_bn": s.get("market_cap_bn", 0),
            "sentiment_score": s["sentiment_score"],
            "bullish_pct": s["bullish_pct"],
            "news_count": s["news_count"],
            "headlines": [h["headline"] for h in top_hl],
            "sentiment_source": s["source"],
            "confidence": conf,
            "premarket": pm,
            **levels,
        })

    signals.sort(key=lambda x: x["confidence"], reverse=True)
    return signals[:top_n]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# OUTPUT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _fmt_num(n, suffix=""):
    if n >= 1e12:
        return f"${n/1e12:.1f}T{suffix}"
    if n >= 1e9:
        return f"${n/1e9:.1f}B{suffix}"
    if n >= 1e6:
        return f"${n/1e6:.0f}M{suffix}"
    return f"${n:,.0f}{suffix}"


def print_terminal(signals):
    et = _now_et()
    w = 80

    print()
    print(f"  {'═' * w}")
    print(f"  PRE-MARKET SENTIMENT SCAN — {et.strftime('%Y-%m-%d %H:%M')} ET")
    print(f"  {'═' * w}")

    if not signals:
        print(f"\n  No bullish signals found today.\n")
        return

    # ── Summary table ──
    print(f"\n  {'#':<3} {'TICKER':<7} {'SCORE':>6} {'BULL%':>6} {'NEWS':>4} "
          f"{'CONF':>5} {'PM%':>7} {'PM VOL':>10} {'MKT CAP':>9}")
    print(f"  {'─'*3} {'─'*7} {'─'*6} {'─'*6} {'─'*4} {'─'*5} {'─'*7} {'─'*10} {'─'*9}")

    for i, s in enumerate(signals, 1):
        pm = s["premarket"]
        pm_chg = f"{pm['change_pct']:+.2f}%" if pm else "  N/A  "
        pm_vol = f"{pm['volume']:>9,}" if pm else "      N/A"
        cap = f"${s['market_cap_bn']:.1f}B" if s["market_cap_bn"] else "  N/A   "

        print(f"  {i:<3} {s['ticker']:<7} {s['sentiment_score']:>+6.3f} "
              f"{s['bullish_pct']:>5.1f}% {s['news_count']:>4} "
              f"{s['confidence']:>5.1f} {pm_chg:>7} {pm_vol} {cap:>9}")

    # ── Detail cards ──
    print(f"\n  {'═' * w}")
    print(f"  DETAILED SIGNALS")
    print(f"  {'═' * w}")

    for i, s in enumerate(signals, 1):
        pm = s["premarket"]
        src = s["sentiment_source"]

        print(f"\n  ┌─ #{i}  {s['ticker']}  —  {s['name']}")
        print(f"  │     Confidence {s['confidence']}/100  |  [{src}]  |  "
              f"Mkt Cap {s['market_cap_bn']:.1f}B" if s["market_cap_bn"] else
              f"\n  ┌─ #{i}  {s['ticker']}  —  Confidence {s['confidence']}/100  |  [{src}]")
        print(f"  │  Sentiment: {s['sentiment_score']:+.3f}   "
              f"Bullish: {s['bullish_pct']:.1f}%   Articles: {s['news_count']}")

        if pm:
            print(f"  │  Pre-Mkt: ${pm['close']:.2f} ({pm['change_pct']:+.2f}%)  "
                  f"Vol: {pm['volume']:,}  VWAP: ${pm['vwap']:.2f}  "
                  f"Range: ${pm['low']:.2f}-${pm['high']:.2f}")
        else:
            print(f"  │  Pre-Mkt: no data (run after 4:00 AM ET or check Alpaca keys)")

        print(f"  │")
        print(f"  │  WHY BULLISH:")
        for h in s["headlines"]:
            line = h[:72] + "…" if len(h) > 72 else h
            print(f"  │    ▸ {line}")

        if s["entry"] > 0:
            print(f"  │")
            print(f"  │  TRADE  Entry ${s['entry']:.2f}  →  "
                  f"Stop ${s['stop']:.2f}  →  "
                  f"TP1 ${s['tp1']:.2f}  →  TP2 ${s['tp2']:.2f}  "
                  f"(R:R 1:{s['rr']})")
        else:
            print(f"  │")
            print(f"  │  TRADE  No levels yet — need pre-market data for entry/stop")

        print(f"  └{'─' * (w - 4)}")

    # ── Trade summary ──
    tradeable = [s for s in signals if s["entry"] > 0]
    if tradeable:
        print(f"\n  {'─' * w}")
        print(f"  READY TO TRADE")
        print(f"  {'─' * w}")
        print(f"  {'TICKER':<7} {'ENTRY':>8} {'STOP':>8} {'TP1':>8} {'TP2':>8} {'R:R':>5} {'CONF':>5}")
        print(f"  {'─'*7} {'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*5} {'─'*5}")
        for s in tradeable:
            print(f"  {s['ticker']:<7} ${s['entry']:>7.2f} ${s['stop']:>7.2f} "
                  f"${s['tp1']:>7.2f} ${s['tp2']:>7.2f} 1:{s['rr']:<4} {s['confidence']:>5.1f}")

    print(f"\n  {'═' * w}\n")


def build_signal_dicts(signals):
    out = []
    for s in signals:
        e = {
            "ticker": s["ticker"], "name": s["name"],
            "confidence": s["confidence"],
            "sentiment_score": s["sentiment_score"],
            "bullish_pct": s["bullish_pct"],
            "news_count": s["news_count"],
            "headlines": s["headlines"],
            "sentiment_source": s["sentiment_source"],
            "market_cap_bn": s["market_cap_bn"],
            "entry": s["entry"], "stop": s["stop"],
            "tp1": s["tp1"], "tp2": s["tp2"], "rr": s["rr"],
        }
        if s["premarket"]:
            e["premarket"] = {
                "close": s["premarket"]["close"],
                "change_pct": s["premarket"]["change_pct"],
                "volume": s["premarket"]["volume"],
                "vwap": s["premarket"]["vwap"],
                "high": s["premarket"]["high"],
                "low": s["premarket"]["low"],
            }
        out.append(e)
    return out


def print_json(signals):
    print(json.dumps(build_signal_dicts(signals), indent=2))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FLASK INTEGRATION (plugs into momentum_screener.py's server)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def register_sentiment_routes(app, require_api_key_decorator):
    from flask import request, jsonify

    @app.route("/api/sentiment")
    @require_api_key_decorator
    def api_sentiment():
        if not FINNHUB_KEY:
            return jsonify({"error": "FINNHUB_API_KEY not set on server"}), 500

        top       = int(request.args.get("top", DEFAULT_TOP_N))
        min_bull  = float(request.args.get("min_bull", MIN_BULLISH_PCT))
        min_score = float(request.args.get("min_score", MIN_SENTIMENT_SCORE))
        raw       = request.args.get("tickers", "").strip()
        custom    = [t for t in raw.split(",") if t.strip()] if raw else None

        try:
            universe_list = get_universe_tickers(custom)
            signals = scan(top_n=top, min_bull=min_bull, min_score=min_score, universe=universe_list)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        et = _now_et()
        return jsonify({
            "date": et.strftime("%Y-%m-%d"),
            "time": et.strftime("%H:%M"),
            "universe_size": len(universe_list),
            "signals": build_signal_dicts(signals),
        })


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AUTO-TRADE (plugs into your existing paper_trader.py)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def auto_trade(signals, risk_profile="moderate"):
    try:
        from paper_trader import submit_trade, is_configured
    except ImportError:
        print("\n  ✗ paper_trader.py not found — put it in the same directory.\n")
        return

    if not is_configured():
        print("\n  ✗ Alpaca not configured (ALPACA_API_KEY / ALPACA_SECRET_KEY).\n")
        return

    tradeable = [s for s in signals if s["entry"] > 0]
    if not tradeable:
        print("\n  ✗ No tradeable signals (need pre-market data for levels).\n")
        return

    print(f"\n  Submitting {len(tradeable)} orders to Alpaca paper account [{risk_profile}]...\n")

    for s in tradeable:
        signal = {
            "ticker": s["ticker"],
            "price": s["entry"],
            "stop": s["stop"],
            "t1": s["tp1"],
            "t2": s["tp2"],
            "rr": s["rr"],
            "score": int(s["confidence"]),
            "setup": f"sentiment_{s['sentiment_source']}",
            "adx": 0, "rsi": 0, "rvol": 0,
            "macd_bull": True,
            "e9": None, "e20": None, "e50": None,
            "regime": "SENTIMENT",
        }
        try:
            res = submit_trade(signal, risk_profile=risk_profile)
            print(f"  ✓ {s['ticker']:6} {res['qty']:>4} shares @ ${s['entry']:.2f}  "
                  f"stop ${s['stop']:.2f}  TP ${s['tp1']:.2f}  "
                  f"(signal {res['signal_id']})")
        except Exception as e:
            print(f"  ✗ {s['ticker']:6} {e}")

    print()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    p = argparse.ArgumentParser(
        description="Pre-Market Sentiment Screener — find bullish stocks before the open",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python premarket_sentiment.py --scan
  python premarket_sentiment.py --auto-trade
  python premarket_sentiment.py --json --top 3
  python premarket_sentiment.py --scan --min-bull 60 --min-score 0.3

Schedule with cron (8:30 AM ET weekdays):
  30 8 * * 1-5 cd /path/to/project && python premarket_sentiment.py --auto-trade >> /tmp/sentiment.log 2>&1
        """,
    )
    p.add_argument("--scan",       action="store_true", help="Scan and display")
    p.add_argument("--auto-trade", action="store_true", help="Scan and submit to paper trader")
    p.add_argument("--json",       action="store_true", help="JSON output")
    p.add_argument("--top",        type=int, default=DEFAULT_TOP_N, help=f"Max results (default {DEFAULT_TOP_N})")
    p.add_argument("--min-bull",   type=float, default=MIN_BULLISH_PCT, help="Min bullish %%")
    p.add_argument("--min-score",  type=float, default=MIN_SENTIMENT_SCORE, help="Min sentiment score")
    p.add_argument("--risk",       default="moderate", choices=["conservative","moderate","aggressive"])
    p.add_argument("--universe",   default="", help="Comma-separated tickers to scan instead of the default curated list")

    args = p.parse_args()

    if not args.scan and not args.auto_trade and not args.json:
        p.print_help()
        sys.exit(0)

    if not FINNHUB_KEY:
        print("\n  ✗ FINNHUB_API_KEY not set")
        print("  Get a free key: https://finnhub.io/register\n")
        sys.exit(1)

    custom = [t for t in args.universe.split(",") if t.strip()] if args.universe else None
    signals = scan(top_n=args.top, min_bull=args.min_bull, min_score=args.min_score, universe=custom)

    if args.json:
        print_json(signals)
    elif args.auto_trade:
        print_terminal(signals)
        auto_trade(signals, risk_profile=args.risk)
    else:
        print_terminal(signals)


if __name__ == "__main__":
    main()