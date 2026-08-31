#!/usr/bin/env python3
"""
MOMENTUM SCREENER
=================
Usage:
  python momentum_screener.py                        # scan default watchlist
  python momentum_screener.py FLEX AMD JBL NVDA      # custom tickers
  python momentum_screener.py --preset ai            # preset watchlist
  python momentum_screener.py --watch                # auto-refresh every 5 min
  python momentum_screener.py --serve                # API server on :5000

Install:
  pip install -r requirements.txt
"""

import sys
import os
import hmac
import time
import argparse
import functools
from datetime import datetime


# ── deps ──────────────────────────────────────────────────────────────────────
try:
    import yfinance as yf
    import pandas as pd
    from tabulate import tabulate
    from colorama import init, Fore, Style
    init(autoreset=True)
except ImportError as e:
    print(f"\n  Missing dependency: {e}")
    print("  pip install -r requirements.txt\n")
    sys.exit(1)

try:
    import ml_model as ml
    ML_AVAILABLE = True
except ImportError as e:
    print(f"  {Fore.YELLOW}⚠ ml_model unavailable ({e}) — ML win-probability disabled{Style.RESET_ALL}")
    ML_AVAILABLE = False

# ── PRESETS ───────────────────────────────────────────────────────────────────
PRESETS = {
    "watchlist": ["FLEX", "AMD", "JBL", "COCO", "MPC"],
    "ai":        ["NVDA", "AMD", "FLEX", "JBL", "PLTR", "SMCI"],
    "momentum":  ["AAPL", "MSFT", "AMZN", "META", "TSLA", "NFLX"],
    "energy":    ["MPC", "XOM", "CVX", "COP", "SLB"],
    "mag7":      ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NVDA"],
    "penny_vol":  ["MARA","RIOT","CLSK","CIFR","HUT","ARBK","BTBT","BITF","HIVE","WULF"],
    "penny_bio":  ["SNDX","IMVT","VERA","PRAX","JANX","DAWN","ITOS","NUVB","KYMR","VKTX"],
    "penny_tech": ["BBAI","AEYE","WRAP","GFAI","UXIN","BTCS","HOLO","MIMO","VERB","IDEX"],
     # ── SINGAPORE (SGX) ──────────────────────────────────────────────────────
    # Blue chips + mid-caps on SGX. Yahoo Finance uses .SI suffix.
    # SGX hours: 9:00 AM – 5:00 PM SGT (UTC+8). Lunch break 12:00–1:00 PM.
    "sgx_core":  ["U96.SI","D05.SI","G92.SI","1F2.SI","BN4.SI"],
    "sgx_broad": ["U96.SI","D05.SI","G92.SI","1F2.SI","BN4.SI",
                  "S68.SI","C6L.SI","Z74.SI","O39.SI","U11.SI"],
}

# ── COLOURS ───────────────────────────────────────────────────────────────────
G  = Fore.GREEN;  R = Fore.RED;  Y = Fore.YELLOW;  C = Fore.CYAN
DM = Style.DIM;   B = Style.BRIGHT;  RS = Style.RESET_ALL

def green(t):  return f"{G}{B}{t}{RS}"
def red(t):    return f"{R}{B}{t}{RS}"
def yellow(t): return f"{Y}{t}{RS}"
def cyan(t):   return f"{C}{t}{RS}"
def dim(t):    return f"{DM}{t}{RS}"
def bold(t):   return f"{B}{t}{RS}"

def log(msg, level="info"):
    ts  = datetime.now().strftime("%H:%M:%S")
    tag = {"ok": green("✓"), "err": red("✗"), "warn": yellow("⚠"), "info": dim("·")}[level]
    print(f"  {dim(ts)}  {tag}  {msg}", flush=True)
# ── MAGIC FIND ────────────────────────────────────────────────────────────────
# Scans a broad universe of US + SGX stocks, scores all of them,
# and returns only the top N by confidence score.
# Run: python momentum_screener.py --magic
# or:  GET /api/magic?market=us&top=5

MAGIC_UNIVERSE = {
    "us": [
        # Large cap momentum leaders
        "NVDA","AMD","MSFT","AAPL","AMZN","META","GOOGL","TSLA","NFLX",
        # AI infrastructure
        "PLTR","SMCI","FLEX","JBL","DELL","HPE","ARM","AVGO",
        # High-beta / high-RVOL
        "MARA","RIOT","CLSK","BITF","HIVE","WULF",
        # Financials / macro
        "JPM","GS","BAC","XOM","CVX","MPC","COP",
        # Growth
        "CRWD","SNOW","MDB","NET","DDOG","ZS","HUBS",
    ],
    "sg": [
        # STI blue chips
        "D05.SI","U11.SI","O39.SI","Z74.SI","C6L.SI","S68.SI","BN4.SI",
        # Mid caps with momentum
        "U96.SI","G92.SI","1F2.SI","S58.SI","V03.SI","F34.SI","T4B.SI",
        # REITs
        "C38U.SI","ME8U.SI","A17U.SI","J69U.SI","K71U.SI",
    ],
    "both": [],  # populated dynamically below
}
MAGIC_UNIVERSE["both"] = MAGIC_UNIVERSE["us"] + MAGIC_UNIVERSE["sg"]

