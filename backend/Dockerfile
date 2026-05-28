FROM python:3.12.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libvips-dev \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
ARG PIP_INDEX_URL
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY . .

RUN mkdir -p /downloads /library /gallerydl-config /app-config

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
