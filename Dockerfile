# ─────────────────────────────────────────────────────────────────────────────
#  Momentum Screener — single-image build
#  Flask serves both the API and index.html (with api-key injected at runtime)
#
#  Build:  docker build -t momentum-screener .
#  Run:    docker run -p 5000:5000 -e API_KEY=your_key momentum-screener
#
#  On Render: set API_KEY in the Environment tab, port is auto-detected.
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim

# ── system deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── python deps ───────────────────────────────────────────────────────────────
# Copy requirements first so Docker layer-caches the pip install
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ── app files ─────────────────────────────────────────────────────────────────
COPY momentum_screener.py ./
COPY ml_model.py          ./
COPY index.html           ./

# ── ML model artifact ─────────────────────────────────────────────────────────
# models/ is gitignored (trained weights aren't checked in) except for
# .gitkeep, so this COPY always succeeds even on a fresh clone that hasn't
# run `python ml_model.py --train` yet — ml_model.py degrades to
# predict_proba() -> None when momentum_model.joblib is absent.
COPY models/ ./models/

# ── runtime config ────────────────────────────────────────────────────────────
# PORT: Render injects this automatically; default to 5000 for local Docker.
# API_KEY: set via  -e API_KEY=xxx  or Render's Environment tab.
#          If unset the server starts unprotected (dev mode).
ENV PORT=5000
EXPOSE $PORT

# ── healthcheck ───────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:${PORT}/health || exit 1

# ── entrypoint ────────────────────────────────────────────────────────────────
CMD python momentum_screener.py --serve --port ${PORT}