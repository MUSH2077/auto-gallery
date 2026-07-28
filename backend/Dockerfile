ARG PYTHON_BASE=python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
FROM ${PYTHON_BASE}

WORKDIR /app

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && \
    apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 update && \
    apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
    ffmpeg \
    libvips-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
ARG PIP_INDEX_URL
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      pip install --no-cache-dir --require-hashes -i "$PIP_INDEX_URL" -r requirements.txt; \
    else \
      pip install --no-cache-dir --require-hashes -r requirements.txt; \
    fi

# Patch gallery-dl twitter extractor: disable SearchTimeline fallback (query ID broken as of 2025)
# After UserTweets pagination completes, return instead of falling back to broken search endpoint.
RUN TWITTER_PY=$(python3 -c "import gallery_dl.extractor.twitter; print(gallery_dl.extractor.twitter.__file__)") && \
    sed -i 's/^            state = reset = 2$/            return/' "$TWITTER_PY" && \
    echo "Patched gallery-dl twitter.py: removed SearchTimeline fallback"

# ── Application source ────────────────────────────────────────────
# CACHEBUST ensures Docker always copies the latest source code.
# Without it, Docker may reuse a stale COPY layer even when files changed.
ARG CACHEBUST=1
COPY . .

RUN mkdir -p /downloads /library /gallerydl-config /app-config

# Memory-conserving Python flags for NAS (7.5 GB RAM):
# - PYTHONMALLOC=malloc: use system malloc (lower RSS than pymalloc for multi-process)
# - PYTHONHASHSEED=0: disable hash randomization (saves a few KB per process)
ENV PYTHONMALLOC=malloc \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
