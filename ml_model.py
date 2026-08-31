#!/usr/bin/env python3
"""
ML MODEL — learned win-probability score, calibrated from history
===================================================================
Replaces nothing; augments the hand-tuned point score in momentum_screener.py
with a logistic regression trained on: "given these technicals, did price move
up by more than `threshold` within `horizon` trading days, historically?"

Standalone leaf module (no import from momentum_screener.py, to avoid a
circular import — see CLAUDE.md's module-wiring convention). Degrades to
predict_proba() -> None if scikit-learn isn't installed or no model has been
trained yet; callers must treat that as "ML unavailable", not an error.

Usage:
  python ml_model.py --train                          # default universe, 1y, 5d horizon
  python ml_model.py --train --tickers NVDA,AMD,FLEX --period 2y --horizon 5
  python ml_model.py --info                            # show trained model metadata
"""

import sys
import argparse
from pathlib import Path
from datetime import datetime, timezone

MODEL_PATH = Path(__file__).parent / "models" / "momentum_model.joblib"

# Canonical feature order — momentum_screener.analyze() must build a dict with
# exactly these keys before calling predict_proba(). Keep the two in sync.
FEATURE_COLUMNS = [
    "adx", "rsi", "rvol", "macd_bull", "squeeze",
    "pct20", "above_e9", "above_e20", "above_e50",
]

DEFAULT_UNIVERSE = [
    "NVDA", "AMD", "MSFT", "AAPL", "AMZN", "META", "GOOGL", "TSLA", "NFLX",
    "PLTR", "SMCI", "FLEX", "JBL", "DELL", "HPE", "ARM", "AVGO",
    "MARA", "RIOT", "CLSK", "BITF", "HIVE", "WULF",
    "JPM", "GS", "BAC", "XOM", "CVX", "MPC", "COP",
    "CRWD", "SNOW", "MDB", "NET", "DDOG", "ZS", "HUBS",
]

try:
    import yfinance as yf
    import pandas as pd
except ImportError as e:
    print(f"Missing dependency: {e}\npip install -r requirements.txt")
    sys.exit(1)

_model_cache = None  # lazy-loaded {"pipeline", "features", "horizon", "threshold", ...}


# ── standalone indicator math ──────────────────────────────────────────────
# Deliberately duplicated from momentum_screener.py (not imported) so this
# module stays a leaf with no circular dependency. Keep in sync if the
# indicator formulas there change.
def _ema(data, period):
    k = 2 / (period + 1); e = data[0]
    for v in data[1:]: e = v * k + e * (1 - k)
    return e

def _rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains = losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        d = closes[i] - closes[i - 1]
        if d > 0: gains += d
        else:     losses -= d
    return 100 - 100 / (1 + gains / (losses or 1e-9))

def _macd_bull(closes):
    return (_ema(closes, 12) - _ema(closes, 26)) > 0

def _adx(highs, lows, closes, period=14):
    if len(closes) < period + 2: return 20.0
    n = min(len(closes) - 1, period); sp = sm = st = 0.0
    for i in range(len(closes) - n, len(closes)):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        dp = max(highs[i]-highs[i-1], 0); dm = max(lows[i-1]-lows[i], 0)
        st += tr; sp += dp if dp > dm else 0; sm += dm if dm > dp else 0
    if st == 0: return 0.0
    pip = (sp/st)*100; mim = (sm/st)*100
    return abs(pip-mim)/(pip+mim or 1)*100

def _rvol(volumes):
    if len(volumes) < 21: return 1.0
    avg = sum(volumes[-21:-1]) / 20
    return volumes[-1] / (avg or 1)

def _bb_squeeze(closes, period=20):
    if len(closes) < period: return False
    sl = closes[-period:]; avg = sum(sl)/period
    std = (sum((v-avg)**2 for v in sl)/period)**0.5
    return (2*std)/avg < 0.05


def extract_features(closes, volumes, highs, lows, price):
    """Build the exact feature dict the model expects, from raw OHLCV lists."""
    e9, e20, e50 = _ema(closes, 9), _ema(closes, 20), _ema(closes, 50)
    return {
        "adx":       _adx(highs, lows, closes),
        "rsi":       _rsi(closes),
        "rvol":      _rvol(volumes),
        "macd_bull": 1.0 if _macd_bull(closes) else 0.0,
        "squeeze":   1.0 if _bb_squeeze(closes) else 0.0,
        "pct20":     (price - e20) / e20 * 100,
        "above_e9":  1.0 if price > e9 else 0.0,
        "above_e20": 1.0 if price > e20 else 0.0,
        "above_e50": 1.0 if price > e50 else 0.0,
    }


# ── dataset ─────────────────────────────────────────────────────────────────
def build_dataset(tickers, period="1y", horizon=5, threshold=0.02, window=65, stride=3):
    """
    Walk each ticker's history day-by-day, computing features as of day i from
    only the trailing `window` bars (mirrors momentum_screener.fetch()'s 65d
    window — no lookahead), and labeling with the forward return over `horizon`
    trading days. This backtests the *feature computation*, not live trades,
    to bootstrap a training set without waiting on paper-trading history.
    """
    rows = []
    for ticker in tickers:
        try:
            raw = yf.download(ticker, period=period, interval="1d",
                               progress=False, auto_adjust=True)
        except Exception as e:
            print(f"  ! {ticker}: fetch failed — {e}")
            continue
        if raw.empty or len(raw) < window + horizon + 1:
            print(f"  ! {ticker}: only {len(raw)} bars — skipping")
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        closes  = raw["Close"].tolist()
        volumes = raw["Volume"].tolist()
        highs   = raw["High"].tolist()
        lows    = raw["Low"].tolist()
        dates   = raw.index.tolist()

        n_rows = 0
        for i in range(window, len(closes) - horizon, stride):
            c = closes[i - window + 1: i + 1]
            v = volumes[i - window + 1: i + 1]
            h = highs[i - window + 1: i + 1]
            l = lows[i - window + 1: i + 1]
            price = c[-1]
            feats = extract_features(c, v, h, l, price)
            fwd_ret = (closes[i + horizon] - price) / price
            feats.update(label=int(fwd_ret > threshold), ticker=ticker,
                         date=dates[i], fwd_ret=fwd_ret)
            rows.append(feats)
            n_rows += 1
        print(f"  ✓ {ticker}: {n_rows} samples")

    return pd.DataFrame(rows)


