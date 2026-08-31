#!/usr/bin/env python3
"""
RESEARCH GOOGLE — Trading Signal Engine
=========================================
Google Search (via SerpAPI) → Gemini → Trade Decision

Output is a TRADE SIGNAL, not a report. Gemini reads the news and
answers one question: "Should I take this trade, and how?"

Setup:
  export SERPAPI_KEY=abc123...        # serpapi.com/manage-api-key (250 free/month)
  export GEMINI_API_KEY=AIzaSy...    # aistudio.google.com (250 free/day)

Install:
  pip install google-genai requests

Usage:
  python research_google.py --check
  python research_google.py FLEX
  python research_google.py FLEX --price 142.26 --setup "MOMENTUM CONT." --score 74
"""

import os, sys, json, time, hashlib, threading, argparse
from datetime import datetime, date
from typing import Optional
from pathlib import Path
import requests

# ── CACHE (disk-backed, survives server restart) ──────────────────────────────
_cache_dir = Path(os.environ.get("RESEARCH_CACHE_DIR", "/tmp/rg_cache"))
_cache_dir.mkdir(exist_ok=True)
CACHE_TTL  = int(os.environ.get("RESEARCH_CACHE_TTL", 3600))

def _ck(ticker: str) -> str:
    return hashlib.md5(ticker.upper().encode()).hexdigest()

def _cache_get(ticker: str) -> Optional[dict]:
    f = _cache_dir / f"{_ck(ticker)}.json"
    try:
        if f.exists():
            d = json.loads(f.read_text())
            if time.time() - d["ts"] < CACHE_TTL:
                return d["data"]
    except Exception:
        pass
    return None

def _cache_set(ticker: str, data: dict):
    try:
        (_cache_dir / f"{_ck(ticker)}.json").write_text(
            json.dumps({"ts": time.time(), "data": data}))
    except Exception:
        pass

# ── QUOTA TRACKER ─────────────────────────────────────────────────────────────
_lock = threading.Lock()
_quota_dir = Path(os.environ.get("TMPDIR", "/tmp"))

class QuotaTracker:
    def __init__(self, name: str, daily: int, rpm: int):
        self.name  = name
        self.daily = daily
        self.rpm   = rpm
        self._file = _quota_dir / f"qt_{name}.json"
        self._min: list = []

    def _load(self) -> int:
        try:
            d = json.loads(self._file.read_text())
            return int(d["count"]) if d.get("date") == str(date.today()) else 0
        except Exception:
            return 0

    def _save(self, n: int):
        try:
            self._file.write_text(json.dumps({"date": str(date.today()), "count": n}))
        except Exception:
            pass

    def check(self) -> tuple[bool, str]:
        with _lock:
            now = time.time()
            n   = self._load()
            if n >= self.daily:
                return False, f"{self.name} daily limit {n}/{self.daily}. Resets midnight PT."
            self._min = [t for t in self._min if now - t < 60]
            if len(self._min) >= self.rpm:
                wait = int(61 - (now - self._min[0]))
                return False, f"{self.name} rate limit — wait {wait}s"
            self._save(n + 1)
            self._min.append(now)
            return True, f"{n+1}/{self.daily}"

    def remaining(self) -> int:
        return max(0, self.daily - self._load())

    def status(self) -> dict:
        used = self._load()
        return {"daily_limit": self.daily, "daily_used": used,
                "daily_remaining": self.daily - used, "rpm_limit": self.rpm}

GOOGLE_DAILY = int(os.environ.get("GOOGLE_DAILY_LIMIT", 90))
GEMINI_DAILY = int(os.environ.get("GEMINI_DAILY_LIMIT", 240))
_gq = QuotaTracker("gsearch", GOOGLE_DAILY, 10)
_mq = QuotaTracker("gemini",  GEMINI_DAILY, 8)

# ── SERPAPI SEARCH ────────────────────────────────────────────────────────────
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
# GOOGLE_API_KEY / GOOGLE_CX no longer needed — SerpAPI handles everything

