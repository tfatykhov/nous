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
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Create workspace directory
RUN mkdir -p /tmp/nous-workspace

# Copy source BEFORE pip install (F5: non-editable install)
COPY pyproject.toml .
COPY nous/ nous/
RUN pip install --no-cache-dir ".[runtime,agent]"

COPY sql/ sql/
COPY static/ static/

# Install Claude Code globally (npm), then create claude-runner user with access to it.
# runner.sh runs Claude Code as claude-runner to bypass root restriction on
# --dangerously-skip-permissions.
RUN npm install -g @anthropic-ai/claude-code

RUN useradd --create-home --shell /bin/bash claude-runner \
    && chown -R claude-runner:claude-runner /tmp/nous-workspace \
    && chmod -R a+rX "$(npm root -g)" \
    && echo "claude-runner ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

HEALTHCHECK --interval=30s --timeout=10s \
  CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["python", "-m", "nous.main"]
