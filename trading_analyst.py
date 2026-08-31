#!/usr/bin/env python3
"""
TRADING ANALYST — Daily Strategy Engine
=========================================
Implements the trading_analyst prompt specification:
  1. Collect user risk attitude + investment period
  2. Run market data analysis via SerpAPI + Gemini (research_google.py)
  3. Generate trading strategies tailored to user profile
  4. Output buy/sell entry/exit points for the day

NEW: --find flag scans a universe of stocks and picks ONE best opportunity.

Pipeline:
  SerpAPI (news) → Gemini (market analysis) → Gemini (strategy generation)

Setup:
  export SERPAPI_KEY=...        # serpapi.com/manage-api-key
  export GEMINI_API_KEY=...     # aistudio.google.com

Usage:
  python trading_analyst.py                                         # interactive
  python trading_analyst.py --tickers AAPL MSFT NVDA
  python trading_analyst.py --tickers FLEX --risk aggressive --period short-term
  python trading_analyst.py --find momentum --risk aggressive --period short-term
  python trading_analyst.py --find mag7 --risk moderate --period medium-term
  python trading_analyst.py --find penny --risk aggressive --period short-term
  python trading_analyst.py --find volume --risk moderate --period short-term
  python trading_analyst.py --find any --risk aggressive --period short-term
  python trading_analyst.py --list          # show all universes
  python trading_analyst.py --check
"""

import os, sys, json, time, argparse
from datetime import datetime
from pathlib import Path

_here = Path(__file__).resolve().parent
_repo_root = _here.parent
sys.path.insert(0, str(_repo_root))

try:
    from tradepilot import data
except ImportError:
    data = None

# ── Try to import research_google from same directory ─────────────────────────
sys.path.insert(0, str(_here))
try:
    import research_google as rg
except ImportError:
    print("\n  ✗ research_google.py not found in the same directory.")
    print("  Place trading_analyst.py in the same folder as research_google.py\n")
    sys.exit(1)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

def _active_model() -> str:
    return rg.get_active_model()

# ── RISK / PERIOD CONSTANTS ───────────────────────────────────────────────────
RISK_OPTIONS   = ["conservative", "moderate", "aggressive"]
PERIOD_OPTIONS = ["short-term", "medium-term", "long-term"]

RISK_DESCRIPTIONS = {
    "conservative": "Prioritise capital preservation. Lower risk, lower returns. Avoid high volatility.",
    "moderate":     "Balanced approach. Accept moderate drawdowns for reasonable returns.",
    "aggressive":   "Willing to accept high risk and volatility for potentially high returns.",
}
PERIOD_DESCRIPTIONS = {
    "short-term":   "Up to 1 year. Day trades, swing trades, momentum plays.",
    "medium-term":  "1 to 3 years. Swing positions, sector rotations, earnings plays.",
    "long-term":    "3+ years. Value investing, dividend growth, compounding.",
}

# ── STOCK UNIVERSES ───────────────────────────────────────────────────────────
UNIVERSES = {
    "mag7": {
        "label":       "Magnificent 7",
        "tickers":     ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA"],
        "description": "Top 7 mega-cap tech stocks",
    },
    "momentum": {
        "label":       "Momentum Movers",
        "tickers":     ["NVDA", "AMD", "TSLA", "PLTR", "CRWD", "MSTR", "COIN",
                        "SMCI", "ARM", "ANET", "RDDT", "APP"],
        "description": "High-momentum growth and tech stocks",
    },
    "penny": {
        "label":       "Penny / Small Cap",
        "tickers":     ["SOUN", "BBAI", "KULR", "DRUG", "MVIS", "OPEN", "NKLA",
                        "CLOV", "SPCE", "NNDM", "AEYE", "MARA"],
        "description": "Sub-$10 high-volatility small caps",
    },
    "volume": {
        "label":       "High Volume",
        "tickers":     ["SPY", "QQQ", "TSLA", "NVDA", "AAPL", "AMD", "BAC",
                        "F", "PLTR", "SOFI", "NIO", "RIVN"],
        "description": "Stocks with consistently high daily trading volume",
    },
    "value": {
        "label":       "Value / Dividend",
        "tickers":     ["BRK-B", "JPM", "JNJ", "PG", "KO", "VZ", "T",
                        "XOM", "CVX", "WMT", "HD", "MCD"],
        "description": "Undervalued or dividend-paying blue chips",
    },
    "sector": {
        "label":       "Sector Leaders",
        "tickers":     ["XLF", "XLE", "XLV", "XLK", "XLI", "GLD", "SLV",
                        "USO", "TLT", "IWM", "DIA", "VTI"],
        "description": "Sector ETFs and broad market leaders",
    },
    "sgx": {
        "label":       "Singapore SGX",
        "tickers":     ["D05.SI", "U11.SI", "O39.SI", "Z74.SI", "G13.SI",
                        "C6L.SI", "S68.SI", "BS6.SI", "A17U.SI", "C38U.SI"],
        "description": "Top SGX-listed Singapore stocks",
    },
    "any": {
        "label":       "Best of All",
        "tickers":     ["NVDA", "AAPL", "MSFT", "TSLA", "PLTR", "AMD",
                        "META", "AMZN", "CRWD", "COIN", "ARM", "MSTR"],
        "description": "Best opportunity across all categories",
    },
}

FIND_OPTIONS = list(UNIVERSES.keys())

