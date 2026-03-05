# Exa Search — Skill Reference

## Overview
Exa is a neural/semantic web search API. Used as fallback when Brave hits rate limits, and directly for deep research, structured data extraction, category-specific searches, and Q&A with citations.

**Free tier:** 1,000 queries/month
**API key env:** `EXA_API_KEY`
**Docs:** https://docs.exa.ai

## Internal Functions

### `_exa_search()` — Main search
```python
await _exa_search(
    query="your query",
    count=10,                          # max results
    search_type="auto",                # "auto" | "fast" | "deep" | "deep-reasoning"
    content_mode="text",               # "text" (full) | "highlights" (excerpts)
    max_text_chars=3000,               # content character limit
    include_domains=["arxiv.org"],     # optional domain filter
    exclude_domains=["pinterest.com"], # optional domain exclusion
    output_schema={...},               # structured output (deep/deep-reasoning only)
    category="news",                   # optional category index
    max_age_hours=24,                  # optional freshness control
    _settings=settings,
    _http=http_client,
)
```

### `_exa_get_contents()` — Extract content from known URLs
```python
await _exa_get_contents(
    urls=["https://example.com/article"],
    content_mode="text",               # "text" | "highlights"
    max_text_chars=3000,
    _settings=settings,
    _http=http_client,
)
```

### `_exa_answer()` — Q&A with citations
```python
await _exa_answer(
    query="What is the current state of AI safety research?",
    _settings=settings,
    _http=http_client,
)
```
Returns a synthesized answer with source citations. Best for factual questions.

## Search Types

| Type | Speed | Best For |
|------|-------|----------|
| `fast` | Fastest | Real-time apps, quick lookups |
| `auto` | Medium | Most queries — balanced relevance & speed |
| `deep` | 4-12s | Research, enrichment, thorough results |
| `deep-reasoning` | Slowest | Complex multi-step reasoning tasks |

**Tip:** `deep` and `deep-reasoning` support structured outputs via `output_schema`.

## Content Modes

| Mode | Config | Best For |
|------|--------|----------|
| `text` | Full page content (up to 20K chars) | RAG, full articles, code docs |
| `highlights` | Key excerpts (up to 4K chars) | Summaries, Q&A, general research |

**Warning:** `text` mode with high `max_text_chars` increases token consumption significantly. Use `highlights` when full content isn't needed.

## Category Indexes

Search dedicated content indexes. Each returns only that content type.
**Omit category for general web search.** Categories can be restrictive — if results are sparse, try without.

### `"people"` — Find people by role/expertise
```python
_exa_search("software engineer distributed systems", category="people")
```
- Use SINGULAR form ("engineer" not "engineers")
- Describe what they work on
- No date/text filters supported

### `"company"` — Find companies by industry/attributes
```python
_exa_search("AI startup healthcare", category="company")
```
- Use SINGULAR form
- Simple entity queries
- Returns company objects, not articles

### `"news"` — News articles
```python
_exa_search("OpenAI announcements", category="news", max_age_hours=24)
```
- Use `max_age_hours=0` for breaking news (forces livecrawl)

### `"research paper"` — Academic papers
```python
_exa_search("transformer architecture improvements", category="research paper")
```
- Includes arxiv.org, paperswithcode.com, and academic sources
- Use `type="auto"` for most queries

### `"tweet"` — Twitter/X posts
```python
_exa_search("AI safety discussion", category="tweet")
```
- Good for real-time discussions and public sentiment

## Content Freshness (`max_age_hours`)

Controls livecrawl behavior for cached content:

| Value | Behavior | Best For |
|-------|----------|----------|
| `24` | Livecrawl if cache > 24h old | Daily-fresh content |
| `1` | Livecrawl if cache > 1h old | Near real-time data |
| `0` | Always livecrawl (ignore cache) | Real-time, breaking news |
| `-1` | Never livecrawl (cache only) | Max speed, historical/static content |
| `None` (omit) | Default — livecrawl as fallback | Recommended for most queries |

**Note:** Livecrawl isn't necessary for historical/educational queries where cached data is sufficient.

## Domain Filtering

```python
_exa_search(
    "query",
    include_domains=["vercel.com"],
    exclude_domains=["community.vercel.com"],  # exclude subdomain of included domain
)
```
- `includeDomains` and `excludeDomains` CAN be used together
- Common pattern: include broad domain, exclude specific subdomain
- Usually not needed — Exa's neural search finds relevant results without filtering

## Structured Outputs (Deep Search)

Only available with `deep` and `deep-reasoning` search types.

```python
result = await _exa_search(
    "articles about GPUs",
    search_type="deep",
    output_schema={
        "type": "object",
        "description": "Companies mentioned in articles",
        "required": ["companies"],
        "properties": {
            "companies": {
                "type": "array",
                "description": "List of companies mentioned",
                "items": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string", "description": "Name of the company"},
                        "description": {"type": "string", "description": "Short description"}
                    }
                }
            }
        }
    },
    content_mode="highlights",
)
```

**Response includes:**
- `output.content` — structured JSON matching your schema
- `output.grounding` — field-level citations with source URLs and confidence scores

**Schema controls:** `type`, `description`, `required`, `properties`, `items`

**When to use:**
- Enrichment workflows (company info, people data, product details)
- Data pipelines (structured data without parsing free text)
- Grounded facts (every field comes with citations and confidence)

## Fallback Chain

Brave (primary) -> Exa (automatic fallback on rate limit, HTTP error, timeout, or missing key)

The fallback is transparent — `web_search` tool tries Brave first, falls back to Exa automatically.

## MCP Server (for Claude Code)

```bash
claude mcp add --transport http exa https://mcp.exa.ai/mcp?exaApiKey=YOUR_KEY
```

Enable all tools:
```
https://mcp.exa.ai/mcp?exaApiKey=YOUR_KEY&tools=web_search_exa,web_search_advanced_exa,get_code_context_exa,crawling_exa,company_research_exa,people_search_exa,deep_researcher_start,deep_researcher_check
```

## Troubleshooting

**Need structured data from search?**
1. Use `type="deep"` or `type="deep-reasoning"` with `output_schema`
2. Define the fields you need — Exa returns grounded JSON with field-level citations

**Results not relevant?**
1. Try `type="auto"` — most balanced, has fallback mechanisms
2. Try `type="deep"` — runs multiple query variations and ranks combined results
3. Refine query — use singular form, be specific
4. Check category matches your use case

**Results too slow?**
1. Use `type="fast"`
2. Reduce `num_results`
3. Skip contents if you only need URLs

**No results?**
1. Remove filters (date, domain restrictions)
2. Simplify query
3. Try `type="auto"` — has fallback mechanisms

**High token cost?**
- Switch from `text` to `highlights` mode
- Reduce `max_text_chars`

## Resources

- **Docs:** https://exa.ai/docs
- **Dashboard:** https://dashboard.exa.ai
- **API Status:** https://status.exa.ai
