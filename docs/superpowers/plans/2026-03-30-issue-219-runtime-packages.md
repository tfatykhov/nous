# Issue #219: Agent Runtime Packages — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add commonly-needed Python and OS packages to the Nous agent Docker runtime, enabling data analysis, document generation, visualization, and other capabilities.

**Architecture:** Two files change — `pyproject.toml` gets a new `agent` optional-dependencies group for Python packages, and `Dockerfile` gets additional `apt-get` packages. The Dockerfile install command changes from `pip install .[runtime]` to `pip install .[runtime,agent]` to pick up both groups.

**Notes:**
- `pytest` is intentionally in `agent` (not just `dev`) because the agent needs to run Nous test suites as part of task execution inside the container.
- `aiohttp` is for agent-executed task code only (e.g. parallel fetches in skills). Nous internals continue to use `httpx`.
- matplotlib ships pre-compiled manylinux wheels on amd64. Build deps (`gcc`, `python3-dev`) are added to the Dockerfile as a safety net for arm64/other platforms.
- `python-pptx>=1.0` uses the v1.0 API. No existing Nous code uses the old 0.6.x API, so this is safe.

**Tech Stack:** Python packaging (pyproject.toml extras), Debian apt packages, Docker

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `pyproject.toml` | Modify | Add `agent` optional-dependencies group with P1+P2 Python packages |
| `Dockerfile` | Modify | Add OS packages (P1+P2+P3), install `.[runtime,agent]` |

## Package Inventory

### Python packages (`pyproject.toml` → `[project.optional-dependencies] agent`)

| Package | Priority | Purpose | Version Range |
|---------|----------|---------|---------------|
| `pandas` | P1 | Data analysis, CSV processing | `>=2.0,<3.0` |
| `matplotlib` | P1 | Chart/visualization generation | `>=3.8,<4.0` |
| `tabulate` | P1 | Formatted table output | `>=0.9,<1.0` |
| `python-docx` | P1 | Word document read/create | `>=1.0,<2.0` |
| `python-pptx` | P1 | PowerPoint read/create | `>=1.0,<2.0` |
| `openpyxl` | P1 | Excel read/create | `>=3.1,<4.0` |
| `Pillow` | P1 | Image processing | `>=10.0,<12.0` |
| `beautifulsoup4` | P1 | HTML parsing | `>=4.12,<5.0` |
| `pytest` | P1 | Run tests in agent runtime | `>=8.0` |
| `graphviz` | P2 | DAG/graph visualization (Python binding) | `>=0.20,<1.0` |
| `aiohttp` | P2 | Async HTTP for parallel fetches | `>=3.9,<4.0` |
| `xlrd` | P2 | Legacy Excel format support | `>=2.0,<3.0` |

### OS packages (`Dockerfile` → `apt-get install`)

| Package | Priority | Purpose |
|---------|----------|---------|
| `pandoc` | P1 | Document format conversion |
| `graphviz` | P2 | Graph/DAG rendering engine |
| `nodejs` + `npm` | P2 | JS runtime, build frontends |
| `ffmpeg` | P3 | Media processing |
| `tesseract-ocr` | P3 | OCR from images |
| `redis-tools` | P3 | Direct cache inspection |

---

### Task 1: Add Python `agent` extras group to pyproject.toml

**Files:**
- Modify: `pyproject.toml:19-29`

- [ ] **Step 1: Add the `agent` optional-dependencies group**

Add after the existing `runtime` group, before `dev`:

```toml
agent = [
    "pandas>=2.0,<3.0",
    "matplotlib>=3.8,<4.0",
    "tabulate>=0.9,<1.0",
    "python-docx>=1.0,<2.0",
    "python-pptx>=1.0,<2.0",
    "openpyxl>=3.1,<4.0",
    "Pillow>=10.0,<12.0",
    "beautifulsoup4>=4.12,<5.0",
    "pytest>=8.0",
    "graphviz>=0.20,<1.0",
    "aiohttp>=3.9,<4.0",
    "xlrd>=2.0,<3.0",
]
```

- [ ] **Step 2: Verify pyproject.toml parses correctly**

Run: `cd /e/Projects/nous && python -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); print(d['project']['optional-dependencies']['agent'])"`
Expected: List of agent dependencies printed without errors

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "feat: add agent optional-dependencies group with P1+P2 Python packages (#219)"
```

---

### Task 2: Add OS packages and install agent extras in Dockerfile

**Files:**
- Modify: `Dockerfile:6-10` (apt-get block)
- Modify: `Dockerfile:18` (pip install line)

- [ ] **Step 1: Expand the apt-get install block with P1/P2/P3 OS packages**

Replace the **entire** existing `RUN apt-get update && apt-get install ...` block (lines 6-10) wholesale with:

```dockerfile
# Install system tools for agent use + build deps for native Python extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Base tools
    postgresql-client curl wget \
    git jq tree ripgrep \
    sqlite3 \
    # Build deps (matplotlib C extensions on non-amd64)
    gcc python3-dev pkg-config libfreetype6-dev \
    # P1: Document conversion
    pandoc \
    # P2: Graph rendering, JS runtime
    graphviz \
    nodejs npm \
    # P3: Media, OCR, cache
    ffmpeg \
    tesseract-ocr \
    redis-tools \
    && rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 2: Change pip install to include agent extras**

Change line 18 from:
```dockerfile
RUN pip install --no-cache-dir ".[runtime]"
```
To:
```dockerfile
RUN pip install --no-cache-dir ".[runtime,agent]"
```

- [ ] **Step 3: Verify Dockerfile syntax**

Run: `docker build --check -f Dockerfile . 2>&1 || echo "No --check support, syntax looks fine if no parse errors"`
Or just visually inspect — Dockerfile syntax is simple.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
git commit -m "feat: add OS packages and agent extras to Docker runtime (#219)"
```

---

### Task 3: Docker build verification

- [ ] **Step 1: Build Docker image (no cache)**

Run: `docker build --no-cache -t nous:test-219 .`
Expected: Build completes successfully. All apt packages install, all pip packages resolve.

- [ ] **Step 2: Verify Python packages are importable**

Run: `docker run --rm nous:test-219 python -c "import pandas, matplotlib, tabulate, docx, pptx, openpyxl, PIL, bs4, graphviz, aiohttp, xlrd; print('All packages OK')"`
Expected: `All packages OK`

- [ ] **Step 3: Verify OS tools are available**

Run: `docker run --rm nous:test-219 bash -c "pandoc --version | head -1 && dot -V && node --version && ffmpeg -version 2>&1 | head -1 && tesseract --version 2>&1 | head -1 && redis-cli --version"`
Expected: Version output for each tool, no "command not found"

- [ ] **Step 4: Verify existing functionality still works**

Run: `docker run --rm nous:test-219 python -c "import nous; print('Nous imports OK')"`
Expected: `Nous imports OK`