# ── FIND: QUICK SCAN PROMPT ───────────────────────────────────────────────────
def _build_scan_prompt(candidates: list[dict], risk: str, period: str, universe_label: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    return f"""You are a professional momentum trader scanning {universe_label} stocks.
Date: {today}
User Profile: {risk} risk, {period} investment horizon

Here are quick news signals for {len(candidates)} stocks:
{json.dumps(candidates, indent=2)}

Pick exactly ONE stock as the best trade opportunity for this user TODAY.
Consider: conviction score, news sentiment, catalyst quality, decision, and fit for {risk}/{period} profile.

Return ONLY valid JSON:
{{
  "winner": "<TICKER>",
  "why_winner": "<2 sentences: why this is the best opportunity today for {risk}/{period} profile>",
  "runner_up": "<TICKER>",
  "avoid": ["<TICKER>", "<TICKER>"],
  "scan_summary": "<1 sentence overall market read across all scanned stocks>",
  "ranked": [
    {{"ticker": "<T>", "rank": 1, "score": 1-10, "reason": "<short reason>"}},
    {{"ticker": "<T>", "rank": 2, "score": 1-10, "reason": "<short reason>"}},
    {{"ticker": "<T>", "rank": 3, "score": 1-10, "reason": "<short reason>"}},
    {{"ticker": "<T>", "rank": 4, "score": 1-10, "reason": "<short reason>"}},
    {{"ticker": "<T>", "rank": 5, "score": 1-10, "reason": "<short reason>"}}
  ]
}}"""

# ── PHASE 1: MARKET DATA ANALYSIS PROMPT ─────────────────────────────────────
def _build_analysis_prompt(ticker: str, signal: dict) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    return f"""You are a senior equity research analyst.
Ticker: {ticker}
Date: {today}

Raw trade signal data from news research:
{json.dumps(signal, indent=2)}

Produce a thorough market data analysis from this signal. Return ONLY valid JSON:

{{
  "ticker": "{ticker}",
  "analysis_date": "{today}",
  "financial_health": {{
    "summary": "<overall financial health based on available news>",
    "revenue_trend": "growing | declining | stable | unknown",
    "earnings_trend": "beating | missing | in-line | unknown",
    "debt_concern": true or false,
    "notes": "<any specific financial data points from news>"
  }},
  "price_trend": {{
    "direction": "uptrend | downtrend | sideways | unknown",
    "momentum": "strong | moderate | weak | unknown",
    "key_levels": "<support/resistance levels if mentioned in news>",
    "recent_move": "<recent price action context>"
  }},
  "market_sentiment": {{
    "overall": "bullish | bearish | neutral | mixed",
    "institutional": "<analyst/institutional view from news>",
    "retail": "<retail/social sentiment if available>",
    "news_tone": "positive | negative | neutral | mixed"
  }},
  "catalysts": {{
    "positive": ["<catalyst 1>", "<catalyst 2>"],
    "negative": ["<risk 1>", "<risk 2>"],
    "upcoming_events": "<earnings, FDA dates, product launches if mentioned>"
  }},
  "sector_context": "<sector/industry dynamics relevant to this ticker>",
  "insider_institutional": {{
    "insider_activity": "<insider buying/selling>",
    "analyst_consensus": "<analyst ratings and targets>",
    "notable_holdings": "unknown"
  }},
  "valuation": {{
    "appears": "undervalued | fairly valued | overvalued | unknown",
    "basis": "<any P/E, P/S, or valuation commentary from news>"
  }},
  "overall_signal": "strong buy | buy | hold | sell | strong sell | unclear",
  "confidence": 1-10,
  "analysis_summary": "<3-4 sentence synthesis of the overall picture>"
}}"""

# ── PHASE 2: STRATEGY GENERATION PROMPT ──────────────────────────────────────
def _build_strategy_prompt(ticker: str, analysis: dict, risk: str, period: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    return f"""You are a professional trading strategist generating actionable daily strategies.
Ticker: {ticker}
Date: {today}

User Profile:
- Risk Attitude: {risk} — {RISK_DESCRIPTIONS[risk]}
- Investment Period: {period} — {PERIOD_DESCRIPTIONS[period]}

Market Data Analysis:
{json.dumps(analysis, indent=2)}

Generate exactly 1 best trading strategy for {ticker} tailored to this user's profile.
Be specific and actionable with concrete entry/exit levels where possible.

Return ONLY valid JSON — no markdown, no explanation:

{{
  "ticker": "{ticker}",
  "generated_date": "{today}",
  "user_risk": "{risk}",
  "user_period": "{period}",
  "market_outlook": "<1 sentence overall market outlook for this ticker today>",
  "top_recommendation": "<strategy_name>",
  "strategies": [
    {{
      "strategy_name": "<concise name e.g. 'Aggressive Momentum Breakout'>",
      "rank": 1,
      "description_rationale": "<paragraph: core idea + why proposed based on analysis + user profile>",
      "alignment_with_user_profile": {{
        "risk_fit": "<why this fits {risk} risk attitude>",
        "period_fit": "<why this fits {period} timeframe>"
      }},
      "key_market_indicators_to_watch": ["<indicator 1>", "<indicator 2>"],
      "trade_setup": {{
        "direction": "LONG | SHORT | NEUTRAL",
        "timeframe": "<intraday | swing 3-5 days | weeks | months>",
        "position_size_guidance": "<% of portfolio guidance for {risk} profile>"
      }},
      "entry": {{
        "condition": "<specific condition to trigger entry>",
        "price_zone": "<price level or range if determinable, else 'market price'>",
        "timing": "<time of day or event trigger>"
      }},
      "exit": {{
        "take_profit": "<target price or % gain>",
        "stop_loss": "<stop price or % loss>",
        "trailing_stop": "<trailing stop guidance if applicable>",
        "time_exit": "<max hold period before reassessing>"
      }},
      "primary_risks": ["<risk 1>", "<risk 2>"],
      "conviction": 1-10
    }}
  ],
  "daily_action_summary": {{
    "recommended_action": "BUY | SELL | HOLD | WAIT",
    "best_entry_today": "<specific entry guidance for today>",
    "avoid_if": "<condition that would invalidate this strategy today>",
    "key_level_to_watch": "<single most important price level today>"
  }}
}}

Rules:
- Conservative: tight stops 3-5%, small position size
- Moderate: stops 5-8%, moderate sizing
- Aggressive: stops 8-15%, larger sizing, momentum/breakout focus
- Short-term: intraday to 5-day holds, technical triggers
- Medium-term: weeks to months, fundamental + technical
- Long-term: months to years, accumulation focus"""

# ── GEMINI CALL — delegates to research_google's fallback chain ───────────────
def _call_gemini(prompt: str, label: str, verbose: bool) -> dict | None:
    """Wraps rg._gemini using a dummy ticker for the label. Auto-fallback included."""
    if not GEMINI_API_KEY:
        print("  ✗ GEMINI_API_KEY not set")
        return None

    # Use rg._gemini directly — it handles model rotation on 429
    result = rg._gemini(label, prompt, verbose, max_output_tokens=4096)

    if result.get("error"):
        print(f"  [GEMINI] ✗ {result['error']}")
        return None

    # _gemini returns a signal-shaped dict; we want the raw parsed JSON
    # For analysis/strategy calls the full dict IS the result
    return result

# ── FAST SIGNAL — replaces 3-phase pipeline with 1 unified Gemini call ───────
# Technical data (from momentum screener) drives entry/stop/target levels.
# Gemini's job is confirmation + rationale only — not generating numbers.
# News enrichment is optional and cached; failure does not block the signal.

def _build_unified_prompt(ticker: str, technical: dict, news: dict, risk: str, period: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    news_block = (
        json.dumps({k: news.get(k) for k in
                    ["decision", "conviction", "news_sentiment", "headline", "catalyst", "risk"]
                    if news.get(k)}, indent=2)
        if news else "Not available"
    )
    return f"""You are a trading strategist. Technical signals below are pre-computed from live price data — trust them.
Your job: confirm the trade thesis with ONE concise assessment.

Date: {today} | Ticker: {ticker}
User profile: {risk} risk | {period}

TECHNICAL (from momentum screener):
  Setup: {technical.get("setup")} | Score: {technical.get("score")}/100
  Price: ${technical.get("price", 0):.2f} | Chg: {technical.get("change_pct", 0):+.2f}%
  ADX: {technical.get("adx", 0):.0f} | RSI: {technical.get("rsi", 0):.0f} | RVOL: {technical.get("rvol", 0):.1f}x
  MACD: {"BULL" if technical.get("macd_bull") else "BEAR"} | ATR: ${technical.get("atr", 0):.2f}
  EMA9: ${technical.get("e9", "?")} | EMA20: ${technical.get("e20", "?")} | EMA50: ${technical.get("e50", "?")}
  vs 20EMA: {technical.get("pct20", 0):+.1f}% | BB Squeeze: {technical.get("squeeze", False)}
  Entry: ${technical.get("price", 0):.2f} | Stop: ${technical.get("stop", "?")} | T1: ${technical.get("t1", "?")} | T2: ${technical.get("t2", "?")} | R:R 1:{technical.get("rr", "?")}

NEWS SENTIMENT (cached, may be absent):
{news_block}

Return ONLY valid JSON — no markdown:
{{
  "action": "BUY | WAIT | AVOID",
  "conviction": 1-10,
  "entry_condition": "<specific price/volume trigger to enter>",
  "avoid_if": "<condition that invalidates this setup today>",
  "key_level": "<single most important price level to watch>",
  "rationale": "<2 sentences: why this works for {risk}/{period} profile>",
  "news_edge": "<1 sentence: how news supports or conflicts with technicals, or N/A>"
}}"""


def generate_fast_signal(
    ticker: str,
    risk: str = "moderate",
    period: str = "short-term",
    screener_data: dict | None = None,
    regime: str = "UNKNOWN",
    fresh: bool = False,
    verbose: bool = False,
) -> dict:
    """
    Fast hybrid signal: technical-first, news-enriched, single Gemini call.
    Returns a dict ready for paper_trader.submit_trade().

    screener_data: pre-computed result of momentum_screener.analyze().
                   If None, fetched live (slower).
    """
    ticker = ticker.upper().strip()

    # 1. Technical data
    if screener_data:
        technical = screener_data
    else:
        try:
            from momentum_screener import fetch, analyze
            raw = fetch(ticker)
            technical = analyze(raw) if raw else None
        except Exception as e:
            return {"ticker": ticker, "error": f"Technical fetch failed: {e}"}

    if not technical:
        return {"ticker": ticker, "error": "Could not fetch price data"}

    # 2. News enrichment (optional — cached, non-blocking)
    news: dict = {}
    try:
        news = rg.research(
            ticker=ticker,
            max_data_age_days=3,
            use_cache=not fresh,
            verbose=verbose,
        )
        if news.get("error") and not news.get("conviction"):
            news = {}
    except Exception:
        news = {}

    # 3. Single Gemini call
    gemini_out: dict = {}
    if GEMINI_API_KEY:
        prompt = _build_unified_prompt(ticker, technical, news, risk, period)
        gemini_out = _call_gemini(prompt, ticker, verbose) or {}

    # 4. Regime penalty on conviction
    base_conviction = gemini_out.get("conviction", technical["score"] // 10)
    if regime == "RISK_OFF":
        base_conviction = max(1, base_conviction - 2)
    elif regime == "NEUTRAL":
        base_conviction = max(1, base_conviction - 1)

    return {
        "ticker":          ticker,
        "price":           technical["price"],
        "change_pct":      technical["change_pct"],
        "setup":           technical["setup"],
        "score":           technical["score"],
        "adx":             technical["adx"],
        "rsi":             technical["rsi"],
        "rvol":            technical["rvol"],
        "macd_bull":       technical["macd_bull"],
        "atr":             technical["atr"],
        "e9":              technical.get("e9"),
        "e20":             technical.get("e20"),
        "e50":             technical.get("e50"),
        "stop":            technical["stop"],
        "t1":              technical["t1"],
        "t2":              technical["t2"],
        "rr":              technical["rr"],
        "regime":          regime,
        "risk":            risk,
        "period":          period,
        "action":          gemini_out.get("action", "WAIT" if technical["score"] < 55 else "BUY"),
        "conviction":      base_conviction,
        "entry_condition": gemini_out.get("entry_condition", technical.get("when", "")),
        "avoid_if":        gemini_out.get("avoid_if", ""),
        "key_level":       gemini_out.get("key_level", technical["stop"]),
        "rationale":       gemini_out.get("rationale", " · ".join(technical.get("reasons", [])[:2])),
        "news_edge":       gemini_out.get("news_edge", ""),
        "news_decision":   news.get("decision", ""),
        "news_conviction": news.get("conviction", 0),
        "generated":       datetime.now().isoformat(),
    }

# ── FIND: SCAN UNIVERSE AND PICK WINNER ───────────────────────────────────────
def run_find(universe_key: str, risk: str, period: str, verbose: bool = True, fresh: bool = False) -> dict:
    """
    Phase A: Quick signal fetch for all tickers in universe (uses cache)
    Phase B: Gemini scans all signals and picks ONE winner
    Phase C: Full deep analysis + strategy on winner only
    """
    universe = UNIVERSES[universe_key]
    tickers  = universe["tickers"]
    label    = universe["label"]

    print(f"\n{'═'*62}")
    print(f"  FIND MODE — Scanning {label} ({len(tickers)} stocks)")
    print(f"  Profile: {risk} risk  |  {period}")
    print(f"  Gemini: {rg._mq.remaining()} left  SerpAPI: {rg._gq.remaining()} left")
    print(f"{'═'*62}")

    # ── Phase A: Quick signal scan ────────────────────────────────────────
    print(f"\n  [A] Scanning {len(tickers)} tickers for news signals...", flush=True)
    candidates = []
    for i, ticker in enumerate(tickers, 1):
        print(f"  [{i:2}/{len(tickers)}] {ticker:<10}", end=" ", flush=True)
        sig = rg.research(
            ticker=ticker,
            max_data_age_days=7,
            use_cache=not fresh,
            verbose=False,
        )
        cached_note = "[C]" if sig.get("_cached") else "[F]"
        dec  = sig.get("decision", "WAIT")
        conv = sig.get("conviction", 0)
        print(f"{dec:<5} conv={conv}  {cached_note}", flush=True)

        if not (sig.get("error") and conv == 0):
            candidates.append({
                "ticker":         ticker,
                "decision":       dec,
                "conviction":     conv,
                "news_sentiment": sig.get("news_sentiment", "neutral"),
                "headline":       (sig.get("headline") or "")[:100],
                "catalyst":       (sig.get("catalyst") or "")[:100],
                "analyst_call":   sig.get("analyst_call", "None found"),
                "hold_overnight": sig.get("hold_overnight", False),
            })

        if not sig.get("_cached"):
            time.sleep(1)

    if not candidates:
        print("  ✗ No valid signals from scan")
        return {"error": "Scan returned no usable signals"}

    candidates.sort(key=lambda x: x["conviction"], reverse=True)
    print(f"\n  ✓ {len(candidates)} signals — top: "
          f"{candidates[0]['ticker']} conv={candidates[0]['conviction']}", flush=True)

    # ── Phase B: Gemini picks winner ──────────────────────────────────────
    print(f"\n  [B] Gemini selecting best opportunity...", flush=True)
    time.sleep(5)
    scan_result = _call_gemini(
        _build_scan_prompt(candidates, risk, period, label),
        f"Scan — {label}", verbose,
    )

    if not scan_result:
        # Fallback to highest conviction BUY
        buys = [c for c in candidates if c["decision"] == "BUY"]
        fallback = buys[0]["ticker"] if buys else candidates[0]["ticker"]
        scan_result = {
            "winner":       fallback,
            "why_winner":   "Highest conviction BUY (Gemini scan unavailable)",
            "runner_up":    candidates[1]["ticker"] if len(candidates) > 1 else "N/A",
            "avoid":        [],
            "scan_summary": "Gemini scan failed — fallback to conviction ranking",
            "ranked":       [{"ticker": c["ticker"], "rank": i+1,
                              "score": c["conviction"], "reason": c["decision"]}
                             for i, c in enumerate(candidates[:5])],
        }

    winner_ticker = scan_result.get("winner", "").upper().strip()
    # Validate winner is in our universe
    if winner_ticker not in tickers:
        winner_ticker = candidates[0]["ticker"]

    print(f"\n  ✓ WINNER: {winner_ticker}", flush=True)
    print(f"    {scan_result.get('why_winner','')[:80]}", flush=True)

    # ── Phase C: Full analysis on winner ──────────────────────────────────
    print(f"\n  [C] Full analysis on {winner_ticker}...", flush=True)
    full_signal = rg.research(
        ticker=winner_ticker,
        max_data_age_days=7,
        use_cache=not fresh,
        verbose=verbose,
    )

    time.sleep(8)
    analysis = _call_gemini(
        _build_analysis_prompt(winner_ticker, full_signal),
        f"Analysis — {winner_ticker}", verbose,
    )

    if not analysis:
        return {"mode": "find", "universe": universe_key, "label": label,
                "scan": scan_result, "winner": winner_ticker,
                "error": "Deep analysis failed", "candidates": candidates}

    print(f"  ✓ {analysis.get('overall_signal','?')} "
          f"confidence {analysis.get('confidence','?')}/10", flush=True)

    time.sleep(8)
    strategies = _call_gemini(
        _build_strategy_prompt(winner_ticker, analysis, risk, period),
        f"Strategy — {winner_ticker}", verbose,
    )

    if not strategies:
        return {"mode": "find", "universe": universe_key, "label": label,
                "scan": scan_result, "winner": winner_ticker,
                "analysis": analysis, "error": "Strategy generation failed",
                "candidates": candidates}

    return {
        "mode":       "find",
        "universe":   universe_key,
        "label":      label,
        "scan":       scan_result,
        "winner":     winner_ticker,
        "signal":     full_signal,
        "analysis":   analysis,
        "strategies": strategies,
        "candidates": candidates,
    }

# ── MAIN PIPELINE (--tickers flow) ────────────────────────────────────────────
def run_trading_analyst(tickers: list[str], risk: str, period: str,
                        verbose: bool = True, fresh: bool = False) -> dict:
    results = {}

    for ticker in tickers:
        ticker = ticker.upper().strip()
        print(f"\n{'━'*62}")
        print(f"  ANALYSING: {ticker}  |  Risk: {risk}  |  Period: {period}")
        print(f"{'━'*62}")

        print(f"\n  [1/3] Fetching market news for {ticker}...", flush=True)
        signal = rg.research(ticker=ticker, max_data_age_days=7,
                             use_cache=not fresh, verbose=verbose)

        # Retry once on JSON truncation
        if signal.get("error") and "JSON" in signal.get("error", ""):
            print(f"  ↻ Truncation detected — retrying in 10s...", flush=True)
            time.sleep(10)
            signal = rg.research(ticker=ticker, max_data_age_days=7,
                                 use_cache=False, verbose=verbose)

        if signal.get("error") and signal.get("conviction", 0) == 0:
            print(f"  ✗ News fetch failed: {signal['error']}")
            results[ticker] = {"error": signal["error"]}
            continue

        cached_note = " [CACHED]" if signal.get("_cached") else ""
        print(f"  ✓ Signal{cached_note} — "
              f"decision: {signal.get('decision','?')}  "
              f"conviction: {signal.get('conviction','?')}/10", flush=True)

        print(f"\n  [2/3] Market data analysis...", flush=True)
        analysis = _call_gemini(_build_analysis_prompt(ticker, signal),
                                f"Analysis — {ticker}", verbose)

        if not analysis:
            results[ticker] = {"error": "Market analysis failed"}
            continue

        print(f"  ✓ {analysis.get('overall_signal','?')} "
              f"confidence {analysis.get('confidence','?')}/10", flush=True)

        time.sleep(8)

        print(f"\n  [3/3] Generating strategy...", flush=True)
        strategies = _call_gemini(_build_strategy_prompt(ticker, analysis, risk, period),
                                  f"Strategy — {ticker}", verbose)

        if not strategies:
            results[ticker] = {"error": "Strategy generation failed", "analysis": analysis}
            continue

        print(f"  ✓ {strategies.get('top_recommendation','?')}", flush=True)
        results[ticker] = {"signal": signal, "analysis": analysis, "strategies": strategies}

    return results

# ── DISPLAY ───────────────────────────────────────────────────────────────────
def register_analyst_routes(app, require_api_key_decorator):
    """
    Register /api/analyst on the momentum_screener Flask app.
    Call this from momentum_screener.py after register_research_routes().

    Route: GET /api/analyst?ticker=AAPL&risk=aggressive&period=short-term
    """

    @app.route("/api/analyst")
    @require_api_key_decorator
    def api_analyst():
        from flask import request, jsonify, abort
        ticker = request.args.get("ticker", "").upper().strip()
        risk   = request.args.get("risk",   "moderate").strip().lower()
        period = request.args.get("period", "short-term").strip().lower()
        fresh  = request.args.get("fresh",  "false").lower() == "true"

        if not ticker:
            abort(400, "ticker param required")
        if risk not in RISK_OPTIONS:
            abort(400, f"risk must be one of: {RISK_OPTIONS}")
        if period not in PERIOD_OPTIONS:
            abort(400, f"period must be one of: {PERIOD_OPTIONS}")

        # Full 3-phase pipeline
        result = run_trading_analyst(
            tickers=[ticker],
            risk=risk,
            period=period,
            verbose=True,
            fresh=fresh,
        )
        data = result.get(ticker, {"error": "No result returned"})
        # Flatten for easier JS consumption
        return jsonify({
            "ticker":     ticker,
            "risk":       risk,
            "period":     period,
            "model_used": rg.get_active_model(),
            **data,
        })

    @app.route("/api/signal")
    @require_api_key_decorator
    def api_fast_signal():
        """
        Fast hybrid signal endpoint — technical-first, single Gemini call.
        Much faster than /api/analyst (1 Gemini call vs 3).
        Use this for paper trading decision support.

        GET /api/signal?ticker=NVDA&risk=moderate&period=short-term
        """
        from flask import request, jsonify, abort
        ticker = request.args.get("ticker", "").upper().strip()
        risk   = request.args.get("risk",   "moderate").strip().lower()
        period = request.args.get("period", "short-term").strip().lower()
        fresh  = request.args.get("fresh",  "false").lower() == "true"
        regime = request.args.get("regime", "UNKNOWN").strip().upper()

        if not ticker:
            abort(400, "ticker param required")
        if risk not in RISK_OPTIONS:
            abort(400, f"risk must be one of: {RISK_OPTIONS}")
        if period not in PERIOD_OPTIONS:
            abort(400, f"period must be one of: {PERIOD_OPTIONS}")

        result = generate_fast_signal(
            ticker=ticker, risk=risk, period=period,
            regime=regime, fresh=fresh, verbose=False,
        )
        return jsonify(result)

    @app.route("/api/analyst/models", methods=["GET"])
    @require_api_key_decorator
    def api_analyst_models():
        from flask import jsonify
        return jsonify({
            "active": rg.get_active_model(),
            "chain":  rg.MODEL_CHAIN,
            "all":    rg._DEFAULT_MODEL_CHAIN,
        })

    @app.route("/api/analyst/models", methods=["POST"])
    @require_api_key_decorator
    def api_analyst_models_set():
        from flask import request, jsonify
        body  = request.get_json(silent=True) or {}
        model = body.get("model", "").strip()
        if not model:
            return jsonify({"error": "body must include {model: '...'}"}), 400
        rg.set_model(model)
        return jsonify({"active": rg.get_active_model(), "chain": rg.MODEL_CHAIN})

def _print_find_results(data: dict, risk: str, period: str):
    try:
        from colorama import Fore, Style, init; init(autoreset=True)
        G=Fore.GREEN; R=Fore.RED; Y=Fore.YELLOW; C=Fore.CYAN
        B=Style.BRIGHT; DIM=Style.DIM; RS=Style.RESET_ALL
    except ImportError:
        G=R=Y=C=B=DIM=RS=""

    now    = datetime.now().strftime("%Y-%m-%d %H:%M")
    scan   = data.get("scan", {})
    winner = data.get("winner", "?")
    label  = data.get("label", data.get("universe", "?"))

    print(f"\n{'═'*62}")
    print(f"  {B}FIND RESULTS — {label}{RS}  {now}")
    print(f"  Profile: {B}{risk.upper()}{RS} risk  |  {B}{period.upper()}{RS}")
    print(f"{'═'*62}")

    print(f"\n  {B}SCAN SUMMARY{RS}")
    print(f"  {scan.get('scan_summary','—')}")

    ranked = scan.get("ranked", [])
    if ranked:
        print(f"\n  {B}RANKINGS{RS}")
        for r in ranked[:5]:
            t   = r.get("ticker","?")
            sc  = r.get("score", 0)
            rk  = r.get("rank","?")
            why = r.get("reason","")[:55]
            col = G if sc >= 7 else Y if sc >= 4 else R
            win = f"  {B}◄ WINNER{RS}" if t == winner else ""
            print(f"  #{rk} {col}{t:<10}{RS} score {sc}/10  {why}{win}")

    avoid = scan.get("avoid", [])
    if avoid:
        print(f"\n  {R}AVOID TODAY: {', '.join(avoid)}{RS}")
    runner = scan.get("runner_up", "")
    if runner and runner != "N/A":
        print(f"  Runner-up:   {runner}")

    print(f"\n{'─'*62}")
    print(f"  {B}★  BEST OPPORTUNITY TODAY:  {G}{winner}{RS}{B}  ★{RS}")
    print(f"  {scan.get('why_winner','')}")
    print(f"{'─'*62}")

    if "error" in data and "analysis" not in data:
        print(f"\n  {R}✗ {data.get('error','')}{RS}\n")
        return

    # Print full strategy for winner
    _print_results(
        {winner: {"signal": data.get("signal",{}),
                  "analysis": data.get("analysis",{}),
                  "strategies": data.get("strategies",{})}},
        risk, period, skip_header=True,
    )

def _print_results(results: dict, risk: str, period: str, skip_header: bool = False):
    try:
        from colorama import Fore, Style, init; init(autoreset=True)
        G=Fore.GREEN; R=Fore.RED; Y=Fore.YELLOW; C=Fore.CYAN
        B=Style.BRIGHT; DIM=Style.DIM; RS=Style.RESET_ALL
    except ImportError:
        G=R=Y=C=B=DIM=RS=""

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    for ticker, data in results.items():
        if not skip_header:
            print(f"\n{'═'*62}")
            print(f"  {B}TRADING ANALYST — {ticker}{RS}  {now}")
            print(f"  Profile: {B}{risk.upper()}{RS} risk  |  {B}{period.upper()}{RS}")
            print(f"{'═'*62}")

        if "error" in data and "analysis" not in data:
            print(f"\n  {R}✗ {data['error']}{RS}\n")
            continue

        analysis = data.get("analysis", {})
        strats   = data.get("strategies", {})

        sent     = analysis.get("market_sentiment", {}).get("overall", "?").upper()
        sig      = analysis.get("overall_signal", "?").upper()
        conf     = analysis.get("confidence", "?")
        sent_col = G if "BULL" in sent else R if "BEAR" in sent else Y
        sig_col  = G if "BUY" in sig else R if "SELL" in sig else Y

        print(f"\n  {B}MARKET SNAPSHOT — {ticker}{RS}")
        print(f"  Sentiment : {sent_col}{sent}{RS}")
        print(f"  Signal    : {sig_col}{sig}{RS}  (confidence {conf}/10)")
        print(f"  Summary   : {analysis.get('analysis_summary','—')}")

        daily   = strats.get("daily_action_summary", {})
        action  = daily.get("recommended_action", "?")
        act_col = G if action=="BUY" else R if action in ("SELL","AVOID") else Y

        print(f"\n  {B}TODAY'S ACTION{RS}")
        print(f"  ➤  {act_col}{B}{action}{RS}")
        print(f"  Best entry  : {daily.get('best_entry_today','—')}")
        print(f"  Key level   : {daily.get('key_level_to_watch','—')}")
        print(f"  Avoid if    : {daily.get('avoid_if','—')}")
        print(f"  Top strategy: {B}{strats.get('top_recommendation','—')}{RS}")

        print(f"\n  {B}STRATEGY{RS}")
        for s in strats.get("strategies", []):
            name    = s.get("strategy_name", "?")
            conv    = s.get("conviction", "?")
            dirn    = s.get("trade_setup", {}).get("direction", "?")
            tf      = s.get("trade_setup", {}).get("timeframe", "?")
            sizing  = s.get("trade_setup", {}).get("position_size_guidance", "")
            entry_z = s.get("entry", {}).get("price_zone", "market")
            entry_c = s.get("entry", {}).get("condition", "")
            timing  = s.get("entry", {}).get("timing", "")
            tp      = s.get("exit", {}).get("take_profit", "?")
            sl      = s.get("exit", {}).get("stop_loss", "?")
            ts      = s.get("exit", {}).get("trailing_stop", "")
            te      = s.get("exit", {}).get("time_exit", "")
            risks   = s.get("primary_risks", [])
            dir_col = G if dirn=="LONG" else R if dirn=="SHORT" else Y

            print(f"\n  {B}{name}{RS}  [{dir_col}{dirn}{RS}]  conviction {conv}/10")
            print(f"  Timeframe    : {tf}")
            if sizing:
                print(f"  Position size: {sizing}")
            print(f"  Entry zone   : {entry_z}")
            print(f"  Entry trigger: {entry_c[:90]}")
            if timing:
                print(f"  Timing       : {timing}")
            print(f"  Take profit  : {tp}")
            print(f"  Stop loss    : {sl}")
            if ts and ts.lower() not in ("n/a", "none", ""):
                print(f"  Trailing stop: {ts}")
            if te:
                print(f"  Time exit    : {te}")
            for i, rk in enumerate(risks[:2], 1):
                print(f"  Risk {i}        : {rk}")

        print(f"\n  {'─'*58}")
        print(f"  {DIM}DISCLAIMER: For educational/informational purposes only.")
        print(f"  Not financial advice. Do your own research and consult a")
        print(f"  qualified financial advisor before making any decisions.")
        print(f"  Past performance is not indicative of future results.{RS}")
        print(f"  {'─'*58}\n")

# ── INTERACTIVE PROMPTS ───────────────────────────────────────────────────────
def _prompt_risk() -> str:
    print("\n  What is your risk attitude?")
    for i, r in enumerate(RISK_OPTIONS, 1):
        print(f"  {i}. {r.capitalize()} — {RISK_DESCRIPTIONS[r]}")
    while True:
        val = input("\n  Enter 1/2/3 or type: ").strip().lower()
        if val in ("1", "conservative"): return "conservative"
        if val in ("2", "moderate"):     return "moderate"
        if val in ("3", "aggressive"):   return "aggressive"
        print("  Please enter 1, 2, or 3")

def _prompt_period() -> str:
    print("\n  What is your investment timeframe?")
    for i, pp in enumerate(PERIOD_OPTIONS, 1):
        print(f"  {i}. {pp.capitalize()} — {PERIOD_DESCRIPTIONS[pp]}")
    while True:
        val = input("\n  Enter 1/2/3 or type: ").strip().lower()
        if val in ("1", "short-term"):  return "short-term"
        if val in ("2", "medium-term"): return "medium-term"
        if val in ("3", "long-term"):   return "long-term"
        print("  Please enter 1, 2, or 3")

def _prompt_tickers() -> list[str]:
    raw = input("\n  Enter ticker(s) to analyse (e.g. FLEX AAPL D05.SI): ").strip().upper()
    tickers = [t.strip() for t in raw.replace(",", " ").split() if t.strip()]
    if not tickers:
        print("  No tickers entered.")
        sys.exit(0)
    return tickers

def _save_report(data: dict, risk: str, period: str):
    out_dir  = Path(os.environ.get("RESEARCH_CACHE_DIR", "/tmp/rg_cache"))
    out_dir.mkdir(exist_ok=True)
    stamp    = datetime.now().strftime("%Y%m%d_%H%M")
    out_file = out_dir / f"strategies_{stamp}.json"
    try:
        out_file.write_text(json.dumps({
            "generated": datetime.now().isoformat(),
            "risk": risk, "period": period, "results": data,
        }, indent=2))
        print(f"  Report saved → {out_file}\n")
    except Exception:
        pass

# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Daily Trading Analyst — strategies + best stock finder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python trading_analyst.py --find momentum --risk aggressive --period short-term
  python trading_analyst.py --find mag7 --risk moderate --period medium-term
  python trading_analyst.py --find penny --risk aggressive --period short-term
  python trading_analyst.py --find sgx --risk conservative --period long-term
  python trading_analyst.py --find any --risk aggressive --period short-term
  python trading_analyst.py --tickers AAPL NVDA --risk aggressive --period short-term
  python trading_analyst.py --list
        """
    )
    p.add_argument("--tickers", nargs="+",
                   help="Analyse specific tickers e.g. FLEX AAPL D05.SI")
    p.add_argument("--find",    choices=FIND_OPTIONS, metavar="UNIVERSE",
                   help=f"Scan a universe and pick 1 best stock: "
                        f"{', '.join(FIND_OPTIONS)}")
    p.add_argument("--risk",    choices=RISK_OPTIONS,   help="conservative | moderate | aggressive")
    p.add_argument("--period",  choices=PERIOD_OPTIONS, help="short-term | medium-term | long-term")
    p.add_argument("--json",    action="store_true",    help="Raw JSON output")
    p.add_argument("--fresh",   action="store_true",    help="Skip cache, force re-fetch")
    p.add_argument("--quiet",   action="store_true",    help="Less verbose output")
    p.add_argument("--check",   action="store_true",    help="Check API keys + quota")
    p.add_argument("--list",    action="store_true",    help="List available --find universes")
    args = p.parse_args()

    # ── List universes ─────────────────────────────────────────────────────
    if args.list:
        print(f"\n  Available --find universes:\n")
        print(f"  {'KEY':<12} {'LABEL':<22} {'TICKERS'}")
        print(f"  {'─'*12} {'─'*22} {'─'*30}")
        for key, u in UNIVERSES.items():
            preview = ", ".join(u["tickers"][:5]) + ("..." if len(u["tickers"]) > 5 else "")
            print(f"  {key:<12} {u['label']:<22} {preview}")
        print()
        return

    # ── Key check ─────────────────────────────────────────────────────────
    serp = bool(rg.SERPAPI_KEY)
    gem  = bool(GEMINI_API_KEY)

    if args.check:
        print(f"\n  {'✓' if serp else '✗'}  SERPAPI_KEY    {'set' if serp else 'NOT SET — serpapi.com'}")
        print(f"  {'✓' if gem  else '✗'}  GEMINI_API_KEY {'set' if gem  else 'NOT SET — aistudio.google.com'}")
        print(f"\n  SerpAPI searches remaining : {rg._gq.remaining()}/{rg.GOOGLE_DAILY}")
        print(f"  Gemini calls remaining     : {rg._mq.remaining()}/{rg.GEMINI_DAILY}")
        print(f"  Gemini model               : {_active_model()}")
        print(f"  Model chain                : {' → '.join(rg.MODEL_CHAIN)}")
        print(f"\n  Estimated quota cost per --find run:")
        for key, u in UNIVERSES.items():
            n = len(u["tickers"])
            print(f"    --find {key:<12} ~{n} SerpAPI  +  ~{n+3} Gemini calls")
        print()
        return

    if not serp or not gem:
        print("\n  ✗ Missing API keys. Run --check for details.\n")
        sys.exit(1)

    print(f"\n{'═'*62}")
    print(f"  TRADING ANALYST  —  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Powered by SerpAPI + Gemini {_active_model()}")
    print(f"{'═'*62}")

    risk   = args.risk   or _prompt_risk()
    period = args.period or _prompt_period()

    # ── FIND MODE ──────────────────────────────────────────────────────────
    if args.find:
        result = run_find(
            universe_key=args.find,
            risk=risk,
            period=period,
            verbose=not args.quiet,
            fresh=args.fresh,
        )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            _print_find_results(result, risk, period)
            _save_report({"find": result}, risk, period)
        return

    # ── TICKER MODE ────────────────────────────────────────────────────────
    tickers = args.tickers or _prompt_tickers()
    print(f"\n  ✓ Tickers : {', '.join(tickers)}")
    print(f"  ✓ Risk    : {risk}")
    print(f"  ✓ Period  : {period}")
    print(f"  Gemini: {rg._mq.remaining()} left  SerpAPI: {rg._gq.remaining()} left\n")

    results = run_trading_analyst(
        tickers=tickers, risk=risk, period=period,
        verbose=not args.quiet, fresh=args.fresh,
    )

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        _print_results(results, risk, period)
        _save_report(results, risk, period)


if __name__ == "__main__":
    main()