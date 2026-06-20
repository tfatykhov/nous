# --- Dashboard build stage (Svelte v2) ---
FROM node:22-slim AS dashboard
WORKDIR /build
COPY dashboard-app/ ./dashboard-app/
RUN cd dashboard-app && npm ci && npm run build
# vite.config.ts outDir is ../static/dashboard-v2/dist => /build/static/dashboard-v2/dist

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

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Create workspace directory
RUN mkdir -p /tmp/nous-workspace

# Copy source BEFORE pip install (F5: non-editable install)
COPY pyproject.toml .
COPY nous/ nous/
# F042: [rerank] pulls sentence-transformers + torch (~2GB). The model
# itself (~90MB) is downloaded on first use into $HF_HOME and persisted
# via the huggingface_cache volume defined in docker-compose.yml.
RUN pip install --no-cache-dir ".[runtime,agent,rerank]"

COPY sql/ sql/
COPY static/ static/
# Built Svelte dashboard (from the dashboard build stage above)
COPY --from=dashboard /build/static/dashboard-v2/dist ./static/dashboard-v2/dist

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
