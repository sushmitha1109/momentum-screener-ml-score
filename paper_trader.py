#!/usr/bin/env python3
"""
PAPER TRADER — Alpaca Paper Trading Integration
================================================
Connects momentum screener signals to Alpaca's paper trading API.
Tracks every signal in a local log so performance can be measured.

Setup:
  export ALPACA_API_KEY=your_key_id
  export ALPACA_SECRET_KEY=your_secret_key

Get keys: alpaca.markets → Paper Trading → API Keys

Position sizing uses risk-based approach:
  conservative → 1% of equity at risk per trade
  moderate     → 2%
  aggressive   → 4%

Usage:
  python paper_trader.py --check          # verify API keys + balance
  python paper_trader.py --positions      # show open positions
  python paper_trader.py --performance    # show signal log stats
"""

import os, sys, json, uuid, argparse
import requests
from datetime import datetime, timezone
from pathlib import Path

ALPACA_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE   = "https://paper-api.alpaca.markets/v2"
SIGNAL_LOG    = Path(os.environ.get("SIGNAL_LOG", "/tmp/momentum_signals.json"))

RISK_PCT = {
    "conservative": 0.01,
    "moderate":     0.02,
    "aggressive":   0.04,
}

# ── ALPACA CLIENT ─────────────────────────────────────────────────────────────
def _headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
        "Content-Type":        "application/json",
    }

def _req(method, path, **kwargs):
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY env vars not set")
    r = requests.request(method, ALPACA_BASE + path, headers=_headers(), timeout=15, **kwargs)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    if not r.ok:
        msg = data.get("message") or data.get("error") or f"HTTP {r.status_code}"
        raise RuntimeError(msg)
    return data

def is_configured():
    return bool(ALPACA_KEY and ALPACA_SECRET)

def get_account():
    return _req("GET", "/account")

def get_positions():
    pos = _req("GET", "/positions")
    log = _load_log()
    sig_map = {s["ticker"]: s for s in reversed(log.get("signals", []))}
    for p in pos:
        s = sig_map.get(p["symbol"])
        if s:
            p["_signal_score"] = s.get("score")
            p["_signal_setup"] = s.get("setup")
            p["_signal_id"]    = s.get("id")
    return pos

def get_orders(status="all", limit=50):
    return _req("GET", f"/orders?status={status}&limit={limit}&direction=desc")

def cancel_order(order_id):
    return _req("DELETE", f"/orders/{order_id}")

def close_position(ticker):
    return _req("DELETE", f"/positions/{ticker}")

# ── POSITION SIZING ───────────────────────────────────────────────────────────
def _calc_qty(entry, stop, equity, risk_profile):
    """Risk-based position sizing: lose at most risk_pct * equity if stop is hit."""
    risk_pct       = RISK_PCT.get(risk_profile, 0.02)
    max_loss       = equity * risk_pct
    risk_per_share = max(abs(entry - stop), 0.01)
    qty            = max(1, round(max_loss / risk_per_share))
    max_position   = equity * 0.15              # never more than 15% in one name
    qty            = min(qty, int(max_position / entry))
    return max(qty, 1)

# ── SIGNAL LOG ────────────────────────────────────────────────────────────────
def _load_log():
    if SIGNAL_LOG.exists():
        try:
            return json.loads(SIGNAL_LOG.read_text())
        except Exception:
            pass
    return {"signals": [], "version": 1}

def _save_log(log):
    SIGNAL_LOG.write_text(json.dumps(log, indent=2))

def _log_signal(entry):
    log = _load_log()
    log["signals"].append(entry)
    _save_log(log)

def update_signal_status(signal_id, status, notes=""):
    """Mark a signal as 'win' | 'loss' | 'cancelled' for performance tracking."""
    log = _load_log()
    for s in log["signals"]:
        if s.get("id") == signal_id:
            s["status"] = status
            s["status_notes"] = notes
            s["status_updated"] = datetime.now(timezone.utc).isoformat()
            break
    _save_log(log)

def get_signal_log(limit=50):
    log = _load_log()
    return list(reversed(log.get("signals", [])))[:limit]