def _search(query: str, days: int = 7) -> list[dict]:
    """SerpAPI Google Search — reliable, no CORS/CX setup needed."""
    if not SERPAPI_KEY:
        return []
    ok, msg = _gq.check()
    if not ok:
        print(f"  [SERP] ⚠ {msg}", flush=True)
        return []

    # tbs=qdr:wN = last N weeks; qdr:dN = last N days
    tbs = f"qdr:d{min(days, 30)}"

    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={
                "api_key": SERPAPI_KEY,
                "engine":  "google",
                "q":       query,
                "num":     5,
                "tbs":     tbs,
                "hl":      "en",
                "gl":      "us",
            },
            timeout=15,
        )
        if r.status_code == 401:
            print(f"  [SERP] ✗ 401 Invalid API key — check SERPAPI_KEY", flush=True)
            return []
        if r.status_code == 429:
            print(f"  [SERP] ✗ 429 Rate limited — monthly quota likely exhausted", flush=True)
            return []
        if r.status_code != 200:
            print(f"  [SERP] ✗ HTTP {r.status_code}: {r.text[:100]}", flush=True)
            return []

        data    = r.json()
        results = []
        for item in data.get("organic_results", []):
            results.append({
                "title":   item.get("title",   ""),
                "snippet": item.get("snippet", ""),
                "link":    item.get("link",    ""),
                "date":    item.get("date",    ""),
            })

        if not results:
            # SerpAPI returns error_message when no results / bad query
            err = data.get("error", "")
            if err:
                print(f"  [SERP] ✗ API error: {err[:100]}", flush=True)

        return results

    except requests.exceptions.Timeout:
        print(f"  [SERP] ✗ Timeout — SerpAPI slow, retry later", flush=True)
        return []
    except Exception as e:
        print(f"  [SERP] ✗ {e}", flush=True)
        return []

def _fetch(ticker: str, days: int, verbose: bool) -> tuple[list[str], int]:
    """Run 4 targeted searches. Returns (snippets, queries_used)."""
    if not SERPAPI_KEY:
        return [], 0

    year = datetime.now().year
    sgx  = " SGX" if ticker.endswith(".SI") else ""
    base = ticker.replace(".SI","")

    queries = [
        f"{ticker} stock news earnings catalyst {year}{sgx}",
        f"{ticker} analyst upgrade downgrade price target {year}",
        f"{ticker} risk warning regulatory SEC {year}",
        f"{base} insider buying selling Form 4 {year}",
    ]

    snippets, used = [], 0
    for q in queries:
        if verbose:
            print(f"  [SERP] ({_gq.remaining()} left) {q[:60]}", flush=True)
        for item in _search(q, days):
            snippets.append(f"{item['title']} — {item['snippet'][:220]}")
        used += 1
        time.sleep(0.2)

    if verbose:
        print(f"  [SERP] {len(snippets)} snippets, {_gq.remaining()} remaining today", flush=True)
    return snippets, used

# ── GEMINI MODEL CHAIN ────────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Priority-ordered fallback list. First model is tried first; on 429/quota
# the next one is tried automatically. Override via GEMINI_MODEL env var
# (comma-separated list, or single model name).
_DEFAULT_MODEL_CHAIN = [
    "gemini-2.5-flash",        # 20 RPD  — primary, best quality
    "gemini-3-flash",          # 20 RPD  — fresh quota pool
    "gemini-2.5-flash-lite",   # 20 RPD  — lighter, faster
    "gemini-3.1-flash-lite",   # 500 RPD — best free fallback
    "gemini-2.0-flash-lite",   # fallback
]

_env_model = os.environ.get("GEMINI_MODEL", "")
if _env_model:
    # Allow comma-separated override: "gemini-3.1-flash-lite,gemini-2.5-flash"
    MODEL_CHAIN = [m.strip() for m in _env_model.split(",") if m.strip()]
else:
    MODEL_CHAIN = _DEFAULT_MODEL_CHAIN

# Active model index — bumped on 429, wraps back on success
_model_idx     = 0
_model_idx_lock = threading.Lock()

def get_active_model() -> str:
    return MODEL_CHAIN[_model_idx % len(MODEL_CHAIN)]

def bump_model(reason: str = "") -> str:
    """Rotate to next model in chain. Returns new model name."""
    global _model_idx
    with _model_idx_lock:
        _model_idx = (_model_idx + 1) % len(MODEL_CHAIN)
        new = MODEL_CHAIN[_model_idx]
    print(f"  [MODEL] ↻ Switched to {new} ({reason})", flush=True)
    return new

def set_model(name: str) -> bool:
    """Manually set active model by name. Returns True if found."""
    global _model_idx
    if name in MODEL_CHAIN:
        with _model_idx_lock:
            _model_idx = MODEL_CHAIN.index(name)
        print(f"  [MODEL] Set to {name}", flush=True)
        return True
    # Allow setting a model not in the chain by inserting at front
    with _model_idx_lock:
        MODEL_CHAIN.insert(0, name)
        _model_idx = 0
    print(f"  [MODEL] Added+set {name}", flush=True)
    return True

