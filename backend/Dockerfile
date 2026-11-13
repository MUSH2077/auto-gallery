ARG PYTHON_BASE=python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
FROM ${PYTHON_BASE} AS runtime-base
WORKDIR /app

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && \
    apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 update && \
    apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
    ca-certificates \
    curl \
    && install -d -m 0755 /usr/share/postgresql-common/pgdg \
    && curl --fail --silent --show-error --location \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      --output /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo '0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76  /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc' | sha256sum --check --strict \
    && printf '%s\n' \
      'Types: deb' \
      'URIs: https://apt.postgresql.org/pub/repos/apt' \
      'Suites: bookworm-pgdg' \
      'Components: main' \
      'Signed-By: /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc' \
      > /etc/apt/sources.list.d/pgdg.sources \
    && apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
    ffmpeg \
    libssl3 \
    openssl \
    libpcre2-8-0 \
    liblzma5 \
    libvips42 \
    libmagickwand-6.q16-6 \
    libmagickcore-6.q16-6-extra \
    postgresql-client-16 \
    && pg_dump --version | grep -Eq '^pg_dump \(PostgreSQL\) 16\.' \
    && rm -rf /var/lib/apt/lists/*

FROM runtime-base AS builder
RUN apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=5 -o Acquire::https::Timeout=30 install -y --no-install-recommends gcc libvips-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-build.txt requirements.txt ./
ARG PIP_INDEX_URL
RUN if [ -n "$PIP_INDEX_URL" ]; then export PIP_INDEX_URL; fi; \
    pip install --no-cache-dir --require-hashes -r requirements-build.txt \
    && pip wheel --no-cache-dir --no-build-isolation --no-deps --require-hashes -r requirements.txt --wheel-dir /wheels
COPY pyproject.toml ./
COPY gitllery_cli/ gitllery_cli/
COPY gitllery_format/ gitllery_format/
RUN pip wheel --no-cache-dir --no-build-isolation --no-deps . --wheel-dir /wheels

FROM runtime-base AS runtime
ARG BUILD_REVISION=development
LABEL org.opencontainers.image.revision="${BUILD_REVISION}"
COPY requirements.txt ./
# Wheels were built from hash-verified dependencies; source-archive hashes do
# not describe locally compiled wheels. Keep build artifacts out of image layers.
RUN --mount=type=bind,from=builder,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --no-deps /wheels/*.whl

# Keep the deployed gallery-dl Twitter pagination patch.
RUN TWITTER_PY=$(python3 -c "import gallery_dl.extractor.twitter; print(gallery_dl.extractor.twitter.__file__)") \
    && sed -i 's/^            state = reset = 2$/            return/' "$TWITTER_PY"

COPY app/ app/
COPY alembic/ alembic/
COPY scripts/ scripts/
COPY *.py alembic.ini ./
RUN mkdir -p /downloads /library /gallerydl-config /app-config
ENV PYTHONMALLOC=malloc \
    BUILD_REVISION=${BUILD_REVISION} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app
EXPOSE 8000
CMD ["python", "api_entrypoint.py"]

FROM runtime AS test
COPY requirements-dev.txt ./
ARG PIP_INDEX_URL
RUN if [ -n "$PIP_INDEX_URL" ]; then export PIP_INDEX_URL; fi; \
    pip install --no-cache-dir --require-hashes -r requirements-dev.txt
COPY tests/ tests/
COPY pyproject.toml ./

# A normal Docker build must produce the runtime-only image.
FROM runtime AS production