# ── TRADE SUBMISSION ──────────────────────────────────────────────────────────
def submit_trade(signal, risk_profile="moderate"):
    """
    Submit a bracket order to Alpaca for a screener signal.

    signal must include:
      ticker, price, stop, t1 (take-profit)
      optionally: score, setup, adx, rsi, rvol, macd_bull, regime

    Returns: {signal_id, order, logged, qty, equity}
    """
    ticker      = signal["ticker"].upper()
    entry       = float(signal["price"])
    stop        = float(signal["stop"])
    take_profit = float(signal["t1"])

    if ticker.endswith(".SI"):
        raise ValueError(f"{ticker} is an SGX stock — Alpaca only supports US equities")

    if stop >= entry:
        raise ValueError(f"Stop ${stop} must be below entry ${entry}")
    if take_profit <= entry:
        raise ValueError(f"Take-profit ${take_profit} must be above entry ${entry}")

    acct   = get_account()
    equity = float(acct.get("equity") or acct.get("portfolio_value") or 100_000)
    qty    = _calc_qty(entry, stop, equity, risk_profile)

    payload = {
        "symbol":        ticker,
        "qty":           str(qty),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
        "order_class":   "bracket",
        "stop_loss":     {"stop_price":  f"{stop:.2f}"},
        "take_profit":   {"limit_price": f"{take_profit:.2f}"},
    }
    order = _req("POST", "/orders", json=payload)

    signal_id = str(uuid.uuid4())[:8]
    logged = {
        "id":              signal_id,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "ticker":          ticker,
        "setup":           signal.get("setup", "?"),
        "score":           signal.get("score", 0),
        "entry":           entry,
        "stop":            stop,
        "t1":              take_profit,
        "t2":              float(signal.get("t2", take_profit)),
        "rr":              signal.get("rr", 0),
        "qty":             qty,
        "position_value":  round(qty * entry, 2),
        "max_loss":        round(qty * (entry - stop), 2),
        "risk_profile":    risk_profile,
        "risk_pct":        RISK_PCT.get(risk_profile, 0.02),
        "adx":             signal.get("adx", 0),
        "rsi":             signal.get("rsi", 0),
        "rvol":            signal.get("rvol", 0),
        "macd_bull":       signal.get("macd_bull", False),
        "e9":              signal.get("e9"),
        "e20":             signal.get("e20"),
        "e50":             signal.get("e50"),
        "regime":          signal.get("regime", "UNKNOWN"),
        "alpaca_order_id": order.get("id"),
        "status":          "submitted",
    }
    _log_signal(logged)
    return {
        "signal_id": signal_id,
        "order":     order,
        "logged":    logged,
        "qty":       qty,
        "equity":    equity,
    }

# ── PERFORMANCE ───────────────────────────────────────────────────────────────
def get_performance():
    """
    Stats from local signal log + Alpaca order history.
    Win/loss tracking requires manual status updates via update_signal_status().
    """
    log     = _load_log()
    signals = log.get("signals", [])

    wins     = [s for s in signals if s.get("status") == "win"]
    losses   = [s for s in signals if s.get("status") == "loss"]
    pending  = [s for s in signals if s.get("status") == "submitted"]
    total_decided = len(wins) + len(losses)

    avg_rr    = (sum(s.get("rr", 0) for s in signals) / len(signals)) if signals else 0
    avg_score = (sum(s.get("score", 0) for s in signals) / len(signals)) if signals else 0

    top_setups = {}
    for s in signals:
        setup = s.get("setup", "?")
        if setup not in top_setups:
            top_setups[setup] = {"count": 0, "wins": 0}
        top_setups[setup]["count"] += 1
        if s.get("status") == "win":
            top_setups[setup]["wins"] += 1

    return {
        "total_signals":  len(signals),
        "submitted":      sum(1 for s in signals if s.get("alpaca_order_id")),
        "pending":        len(pending),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(len(wins) / total_decided * 100, 1) if total_decided else None,
        "avg_rr":         round(avg_rr, 2),
        "avg_score":      round(avg_score, 1),
        "setup_breakdown": {
            k: {"count": v["count"],
                "win_rate": round(v["wins"] / v["count"] * 100, 1) if v["count"] else 0}
            for k, v in top_setups.items()
        },
        "recent":         list(reversed(signals))[:10],
    }