# Keep GEMINI_MODEL as alias for backward compat
GEMINI_MODEL = property(get_active_model)

def _build_signal_prompt(
    ticker: str,
    snippets: list[str],
    price: float = 0,
    setup: str = "",
    score: int = 0,
) -> str:
    today    = datetime.now().strftime("%Y-%m-%d")
    sgx_note = "SGX-listed stock priced in SGD." if ticker.endswith(".SI") else ""
    tech_ctx = ""
    if price and setup:
        tech_ctx = (f"\nTechnical context (from screener): "
                    f"price=${price:.2f}, setup={setup}, confidence={score}/100")

    news_block = "\n".join(f"[{i+1}] {s}" for i, s in enumerate(snippets)) if snippets else "No news found."

    return f"""You are a professional momentum trader making a real trade decision.
Ticker: {ticker}  {sgx_note}
Date: {today}{tech_ctx}

Recent news (Google Search):
{news_block}

Read the news above. Then answer: should I enter a long trade on {ticker} right now?

Return ONLY this JSON — no markdown, no explanation:
{{
  "ticker": "{ticker}",
  "decision": "BUY | WAIT | AVOID",
  "conviction": 1-10,
  "entry_ok": true or false,
  "headline": "<single most important news item affecting this trade>",
  "catalyst": "<what is driving or could drive price higher — or 'None found'>",
  "risk": "<single biggest reason this trade could fail>",
  "insider": "<insider buying/selling if found — or 'None found'>",
  "analyst_call": "<most recent analyst rating+target if found — or 'None found'>",
  "news_sentiment": "bullish | bearish | neutral | mixed",
  "hold_overnight": true or false,
  "reasoning": "<2-3 sentences max — why BUY/WAIT/AVOID based only on the news above>"
}}

Decision rules:
- BUY: news confirms momentum, no red flags, catalyst present
- WAIT: mixed signals, unclear catalyst, or no meaningful news
- AVOID: negative catalyst, earnings miss, SEC/regulatory issue, insider selling, downgrade"""

# ── GEMINI CALL (with auto model fallback) ────────────────────────────────────
def _gemini(ticker: str, prompt: str, verbose: bool, max_output_tokens: int = 4096) -> dict:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return _err(ticker, "google-genai not installed. Run: pip install google-genai")

    if not GEMINI_API_KEY:
        return _err(ticker, "GEMINI_API_KEY not set — get free key at aistudio.google.com")

    # Try each model in chain until one works
    tried = set()
    for attempt in range(len(MODEL_CHAIN)):
        model = get_active_model()
        if model in tried:
            break
        tried.add(model)

        ok, msg = _mq.check()
        if not ok:
            print(f"  [GEMINI] ⚠ quota tracker: {msg} — trying next model", flush=True)
            bump_model("quota tracker")
            continue

        if verbose:
            print(f"  [GEMINI] {model} ({_mq.remaining()} calls left)...", flush=True)

        try:
            _saved = os.environ.pop("GOOGLE_API_KEY", None)
            client = genai.Client(api_key=GEMINI_API_KEY)
            if _saved: os.environ["GOOGLE_API_KEY"] = _saved

            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=max_output_tokens,
                    response_mime_type="application/json",
                ),
            )
            raw = resp.text.strip() if resp.text else ""
            if verbose:
                print(f"  [GEMINI] {model} → {len(raw)} chars", flush=True)
            result = _parse(ticker, raw, verbose)
            result["_model_used"] = model
            return result

        except Exception as e:
            s = str(e)
            if "429" in s or "quota" in s.lower() or "RESOURCE_EXHAUSTED" in s:
                next_m = bump_model(f"429 on {model}")
                if verbose:
                    print(f"  [GEMINI] Rate limited on {model} — trying {next_m}", flush=True)
                time.sleep(2)
                continue
            if "API_KEY" in s or "INVALID_ARGUMENT" in s:
                return _err(ticker, f"Gemini auth error — verify GEMINI_API_KEY. {s[:80]}")
            return _err(ticker, f"Gemini error on {model}: {s[:120]}")

    return _err(ticker, f"All Gemini models exhausted: {', '.join(MODEL_CHAIN)}")

