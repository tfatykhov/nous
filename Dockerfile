FROM python:3.12-slim

WORKDIR /app

# Install system tools, build deps, and agent runtime packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client curl wget \
    git jq tree ripgrep \
    sqlite3 \
    gcc python3-dev pkg-config libfreetype6-dev \
    pandoc \
    graphviz \
    nodejs npm \
    ffmpeg \
    tesseract-ocr \
    redis-tools \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI (F054: Claude Code Job Runner)
RUN npm install -g @anthropic-ai/claude-code

# Create workspace and claude-jobs directories
RUN mkdir -p /tmp/nous-workspace /workspace/claude-jobs

# Copy source BEFORE pip install (F5: non-editable install)
COPY pyproject.toml .
COPY nous/ nous/
RUN pip install --no-cache-dir ".[runtime,agent]"

COPY sql/ sql/
COPY static/ static/

HEALTHCHECK --interval=30s --timeout=10s \
  CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["python", "-m", "nous.main"]