# ── FLASK ROUTES ──────────────────────────────────────────────────────────────
def register_paper_routes(app, require_api_key):
    from flask import request, jsonify, abort

    @app.route("/api/paper/status")
    @require_api_key
    def api_paper_status():
        configured = is_configured()
        if not configured:
            return jsonify({
                "configured": False,
                "message": "Set ALPACA_API_KEY and ALPACA_SECRET_KEY env vars",
            })
        try:
            acct = get_account()
            return jsonify({
                "configured":    True,
                "equity":        acct.get("equity"),
                "buying_power":  acct.get("buying_power"),
                "cash":          acct.get("cash"),
                "account_status": acct.get("status"),
            })
        except Exception as e:
            return jsonify({"configured": True, "error": str(e)}), 500

    @app.route("/api/paper/positions")
    @require_api_key
    def api_paper_positions():
        try:
            return jsonify(get_positions())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/paper/orders")
    @require_api_key
    def api_paper_orders():
        try:
            status = request.args.get("status", "all")
            return jsonify(get_orders(status=status, limit=50))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/paper/trade", methods=["POST"])
    @require_api_key
    def api_paper_trade():
        body         = request.get_json(silent=True) or {}
        signal       = body.get("signal")
        risk_profile = body.get("risk", "moderate")
        if not signal or not signal.get("ticker"):
            abort(400, "body must include {signal: {...}, risk: 'moderate'}")
        if risk_profile not in RISK_PCT:
            abort(400, f"risk must be one of: {list(RISK_PCT.keys())}")
        try:
            result = submit_trade(signal, risk_profile=risk_profile)
            return jsonify(result)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/paper/signals")
    @require_api_key
    def api_paper_signals():
        limit = int(request.args.get("limit", 50))
        return jsonify(get_signal_log(limit=limit))

    @app.route("/api/paper/signals/<signal_id>", methods=["PATCH"])
    @require_api_key
    def api_paper_signal_update(signal_id):
        body   = request.get_json(silent=True) or {}
        status = body.get("status", "")
        notes  = body.get("notes", "")
        if status not in ("win", "loss", "cancelled"):
            abort(400, "status must be win | loss | cancelled")
        update_signal_status(signal_id, status, notes)
        return jsonify({"ok": True, "id": signal_id, "status": status})

    @app.route("/api/paper/performance")
    @require_api_key
    def api_paper_performance():
        try:
            return jsonify(get_performance())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/paper/cancel/<order_id>", methods=["POST"])
    @require_api_key
    def api_paper_cancel(order_id):
        try:
            return jsonify(cancel_order(order_id))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Paper Trader — Alpaca integration")
    p.add_argument("--check",       action="store_true", help="Verify API keys and show account balance")
    p.add_argument("--positions",   action="store_true", help="Show open positions")
    p.add_argument("--orders",      action="store_true", help="Show recent orders")
    p.add_argument("--performance", action="store_true", help="Show signal log performance stats")
    p.add_argument("--signals",     action="store_true", help="Show recent signal log entries")
    p.add_argument("--win",  metavar="SIGNAL_ID", help="Mark signal as WIN")
    p.add_argument("--loss", metavar="SIGNAL_ID", help="Mark signal as LOSS")
    args = p.parse_args()

    if not is_configured():
        print("\n  ✗ ALPACA_API_KEY and ALPACA_SECRET_KEY not set")
        print("  Get keys at alpaca.markets → Paper Trading → API Keys\n")
        sys.exit(1)

    if args.check:
        acct = get_account()
        print(f"\n  ✓ Alpaca paper account connected")
        print(f"  Equity       : ${float(acct['equity']):,.2f}")
        print(f"  Buying power : ${float(acct['buying_power']):,.2f}")
        print(f"  Cash         : ${float(acct['cash']):,.2f}")
        print(f"  Status       : {acct['status']}\n")

    if args.positions:
        pos = get_positions()
        if not pos:
            print("\n  No open positions\n")
        else:
            print(f"\n  {'TICKER':<8} {'QTY':>6} {'ENTRY':>8} {'CURRENT':>8} {'P&L':>10} {'P&L%':>7}")
            print(f"  {'─'*8} {'─'*6} {'─'*8} {'─'*8} {'─'*10} {'─'*7}")
            for p in pos:
                pnl = float(p.get("unrealized_pl", 0))
                pnl_pct = float(p.get("unrealized_plpc", 0)) * 100
                col = "+" if pnl >= 0 else ""
                print(f"  {p['symbol']:<8} {p['qty']:>6} ${float(p['avg_entry_price']):>7.2f} "
                      f"${float(p['current_price']):>7.2f} {col}${pnl:>9.2f} {col}{pnl_pct:>6.1f}%")
        print()

    if args.performance:
        perf = get_performance()
        print(f"\n  SIGNAL LOG PERFORMANCE")
        print(f"  Total signals  : {perf['total_signals']}")
        print(f"  Submitted      : {perf['submitted']}")
        print(f"  Pending        : {perf['pending']}")
        print(f"  Wins           : {perf['wins']}")
        print(f"  Losses         : {perf['losses']}")
        wr = perf.get('win_rate')
        print(f"  Win rate       : {f'{wr}%' if wr else 'N/A (mark wins/losses with --win/--loss)'}")
        print(f"  Avg score      : {perf['avg_score']}/100")
        print(f"  Avg R:R        : 1:{perf['avg_rr']}")
        if perf["setup_breakdown"]:
            print(f"\n  Setup breakdown:")
            for setup, stats in perf["setup_breakdown"].items():
                print(f"    {setup:<20} {stats['count']} trades  {stats['win_rate']}% win rate")
        print()

    if args.signals:
        sigs = get_signal_log(limit=20)
        if not sigs:
            print("\n  No signals logged yet\n")
        else:
            print(f"\n  {'ID':<8} {'DATE':<12} {'TICKER':<8} {'SETUP':<18} {'SCORE':>5} {'STATUS'}")
            print(f"  {'─'*8} {'─'*12} {'─'*8} {'─'*18} {'─'*5} {'─'*10}")
            for s in sigs:
                ts   = s["timestamp"][:10]
                stat = s.get("status", "submitted")
                print(f"  {s['id']:<8} {ts:<12} {s['ticker']:<8} {s.get('setup','?'):<18} "
                      f"{s.get('score',0):>5} {stat}")
        print()

    if args.win:
        update_signal_status(args.win, "win")
        print(f"  ✓ Signal {args.win} marked as WIN\n")

    if args.loss:
        update_signal_status(args.loss, "loss")
        print(f"  ✓ Signal {args.loss} marked as LOSS\n")

if __name__ == "__main__":
    main()