def magic_find(market="both", top=5, min_score=55):
    """
    Scan the full universe for a given market, score everything,
    return only top N results above min_score.
    """
    universe = MAGIC_UNIVERSE.get(market, MAGIC_UNIVERSE["both"])
    log(f"[MAGIC] Scanning {len(universe)} stocks in universe: {market.upper()}", "info")

    raw_data, failed = [], []
    for t in universe:
        d = fetch(t)
        if d:
            raw_data.append(d)
        else:
            failed.append(t)

    if not raw_data:
        log("[MAGIC] No data fetched — check connection", "err")
        return []

    results = [analyze(d) for d in raw_data]
    # Filter by min score and sort
    qualified = [r for r in results if r["score"] >= min_score]
    qualified.sort(key=lambda x: x["score"], reverse=True)
    top_picks = qualified[:top]

    log(f"[MAGIC] Scanned {len(results)} | Qualified (score≥{min_score}): {len(qualified)} | Showing top {len(top_picks)}", "ok")
    for i, r in enumerate(top_picks, 1):
        log(f"[MAGIC] #{i} {r['ticker']:>8}  score {r['score']}/100  {r['setup']}", "ok")

    return top_picks

# ── DAILY BRIEFING ────────────────────────────────────────────────────────────
def daily_picks(market="both", top=5):
    """Regime check + top momentum picks for the day."""
    log("Fetching market regime...", "info")
    rd = regime_data()
    regime = "UNKNOWN"
    if rd:
        vix     = rd["vix"]["price"]
        spy_chg = rd["spy"]["changePct"]
        qqq_chg = rd["qqq"]["changePct"]
        if spy_chg > 0 and qqq_chg > 0 and vix < 20:
            regime = "RISK_ON"
        elif vix < 25:
            regime = "NEUTRAL"
        else:
            regime = "RISK_OFF"
    picks = magic_find(market=market, top=top, min_score=55)
    return {
        "date":        datetime.now().strftime("%Y-%m-%d"),
        "time":        datetime.now().strftime("%H:%M"),
        "regime":      regime,
        "regime_data": rd,
        "market":      market,
        "picks":       picks,
    }

# ── DATA FETCH ────────────────────────────────────────────────────────────────
def fetch(ticker):
    log(f"[{ticker}] Fetching...", "info")
    try:
        raw = yf.download(ticker, period="65d", interval="1d",
                          progress=False, auto_adjust=True)
        if raw.empty or len(raw) < 15:
            log(f"[{ticker}] Only {len(raw)} bars — need 15+", "warn")
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        closes  = raw["Close"].dropna().tolist()
        volumes = raw["Volume"].dropna().tolist()
        highs   = raw["High"].dropna().tolist()
        lows    = raw["Low"].dropna().tolist()
        price   = closes[-1]
        prev    = closes[-2]
        chg_pct = (price - prev) / prev * 100
        try:
            name = yf.Ticker(ticker).info.get("longName", ticker)
        except Exception:
            name = ticker
        log(f"[{ticker}] OK  ${price:.2f}  {chg_pct:+.2f}%  {len(closes)} bars  vol {volumes[-1]/1e6:.1f}M", "ok")
        return dict(ticker=ticker, name=name, price=price,
                    change_pct=chg_pct, closes=closes,
                    volumes=volumes, highs=highs, lows=lows)
    except Exception as e:
        log(f"[{ticker}] Failed — {e}", "err")
        return None

# ── INDICATORS ────────────────────────────────────────────────────────────────
def ema(data, period):
    k = 2 / (period + 1); e = data[0]
    for v in data[1:]: e = v * k + e * (1 - k)
    return e

def rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains = losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i - 1]
        if d > 0: gains += d
        else:     losses -= d
    return 100 - 100 / (1 + gains / (losses or 1e-9))

def macd(closes):
    m = ema(closes, 12) - ema(closes, 26); return m, m > 0