# ── JSON PARSE ────────────────────────────────────────────────────────────────
def _parse(ticker: str, text: str, verbose: bool) -> dict:
    if not text:
        return _err(ticker, "Empty response from Gemini")
    clean = text.strip()
    if "```" in clean:
        clean = "\n".join(l for l in clean.split("\n") if not l.strip().startswith("```"))
    s, e = clean.find("{"), clean.rfind("}") + 1
    if s == -1 or e == 0:
        if verbose: print(f"  [PARSE] No JSON. Raw:\n{text[:300]}", flush=True)
        return _err(ticker, "Gemini did not return JSON")
    try:
        d = json.loads(clean[s:e])
        d["_cached"] = False
        return d
    except json.JSONDecodeError as ex:
        if verbose: print(f"  [PARSE] JSONDecodeError: {ex}", flush=True)
        return _err(ticker, f"JSON parse error: {ex}")

def _err(ticker: str, msg: str) -> dict:
    return {
        "ticker": ticker, "decision": "WAIT", "conviction": 0,
        "entry_ok": False, "headline": "Research unavailable",
        "catalyst": "N/A", "risk": msg, "insider": "N/A",
        "analyst_call": "N/A", "news_sentiment": "neutral",
        "hold_overnight": False,
        "reasoning": f"Research failed: {msg}",
        "error": msg, "_cached": False,
    }

# ── PUBLIC API ────────────────────────────────────────────────────────────────
def research(
    ticker: str,
    max_data_age_days: int = 7,
    price: float = 0,
    setup: str = "",
    score: int = 0,
    use_cache: bool = True,
    verbose: bool = False,
) -> dict:
    """
    Fetch Google news (via SerpAPI) + Gemini trade signal for a ticker.

    Returns a compact trade decision dict:
      decision: BUY | WAIT | AVOID
      conviction: 1-10
      entry_ok: bool
      headline, catalyst, risk, insider, analyst_call
      news_sentiment: bullish | bearish | neutral | mixed
      hold_overnight: bool
      reasoning: 2-3 sentence max
    """
    ticker = ticker.upper().strip()

    if use_cache:
        cached = _cache_get(ticker)
        if cached:
            if verbose: print(f"  [CACHE] Hit for {ticker}", flush=True)
            cached["_cached"] = True
            return cached

    if not GEMINI_API_KEY:
        return _err(ticker, "Missing GEMINI_API_KEY — get free key at aistudio.google.com")

    if not SERPAPI_KEY and verbose:
        print(f"  [SERP] ⚠ No SERPAPI_KEY — Gemini will use training knowledge only", flush=True)

    snippets, n_queries = _fetch(ticker, max_data_age_days, verbose)
    prompt = _build_signal_prompt(ticker, snippets, price, setup, score)
    result = _gemini(ticker, prompt, verbose)

    result["_queries_used"]     = n_queries
    result["_google_remaining"] = _gq.remaining()
    result["_gemini_remaining"] = _mq.remaining()

    if "error" not in result:
        _cache_set(ticker, result)

    return result

# ── FLASK ROUTES ──────────────────────────────────────────────────────────────
def register_research_routes(app, require_api_key_decorator):
    """Drop-in replacement — same signature as research.py."""

    @app.route("/api/research")
    @require_api_key_decorator
    def api_research():
        from flask import request, jsonify, abort
        ticker = request.args.get("ticker", "").upper().strip()
        days   = int(request.args.get("days",  7))
        price  = float(request.args.get("price", 0))
        setup  = request.args.get("setup", "")
        score  = int(request.args.get("score", 0))
        fresh  = request.args.get("fresh", "false").lower() == "true"
        if not ticker: abort(400, "ticker required")
        return jsonify(research(
            ticker=ticker, max_data_age_days=days,
            price=price, setup=setup, score=score,
            use_cache=not fresh, verbose=True,
        ))

    @app.route("/api/research/quota")
    @require_api_key_decorator
    def api_quota():
        from flask import jsonify
        return jsonify({
            "google": _gq.status(), "gemini": _mq.status(),
            "keys": {k: bool(v) for k, v in {
                "SERPAPI_KEY":    SERPAPI_KEY,
                "GEMINI_API_KEY": GEMINI_API_KEY,
            }.items()},
            "active_model":  get_active_model(),
            "model_chain":   MODEL_CHAIN,
        })

    @app.route("/api/models", methods=["GET"])
    @require_api_key_decorator
    def api_models_get():
        from flask import jsonify
        return jsonify({
            "active":  get_active_model(),
            "chain":   MODEL_CHAIN,
            "all":     _DEFAULT_MODEL_CHAIN,
        })

    @app.route("/api/models", methods=["POST"])
    @require_api_key_decorator
    def api_models_set():
        from flask import request, jsonify
        body  = request.get_json(silent=True) or {}
        model = body.get("model", "").strip()
        if not model:
            return jsonify({"error": "body must include {model: '...'}"}), 400
        set_model(model)
        return jsonify({"active": get_active_model(), "chain": MODEL_CHAIN})

