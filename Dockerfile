FROM python:3.12-slim

WORKDIR /app

# Install system tools for agent use
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client curl wget \
    git jq tree ripgrep \
    sqlite3 && \
    rm -rf /var/lib/apt/lists/*

# Create workspace directory
RUN mkdir -p /tmp/nous-workspace

# Copy source BEFORE pip install (F5: non-editable install)
COPY pyproject.toml .
COPY nous/ nous/
RUN pip install --no-cache-dir ".[runtime]"

COPY sql/ sql/
COPY static/ static/

HEALTHCHECK --interval=30s --timeout=10s \
  CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["python", "-m", "nous.main"]