def adx(highs, lows, closes, period=14):
    if len(closes) < period + 2: return 20.0
    n = min(len(closes) - 1, period); sp = sm = st = 0.0
    for i in range(len(closes) - n, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        dp = max(highs[i]-highs[i-1], 0); dm = max(lows[i-1]-lows[i], 0)
        st += tr; sp += dp if dp > dm else 0; sm += dm if dm > dp else 0
    if st == 0: return 0.0        # ← guard for illiquid/flat stocks

    pip = (sp/st)*100; mim = (sm/st)*100
    return abs(pip-mim)/(pip+mim or 1)*100

def rvol(volumes):
    if len(volumes) < 21: return 1.0
    avg = sum(volumes[-21:-1]) / 20
    return volumes[-1] / (avg or 1)

def atr(highs, lows, closes, period=14):
    n = min(len(closes)-1, period); total = 0.0
    for i in range(len(closes)-n, len(closes)):
        total += max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
    return total / n

def bb_squeeze(closes, period=20):
    if len(closes) < period: return False
    sl = closes[-period:]; avg = sum(sl)/period
    std = (sum((v-avg)**2 for v in sl)/period)**0.5
    return (2*std)/avg < 0.05

# ── SIGNAL ENGINE ─────────────────────────────────────────────────────────────
def analyze(data):
    closes = data["closes"]; volumes = data["volumes"]
    highs  = data["highs"];  lows    = data["lows"]
    price  = data["price"];  chg_pct = data["change_pct"]

    e9=ema(closes,9); e20=ema(closes,20); e50=ema(closes,50)
    rsi_v=rsi(closes); macd_v,macd_bull=macd(closes)
    adx_v=adx(highs,lows,closes); rvol_v=rvol(volumes)
    atr_v=atr(highs,lows,closes); squeeze=bb_squeeze(closes)
    pct20=(price-e20)/e20*100; extended=pct20>8
    rsi_bull=55<=rsi_v<=70; trend_ok=adx_v>25

    score = 30
    if trend_ok:                       score += 15
    if adx_v > 35:                     score += 5
    if rvol_v > 1.5:                   score += 15
    if rvol_v > 2.0:                   score += 5
    if macd_bull:                      score += 10
    if price > e9 > 0 and price > e20: score += 10
    if price > e50:                    score += 5
    if rsi_bull:                       score += 10
    elif rsi_v >= 45:                  score += 5
    if squeeze:                        score += 10
    if extended:                       score -= 15
    if rsi_v > 80:                     score -= 10
    score = max(10, min(95, score))

    setup = "NEUTRAL"
    if extended and rsi_v > 70:                   setup = "EXTENDED"
    elif squeeze and adx_v < 25:                  setup = "BB SQUEEZE"
    elif trend_ok and macd_bull and price > e20:  setup = "MOMENTUM CONT."
    elif chg_pct > 5 and rvol_v > 1.5:           setup = "BREAKOUT"
    elif chg_pct < -3 and price > e50:            setup = "PULLBACK ENTRY"
    elif macd_bull and price > e20:               setup = "TREND FOLLOWING"

    stop = round(price - 1.5*atr_v, 2)
    t1   = round(price + 2.0*atr_v, 2)
    t2   = round(price + 3.5*atr_v, 2)
    rr   = round((t1-price)/max(price-stop, 0.01), 1)

    reasons, risks = [], []
    if adx_v > 35:     reasons.append(f"ADX {adx_v:.0f} — very strong trend")
    elif trend_ok:     reasons.append(f"ADX {adx_v:.0f} — trend confirmed")
    else:              risks.append(f"ADX {adx_v:.0f} — weak trend")
    if rvol_v > 2:     reasons.append(f"RVOL {rvol_v:.1f}x — institutional surge")
    elif rvol_v > 1.5: reasons.append(f"RVOL {rvol_v:.1f}x — vol confirms move")
    else:              risks.append(f"RVOL {rvol_v:.1f}x — low volume")
    if rsi_bull:       reasons.append(f"RSI {rsi_v:.0f} — ideal bull zone")
    elif rsi_v > 70:   risks.append(f"RSI {rsi_v:.0f} — overbought")
    elif rsi_v < 45:   risks.append(f"RSI {rsi_v:.0f} — bearish momentum")
    if macd_bull:      reasons.append("MACD positive")
    else:              risks.append("MACD negative")
    if 0 < pct20 <= 5: reasons.append(f"{pct20:.1f}% above 20 EMA — clean structure")
    elif pct20 > 8:    risks.append(f"{pct20:.1f}% above 20 EMA — extended")
    if squeeze:        reasons.append("BB squeeze — volatility compression")

    when = ("Enter 9:30-10:00 AM ET open OR Power Hour 3-4 PM ET on volume spike"
            if score >= 65 else
            "Wait for RVOL > 1.5x before entry. Skip 11:30 AM-2 PM midday chop"
            if score >= 45 else
            "Low confidence — observe only, skip or wait for stronger setup")

    ml_win_prob = None
    if ML_AVAILABLE:
        try:
            ml_win_prob = ml.predict_proba({
                "adx": adx_v, "rsi": rsi_v, "rvol": rvol_v,
                "macd_bull": 1.0 if macd_bull else 0.0,
                "squeeze":   1.0 if squeeze else 0.0,
                "pct20":     pct20,
                "above_e9":  1.0 if price > e9  else 0.0,
                "above_e20": 1.0 if price > e20 else 0.0,
                "above_e50": 1.0 if price > e50 else 0.0,
            })
        except Exception:
            ml_win_prob = None   # model unavailable/untrained — not a hard error

    log(f"[{data['ticker']}] Score {score}/100 | {setup} | ADX {adx_v:.0f} | "
        f"RSI {rsi_v:.0f} | RVOL {rvol_v:.1f}x | MACD {'BULL' if macd_bull else 'BEAR'}",
        "ok" if score >= 65 else "warn" if score >= 45 else "err")

    return dict(
        ticker=data["ticker"], name=data["name"],
        price=price, changePct=chg_pct,           # ← camelCase for JS frontend
        change_pct=chg_pct,                        # ← keep snake_case alias too
        setup=setup, score=score,
        rsi=rsi_v, adx=adx_v, rvol=rvol_v,
        macd=macd_v, macd_bull=macd_bull,
        atr=atr_v, pct20=pct20, squeeze=squeeze,
        e9=round(e9, 2), e20=round(e20, 2), e50=round(e50, 2),
        stop=str(stop), t1=str(t1), t2=str(t2), rr=rr,
        reasons=reasons, risks=risks, when=when,
        ml_win_prob=ml_win_prob,
    )

# ── REGIME ────────────────────────────────────────────────────────────────────
def regime_data():
    """Returns SPY/QQQ/VIX data with camelCase keys for the JS frontend."""
    results = {}
    for sym in ["SPY", "QQQ", "^VIX"]:
        d = fetch(sym)
        if d: results[sym] = d
    if len(results) < 3: return None
    # FIX: use camelCase changePct so JS frontend (d.spy.changePct) works correctly
    return {
        "spy": {"price": results["SPY"]["price"],  "changePct": results["SPY"]["change_pct"]},
        "qqq": {"price": results["QQQ"]["price"],  "changePct": results["QQQ"]["change_pct"]},
        "vix": {"price": results["^VIX"]["price"], "changePct": results["^VIX"]["change_pct"]},
    }

def market_regime():
    """CLI-only: pretty-print current market regime."""
    log("Fetching market regime (SPY, QQQ, VIX)...", "info")
    rd = regime_data()
    if not rd: return "UNKNOWN"
    vix = rd["vix"]["price"]
    spy_chg = rd["spy"]["changePct"]   # FIX: was rd["spy"]["change_pct"] — KeyError
    qqq_chg = rd["qqq"]["changePct"]
    if spy_chg > 0 and qqq_chg > 0 and vix < 20:
        return green("RISK ON") + f"  — Long setups preferred. VIX {vix:.1f}"
    elif vix < 25:
        return yellow("NEUTRAL") + f" — Selective only. VIX {vix:.1f}"
    else:
        return red("RISK OFF") + f" — Avoid new longs. VIX {vix:.1f}"

# ── TERMINAL DISPLAY ──────────────────────────────────────────────────────────
def conf_bar(score, width=20):
    filled = int(score/100*width)
    bar = "█"*filled + "░"*(width-filled)
    return green(bar) if score>=65 else yellow(bar) if score>=45 else red(bar)

def print_card(a, rank):
    chg_str  = f"{a['change_pct']:+.2f}%"
    chg_col  = green(chg_str) if a["change_pct"] >= 0 else red(chg_str)
    rank_line = bold(f"#{rank}  {a['ticker']}") + "  " + dim(a["name"])
    print()
    print(f"  {'─'*62}")
    print(f"  {rank_line}")
    print(f"  {cyan(a['setup'])}    ${a['price']:.2f}  {chg_col} today")
    print(f"  {'─'*62}")
    adx_c  = green(f"{a['adx']:.0f}")    if a["adx"]  > 25  else yellow(f"{a['adx']:.0f}")
    rsi_c  = (red if a["rsi"]>70 else green if a["rsi"]>=55 else dim)(f"{a['rsi']:.0f}")
    rvol_c = green(f"{a['rvol']:.1f}x")  if a["rvol"] > 1.5 else dim(f"{a['rvol']:.1f}x")
    macd_c = green(f"▲{a['macd']:.1f}")  if a["macd_bull"]  else red(f"▼{abs(a['macd']):.1f}")
    pct_c  = green(f"+{a['pct20']:.1f}%") if a["pct20"]>= 0 else red(f"{a['pct20']:.1f}%")
    sqz    = yellow("SQUEEZE") if a["squeeze"] else dim("no squeeze")
    print(f"  ADX {adx_c}  RSI {rsi_c}  RVOL {rvol_c}  "
          f"MACD {macd_c}  vs20EMA {pct_c}  ATR ${a['atr']:.2f}  {sqz}")
    entry_s=dim(f"${a['price']:.2f}"); stop_s=red(f"${a['stop']}"); 
    t1_s=green(f"${a['t1']}"); t2_s=green(f"${a['t2']}"); rr_s=cyan(f"1:{a['rr']}")
    print(f"\n  {'ENTRY':>10}  {'STOP':>10}  {'TARGET 1':>10}  {'TARGET 2':>10}  {'R:R':>6}")
    print(f"  {entry_s:>10}  {stop_s:>10}  {t1_s:>10}  {t2_s:>10}  {rr_s:>6}")
    print(f"\n  CONFIDENCE  {conf_bar(a['score'])}  {bold(str(a['score']))}/100")
    if a.get("ml_win_prob") is not None:
        p = a["ml_win_prob"]
        p_str = f"{p:.0%}"
        p_col = green(p_str) if p >= 0.55 else red(p_str) if p < 0.45 else yellow(p_str)
        print(f"  ML WIN PROB {p_col}  {dim('(learned from history, independent of the score above)')}")
    if a["reasons"]: print(f"\n  {green('WHY:')}   {dim('  ·  '.join(a['reasons']))}")
    if a["risks"]:   print(f"  {red('RISKS:')}  {dim('  ·  '.join(a['risks']))}")
    print(f"  {yellow('WHEN:')}   {a['when']}")

def print_summary_table(results):
    rows = []
    for i,a in enumerate(results):
        ml_p = a.get("ml_win_prob")
        rows.append([f"#{i+1}  {a['ticker']}", a["setup"],
                     f"${a['price']:.2f}", f"{a['change_pct']:+.2f}%",
                     f"{a['adx']:.0f}", f"{a['rsi']:.0f}", f"{a['rvol']:.1f}x",
                     "BULL" if a["macd_bull"] else "BEAR",
                     f"${a['stop']}", f"${a['t1']}", f"{a['score']}/100",
                     f"{ml_p:.0%}" if ml_p is not None else "-"])
    headers=["TICKER","SETUP","PRICE","CHG","ADX","RSI","RVOL","MACD","STOP","T1","SCORE","ML%"]
    print("\n" + tabulate(rows, headers=headers, tablefmt="simple"))

def print_timing_guide():
    print(f"""
  ┌──────────────────────────────────────────────────────────────┐
  │  {green('9:30-10:30 AM ET')}   BEST WINDOW — highest volume, enter breakouts  │
  │  {yellow('11:30 AM-2 PM ET')}  MIDDAY CHOP — avoid new entries, monitor only  │
  │  {cyan('3:00-4:00 PM ET ')}   POWER HOUR  — trail stops or exit positions   │
  └──────────────────────────────────────────────────────────────┘""")

def print_daily_briefing(briefing):
    regime = briefing.get("regime", "UNKNOWN")
    col    = green if regime == "RISK_ON" else (yellow if regime == "NEUTRAL" else red)
    now    = f"{briefing.get('date','')}  {briefing.get('time','')}"
    print(f"\n{'═'*64}")
    print(f"  {bold('DAILY PICKS BRIEFING')}  {dim(now)}")
    print(f"{'═'*64}")
    print_timing_guide()
    print(f"\n  MARKET REGIME:  {col(regime)}")
    rd = briefing.get("regime_data") or {}
    if rd:
        spy_s = green(f"{rd['spy']['changePct']:+.2f}%") if rd['spy']['changePct'] >= 0 else red(f"{rd['spy']['changePct']:+.2f}%")
        qqq_s = green(f"{rd['qqq']['changePct']:+.2f}%") if rd['qqq']['changePct'] >= 0 else red(f"{rd['qqq']['changePct']:+.2f}%")
        vix_s = red(f"{rd['vix']['price']:.1f}") if rd['vix']['price'] > 25 else yellow(f"{rd['vix']['price']:.1f}")
        print(f"  SPY {spy_s}   QQQ {qqq_s}   VIX {vix_s}")
    picks = briefing.get("picks", [])
    mkt   = briefing.get("market", "").upper()
    print(f"\n  TOP {len(picks)} PICKS — {mkt} UNIVERSE")
    print(f"  {'─'*62}")
    for i, p in enumerate(picks, 1):
        chg_s  = green(f"{p['change_pct']:+.2f}%") if p["change_pct"] >= 0 else red(f"{p['change_pct']:+.2f}%")
        macd_s = green("▲BULL") if p["macd_bull"] else red("▼BEAR")
        sqz_s  = yellow(" BB-SQUEEZE") if p.get("squeeze") else ""
        print()
        print(f"  #{i}  {bold(p['ticker'])}  {dim(p['name'])}")
        print(f"      {cyan(p['setup'])}{sqz_s}   Score {bold(str(p['score']))}/100")
        print(f"      Price   : ${p['price']:.2f}  {chg_s}  ATR ${p['atr']:.2f}")
        print(f"      Chart   : EMA9 ${p.get('e9','—')}  EMA20 ${p.get('e20','—')}  EMA50 ${p.get('e50','—')}")
        print(f"      Trade   : Entry~${p['price']:.2f}  Stop ${p['stop']}  T1 ${p['t1']}  T2 ${p['t2']}  R:R 1:{p['rr']}")
        print(f"      Signals : ADX {p['adx']:.0f}  RSI {p['rsi']:.0f}  RVOL {p['rvol']:.1f}x  MACD {macd_s}")
        if p.get("reasons"):
            print(f"      Why     : {dim('  ·  '.join(p['reasons'][:2]))}")
        print(f"      When    : {p['when']}")
    print(f"\n  {'═'*62}")
    print(f"  {dim('Not financial advice. Use stop-losses. Manage risk.')}")
    print(f"  {'═'*62}\n")

# ── TERMINAL SCAN LOOP ────────────────────────────────────────────────────────
def run(tickers, watch=False):
    while True:
        now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        print(f"\n{'═'*64}")
        print(f"  {bold('MOMENTUM / SCAN')}  {dim(now)}")
        print(f"{'═'*64}")
        print_timing_guide()
        print(f"\n  MARKET REGIME:  {market_regime()}\n")
        print(f"  {'─'*62}")
        print(f"  Scanning: {', '.join(tickers)}")
        print(f"  {'─'*62}")
        raw_data = [d for d in (fetch(t) for t in tickers) if d]
        if not raw_data:
            print(red("\n  All fetches failed.\n"))
            if not watch: break
            time.sleep(300); continue
        results = sorted([analyze(d) for d in raw_data], key=lambda x: x["score"], reverse=True)
        failed  = len(tickers) - len(raw_data)
        print(f"\n  {green(str(len(results)))} analyzed  {red(str(failed)) if failed else dim('0')} failed\n")
        print_summary_table(results)
        for i,a in enumerate(results,1): print_card(a,i)
        print(f"\n  {'═'*62}")
        print(f"  {dim('Not financial advice. Use stop-losses. Manage risk.')}")
        print(f"  {'═'*62}\n")
        if not watch: break
        print(f"\n  {dim('Next scan in 5 minutes...')}\n")
        time.sleep(300)

# ── FLASK API SERVER ──────────────────────────────────────────────────────────
def serve(port=5000):
    try:
        from flask import Flask, jsonify, request, abort, Response
        from flask_cors import CORS
    except ImportError:
        print(red("\n  Run:  pip install flask flask-cors\n"))
        sys.exit(1)

    # ── API key auth ──────────────────────────────────────────────────────────
    API_KEY = os.environ.get("API_KEY", "")
    if not API_KEY:
        print(yellow("\n  ⚠  WARNING: API_KEY env var not set. Server is UNPROTECTED."))
        print(   "     Generate one:  python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
        print(   "     Then:          export API_KEY=your_key_here\n")

    raw_origins = os.environ.get("ALLOWED_ORIGINS", "*")
    origins = [o.strip() for o in raw_origins.split(",")] if raw_origins != "*" else "*"

    app = Flask(__name__)
    CORS(app, origins=origins)

    # FIX: moved hmac import to module top level; decorator is clean now
    def require_api_key(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            if not API_KEY:
                return f(*args, **kwargs)
            auth  = request.headers.get("Authorization", "")
            token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
            if not token:
                token = request.args.get("api_key", "")
            if not hmac.compare_digest(token, API_KEY):
                return jsonify({"error": "Unauthorized", "message": "Invalid or missing API key"}), 401
            return f(*args, **kwargs)
        return decorated

    # ── routes ────────────────────────────────────────────────────────────────
    @app.route("/")
    def index():
        # Serve index.html with api-key and api-base injected as <meta> tags.
        # The browser receives the key only over localhost — never in the URL or JS source.
        from flask import Response
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        if not os.path.exists(html_path):
            return jsonify({"error": "index.html not found next to momentum_screener.py"}), 404
        with open(html_path, "r", encoding="utf-8") as fh:
            html = fh.read()
        # Detect the public-facing base URL so the injected api-base meta tag
        # works both on localhost and on deployed hosts (Render, Fly, etc.).
        # request.host includes the port when non-standard (e.g. localhost:5000)
        # but not on standard ports (80/443), so this is safe in all cases.
        scheme   = request.headers.get("X-Forwarded-Proto", request.scheme)
        host     = request.headers.get("X-Forwarded-Host",  request.host)
        api_base = f"{scheme}://{host}/api"
        meta_tags = (
            f'<meta name="api-base" content="{api_base}">\n'
            f'<meta name="api-key"  content="{API_KEY}">\n'
        )
        html = html.replace("<head>", f"<head>\n{meta_tags}", 1)
        return Response(html, mimetype="text/html")

    @app.route("/health")
    def health():
        from datetime import timezone
        return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})

    @app.route("/api/quote")
    @require_api_key
    def api_quote():
        ticker = request.args.get("ticker", "").upper().strip()
        if not ticker:
            return jsonify({"error": "ticker param required"}), 400
        data = fetch(ticker)
        if not data:
            return jsonify({"error": f"Could not fetch {ticker}"}), 500
        result = analyze(data)
        return jsonify(result)

    @app.route("/api/regime")
    @require_api_key
    def api_regime():
        rd = regime_data()
        if not rd:
            return jsonify({"error": "regime fetch failed"}), 500
        return jsonify(rd)

    @app.route("/api/scan")
    @require_api_key
    def api_scan():
        preset = request.args.get("preset", "").strip()
        raw    = request.args.get("tickers", "").strip()
        if preset and preset in PRESETS:
            tickers = PRESETS[preset]
        elif raw:
            tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
        else:
            return jsonify({"error": "pass ?tickers=FLEX,AMD or ?preset=watchlist"}), 400
        results = []
        for t in tickers:
            d = fetch(t)
            if d: results.append(analyze(d))
        results.sort(key=lambda x: x["score"], reverse=True)
        return jsonify(results)
    @app.route("/api/magic")
    @require_api_key
    def api_magic():
        market    = request.args.get("market", "both").strip()
        top       = int(request.args.get("top", 5))
        min_score = int(request.args.get("min_score", 55))
        if market not in MAGIC_UNIVERSE:
            abort(400, f"market must be one of: {list(MAGIC_UNIVERSE.keys())}")
        results = magic_find(market=market, top=top, min_score=min_score)
        return jsonify(results)

    @app.route("/api/daily")
    @require_api_key
    def api_daily():
        market = request.args.get("market", "both").strip()
        top    = int(request.args.get("top", 5))
        if market not in MAGIC_UNIVERSE:
            abort(400, f"market must be one of: {list(MAGIC_UNIVERSE.keys())}")
        result = daily_picks(market=market, top=top)
        return jsonify(result)

    research_routes = False
    analyst_routes  = False

    try:
        import research_google
        from research_google import register_research_routes
        register_research_routes(app, require_api_key)
        research_routes = True
    except Exception as e:
        print(yellow(f"  ⚠ Research route disabled: {e}"))

    try:
        import trading_analyst
        from trading_analyst import register_analyst_routes
        register_analyst_routes(app, require_api_key)
        analyst_routes = True
    except Exception as e:
        print(yellow(f"  ⚠ Analyst route disabled: {e}"))
    paper_routes = False
    try:
        import paper_trader
        from paper_trader import register_paper_routes
        register_paper_routes(app, require_api_key)
        paper_routes = True
    except Exception as e:
        print(yellow(f"  ⚠ Paper trading disabled: {e}"))

    sentiment_routes = False
    try:
        import premarket_sentiment
        from premarket_sentiment import register_sentiment_routes
        register_sentiment_routes(app, require_api_key)
        sentiment_routes = True
    except Exception as e:
        print(yellow(f"  ⚠ Sentiment route disabled: {e}"))

    if not sentiment_routes:
        @app.route("/api/sentiment")
        @require_api_key
        def api_sentiment_stub():
            return jsonify({"error": "Sentiment API unavailable. Ensure premarket_sentiment.py is present and importable, and FINNHUB_API_KEY is set."}), 500

    report_routes = False
    try:
        import data_analyst
        from data_analyst import register_data_analyst_routes
        register_data_analyst_routes(app, require_api_key)
        report_routes = True
    except Exception as e:
        print(yellow(f"  ⚠ Report route disabled: {e}"))
 
    if not report_routes:
        @app.route("/api/report")
        @require_api_key
        def api_report_stub():
            return jsonify({"error": "Report API unavailable. Ensure data_analyst.py is present."}), 500

    if not research_routes:
        @app.route("/api/research")
        @require_api_key
        def api_research_stub():
            return jsonify({"error": "Research API unavailable. Ensure research_google.py is present and importable."}), 500

        @app.route("/api/research/quota")
        @require_api_key
        def api_research_quota_stub():
            return jsonify({"error": "Research API unavailable."}), 500

    if not analyst_routes:
        @app.route("/api/analyst")
        @require_api_key
        def api_analyst_stub():
            return jsonify({"error": "Analyst API unavailable. Ensure trading_analyst.py is present and importable."}), 500

        @app.route("/api/analyst/models", methods=["GET", "POST"])
        @require_api_key
        def api_analyst_models_stub():
            return jsonify({"error": "Analyst API unavailable."}), 500

    # ── startup banner ────────────────────────────────────────────────────────
    print(f"\n{'═'*64}")
    print(f"  {bold('MOMENTUM SCREENER — API SERVER')}")
    print(f"{'═'*64}")
    print(f"\n  {green('Auth:')}    {'API key required (Bearer token)' if API_KEY else red('UNPROTECTED — set API_KEY env var')}")
    print(f"  {green('Origins:')} {origins}")
    print(f"\n  {green('Routes:')}")
    print(f"  {cyan('GET')}  http://localhost:{port}/health                        (public)")
    print(f"  {cyan('GET')}  http://localhost:{port}/api/quote?ticker=FLEX         (auth)")
    print(f"  {cyan('GET')}  http://localhost:{port}/api/regime                    (auth)")
    print(f"  {cyan('GET')}  http://localhost:{port}/api/scan?preset=watchlist     (auth)")
    print(f"  {cyan('GET')}  http://0.0.0.0:{port}/api/magic?market=both&top=5   (auth)")
    print(f"  {cyan('GET')}  http://0.0.0.0:{port}/api/magic?market=us&top=5     (auth)")
    print(f"  {cyan('GET')}  http://0.0.0.0:{port}/api/magic?market=sg&top=5     (auth)")
    print(f"  {cyan('GET')}  http://0.0.0.0:{port}/api/daily?market=both&top=5   (auth)")
    if research_routes:
        print(f"  {cyan('GET')}  http://0.0.0.0:{port}/api/research?ticker=FLEX       (auth)")
        print(f"  {cyan('GET')}  http://0.0.0.0:{port}/api/research/quota           (auth)")
    if analyst_routes:
        print(f"  {cyan('GET')}  http://0.0.0.0:{port}/api/analyst?ticker=AAPL&risk=moderate&period=short-term   (auth)")
        print(f"  {cyan('GET')}  http://0.0.0.0:{port}/api/signal?ticker=AAPL&risk=moderate   (auth)  [FAST]")
        print(f"  {cyan('GET')}  http://0.0.0.0:{port}/api/analyst/models                 (auth)")
    if paper_routes:
        print(f"  {cyan('GET')}  http://0.0.0.0:{port}/api/paper/status                  (auth)")
        print(f"  {cyan('GET')}  http://0.0.0.0:{port}/api/paper/positions               (auth)")
        print(f"  {cyan('PST')}  http://0.0.0.0:{port}/api/paper/trade                   (auth)")
        print(f"  {cyan('GET')}  http://0.0.0.0:{port}/api/paper/performance              (auth)")
    if sentiment_routes:
        print(f"  {cyan('GET')}  http://0.0.0.0:{port}/api/sentiment?top=5                (auth)")
    print(f"\n  {dim('Press Ctrl+C to stop.')}\n")

    app.run(host="0.0.0.0", port=port, debug=False)

# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Momentum Screener")
    parser.add_argument("tickers", nargs="*")
    parser.add_argument("--preset", choices=list(PRESETS.keys()), default="watchlist")
    parser.add_argument("--watch",  action="store_true")
    parser.add_argument("--serve",  action="store_true")
    parser.add_argument("--port",   type=int, default=int(os.environ.get("PORT", 5000)))
    parser.add_argument("--magic",  action="store_true",
                        help="Scan full US+SGX universe and show top momentum setups")
    parser.add_argument("--market", choices=["us","sg","both"], default="both",
                        help="Universe for --magic scan (default: both)")
    parser.add_argument("--top",    type=int, default=5,
                        help="Number of top results for --magic / --daily (default: 5)")
    parser.add_argument("--daily",  action="store_true",
                        help="Morning briefing: regime + top picks with chart levels")
    args = parser.parse_args()

    if args.serve:
        serve(port=args.port)
        return

    if args.daily:
        briefing = daily_picks(market=args.market, top=args.top)
        print_daily_briefing(briefing)
        return

    if args.magic:
        results = magic_find(market=args.market, top=args.top)
        if results:
            print_summary_table(results)
            for i, a in enumerate(results, 1):
                print_card(a, i)
        return

    tickers = [t.upper() for t in args.tickers] if args.tickers else PRESETS[args.preset]
    try:
        run(tickers, watch=args.watch)
    except KeyboardInterrupt:
        print(f"\n  {dim('Stopped.')}\n")

if __name__ == "__main__":
    main()