# ── CLI ───────────────────────────────────────────────────────────────────────
def _print_signal(r: dict):
    try:
        from colorama import Fore, Style, init; init(autoreset=True)
        G=Fore.GREEN; R=Fore.RED; Y=Fore.YELLOW; C=Fore.CYAN; B=Style.BRIGHT; RS=Style.RESET_ALL
    except ImportError:
        G=R=Y=C=B=RS=""

    if r.get("error") and r.get("conviction", 0) == 0:
        print(f"\n  ✗ {r['error']}\n"); return

    dec   = r.get("decision", "WAIT")
    conv  = r.get("conviction", 0)
    ok    = r.get("entry_ok", False)
    sent  = r.get("news_sentiment", "neutral").upper()
    night = r.get("hold_overnight", False)

    dec_col  = G if dec=="BUY" else R if dec=="AVOID" else Y
    sent_col = G if sent=="BULLISH" else R if sent=="BEARISH" else Y
    cached   = " [CACHED]" if r.get("_cached") else ""
    grem     = r.get("_google_remaining", "?")
    mrem     = r.get("_gemini_remaining", "?")

    print(f"\n{'═'*60}")
    print(f"  TRADE SIGNAL — {r.get('ticker','')}  {datetime.now().strftime('%Y-%m-%d %H:%M')}{cached}")
    model_used = r.get("_model_used", get_active_model())
    print(f"  SerpAPI: {grem} left  Gemini: {mrem} left  Model: {model_used}")
    print(f"{'═'*60}")
    print(f"\n  {B}DECISION:{RS}       {dec_col}{B}{dec}{RS}  (conviction {conv}/10)")
    print(f"  {B}Entry OK:{RS}        {'✓ YES' if ok else '✗ NO'}")
    print(f"  {B}News sentiment:{RS}  {sent_col}{sent}{RS}")
    print(f"  {B}Hold overnight:{RS}  {'YES' if night else 'NO'}")
    print(f"\n  {B}Headline:{RS}    {r.get('headline','—')}")
    print(f"  {B}Catalyst:{RS}    {r.get('catalyst','—')}")
    print(f"  {B}Risk:{RS}        {r.get('risk','—')}")
    if r.get("insider") and r["insider"] != "None found":
        print(f"  {B}Insider:{RS}     {r['insider']}")
    if r.get("analyst_call") and r["analyst_call"] != "None found":
        print(f"  {B}Analyst:{RS}     {r['analyst_call']}")
    print(f"\n  {B}Reasoning:{RS}")
    words = r.get("reasoning","").split()
    line = "  "
    for w in words:
        if len(line)+len(w) > 62: print(line); line = "  "+w+" "
        else: line += w+" "
    if line.strip(): print(line)
    print(f"\n{'═'*60}\n")

def main():
    p = argparse.ArgumentParser(description="Trade signal: SerpAPI Google Search + Gemini")
    p.add_argument("ticker", nargs="?")
    p.add_argument("--days",  type=int,   default=7)
    p.add_argument("--price", type=float, default=0)
    p.add_argument("--setup", default="")
    p.add_argument("--score", type=int,   default=0)
    p.add_argument("--json",  action="store_true")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--check", action="store_true")
    args = p.parse_args()

    if args.check or not args.ticker:
        print(f"\n  {'✓' if SERPAPI_KEY    else '○'}  SERPAPI_KEY       {'set' if SERPAPI_KEY    else 'optional — serpapi.com (adds live news)'}")
        print(f"  {'✓' if GEMINI_API_KEY else '✗'}  GEMINI_API_KEY    {'set' if GEMINI_API_KEY else 'NOT SET — aistudio.google.com'}")
        print(f"\n  Active model:  {get_active_model()}")
        print(f"  Model chain:   {' → '.join(MODEL_CHAIN)}")
        print(f"\n  SerpAPI: {_gq.remaining()}/{GOOGLE_DAILY}/day  Gemini: {_mq.remaining()}/{GEMINI_DAILY}/day  Cost: $0.00\n")
        if not args.ticker: return

    result = research(
        ticker=args.ticker, max_data_age_days=args.days,
        price=args.price, setup=args.setup, score=args.score,
        use_cache=not args.fresh, verbose=True,
    )
    if args.json: print(json.dumps(result, indent=2))
    else: _print_signal(result)

if __name__ == "__main__":
    main()