# ── train / evaluate ────────────────────────────────────────────────────────
def train(tickers=None, period="1y", horizon=5, threshold=0.02, stride=3,
          model_path=MODEL_PATH):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import roc_auc_score, accuracy_score
    import joblib

    tickers = tickers or DEFAULT_UNIVERSE
    print(f"Building dataset from {len(tickers)} tickers "
          f"(period={period}, horizon={horizon}d, threshold={threshold:+.1%})...")
    df = build_dataset(tickers, period=period, horizon=horizon,
                        threshold=threshold, stride=stride)
    df = df.dropna(subset=FEATURE_COLUMNS + ["label"])
    if len(df) < 50:
        print(f"Only {len(df)} usable rows — need at least 50. Widen --period or --tickers.")
        return None

    df = df.sort_values("date")
    cutoff = df["date"].quantile(0.8)          # time-based split — no shuffling across time
    train_df, test_df = df[df["date"] <= cutoff], df[df["date"] > cutoff]

    X_train, y_train = train_df[FEATURE_COLUMNS], train_df["label"]
    X_test,  y_test  = test_df[FEATURE_COLUMNS],  test_df["label"]

    pipeline = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(max_iter=1000, class_weight="balanced")),
    ])
    pipeline.fit(X_train, y_train)

    base_rate = y_train.mean()
    print(f"\nRows: {len(df)}  (train {len(train_df)} / test {len(test_df)}, split @ {cutoff.date()})")
    print(f"Base win rate (train): {base_rate:.1%}")

    if len(y_test) and y_test.nunique() > 1:
        proba = pipeline.predict_proba(X_test)[:, 1]
        preds = (proba >= 0.5).astype(int)
        print(f"Holdout AUC:      {roc_auc_score(y_test, proba):.3f}  (0.5 = coin flip)")
        print(f"Holdout accuracy: {accuracy_score(y_test, preds):.1%}  "
              f"(vs {max(base_rate, 1-base_rate):.1%} if always predicting the majority class)")
    else:
        print("Holdout set too small/one-sided to score — trained anyway, but treat with caution.")

    for feat, coef in zip(FEATURE_COLUMNS, pipeline.named_steps["clf"].coef_[0]):
        print(f"  {feat:<10} coef {coef:+.3f}")

    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "pipeline":   pipeline,
        "features":   FEATURE_COLUMNS,
        "horizon":    horizon,
        "threshold":  threshold,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "n_rows":     len(df),
        "tickers":    tickers,
    }, model_path)
    print(f"\nSaved model → {model_path}")
    return pipeline


def _load_model():
    global _model_cache
    if _model_cache is not None:
        return _model_cache
    if not MODEL_PATH.exists():
        return None
    try:
        import joblib
        _model_cache = joblib.load(MODEL_PATH)
    except Exception as e:
        print(f"  ! ml_model: failed to load {MODEL_PATH} — {e}")
        return None
    return _model_cache


def predict_proba(features: dict):
    """
    features: dict with exactly FEATURE_COLUMNS keys (see extract_features()).
    Returns a float win-probability in [0,1], or None if no model is trained
    or inference fails for any reason — callers must treat None as "skip".
    """
    model = _load_model()
    if model is None:
        return None
    try:
        row = [[features[c] for c in model["features"]]]
        return float(model["pipeline"].predict_proba(row)[0][1])
    except Exception:
        return None


def main():
    p = argparse.ArgumentParser(description="Train/inspect the momentum win-probability model")
    p.add_argument("--train", action="store_true", help="build dataset + train + save model")
    p.add_argument("--info", action="store_true", help="show metadata for the saved model")
    p.add_argument("--tickers", help="comma-separated tickers (default: built-in universe)")
    p.add_argument("--period", default="1y", help="yfinance history window, e.g. 1y, 2y")
    p.add_argument("--horizon", type=int, default=5, help="forward-looking days for the label")
    p.add_argument("--threshold", type=float, default=0.02, help="forward return counted as a win")
    p.add_argument("--stride", type=int, default=3, help="days between sampled rows per ticker")
    args = p.parse_args()

    if args.info:
        model = _load_model()
        if model is None:
            print(f"No trained model at {MODEL_PATH}")
        else:
            print(f"Trained:   {model['trained_at']}")
            print(f"Rows:      {model['n_rows']}")
            print(f"Horizon:   {model['horizon']}d   Threshold: {model['threshold']:+.1%}")
            print(f"Tickers:   {len(model['tickers'])}")
        return

    if args.train:
        tickers = args.tickers.split(",") if args.tickers else None
        train(tickers=tickers, period=args.period, horizon=args.horizon,
              threshold=args.threshold, stride=args.stride)
        return

    p.print_help()


if __name__ == "__main__":
    main()
