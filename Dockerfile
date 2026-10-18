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
    git make jq tree ripgrep \
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

# Headless Chromium for Playwright (the agent's web research and web-app testing). The browser and its
# system libraries are baked into the image, so they survive a rebuild, and the directory is readable by
# every user, the job user included. This runs before the source is copied, so the layer is rebuilt only
# when pyproject.toml changes; the version comes from the agent extra's playwright pin, which also fixes
# the Chromium revision.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN pip install --no-cache-dir "$(python -c "import tomllib; print(next(d for d in tomllib.load(open('pyproject.toml', 'rb'))['project']['optional-dependencies']['agent'] if d.startswith('playwright')))")" \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /opt/ms-playwright

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

# Claude Code plugins, pre-installed into a read-only seed: a run registers the seed's marketplaces
# and loads its plugins with no download. Which ones a run loads is settings.json's enabledPlugins
# (in CLAUDE_CONFIG_DIR, on the workspace volume). Rebuilt each build, so plugins refresh per deploy.
ENV CLAUDE_CODE_PLUGIN_SEED_DIR=/opt/claude-plugins-seed
RUN export CLAUDE_CODE_PLUGIN_CACHE_DIR=/opt/claude-plugins-seed CLAUDE_CONFIG_DIR=/tmp/claude-seed-build \
    && claude plugin marketplace add anthropics/claude-plugins-official \
    && claude plugin marketplace add forrestchang/andrej-karpathy-skills \
    && claude plugin marketplace add tfatykhov/cognition-engines-marketplace \
    && for p in superpowers code-review feature-dev claude-md-management pr-review-toolkit \
         code-simplifier frontend-design; do \
         claude plugin install "$p@claude-plugins-official" || exit 1; \
       done \
    && claude plugin install andrej-karpathy-skills@karpathy-skills \
    && claude plugin install forge@cognition-engines \
    && rm -rf /tmp/claude-seed-build \
    && chmod -R a+rX /opt/claude-plugins-seed

RUN useradd --create-home --shell /bin/bash claude-runner \
    && chown -R claude-runner:claude-runner /tmp/nous-workspace \
    && chmod -R a+rX "$(npm root -g)" \
    && echo "claude-runner ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

# browser_task (F100) runs Chromium as nous-browser: a system user with no sudo, no login shell and no
# groups beyond its own, so a renderer exploit does not get root in the container that holds the
# database and API credentials. nous/browser/chromium-as-user.sh drops to it with setpriv; its home only
# holds Chromium's caches.
# iptables: the kernel egress rule that confines nous-browser to the local egress proxy
# (nous/browser/firewall.py; needs cap_add NET_ADMIN at run time).
RUN apt-get update && apt-get install -y --no-install-recommends iptables \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --create-home --home-dir /home/nous-browser --shell /usr/sbin/nologin nous-browser \
    && chmod 0755 /app/nous/browser/chromium-as-user.sh

HEALTHCHECK --interval=30s --timeout=10s \
  CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["python", "-m", "nous.main"]
