# MCP Subagent Cheat Sheet

> All tools require the word **`subagent`** in your message to activate.

---

## Model Override Syntax

Any tool with a `model` parameter accepts a model key override:

```
subagent analyze with model glm-5.2: <your prompt>
subagent debate with model claude-sonnet-4: <prompt>
subagent search with model gemini-2.5-pro: <query>
```

Say `subagent status` to see all 38 available model keys + their capabilities.

### Model Keys by Provider

| Provider | Keys (fast → powerful) |
|----------|------------------------|
| Google | `gemini-3.1-flash-lite`, `gemma-3-12b`, `gemini-3.5-flash`★, `gemini-2.5-flash`, `gemini-2.5-pro`★★ |
| Anthropic | `claude-haiku-4.5`, `claude-sonnet-4/4.5/4.6/5`, `claude-opus-4.5/4.6/4.7/4.8/5` |
| OpenAI | `gpt-5-nano`, `gpt-5-mini`, `gpt-5.4-nano/mini`, `gpt-5/5.1/5.2/5.3-codex/5.4/5.5/5.5-pro/5.6-luna/sol/terra`, `gpt-oss-20b/120b` |
| Meta | `llama-3.1-8b`, `llama-3.3-70b`, `llama-4-maverick` |
| Zhipu | `glm-5.2` (thinking, fast, low cost) |
| Alibaba | `qwen3-next-80b`, `qwen3.5-122b` |
| Databricks | `inkling` |

★ = default for all fast roles &nbsp; ★★ = default for `cascade_powerful`

---

## The 15 Tools (Quick Reference)

| # | Tool | Type | Purpose |
|---|------|------|---------|
| 1 | `status` | sync | Health check, model list, routing defaults |
| 2 | `analyze` | sync | General text/reasoning |
| 3 | `vision` | sync | Image analysis |
| 4 | `fan_out` | sync | Parallel prompts (up to 10, same model) |
| 5 | `structured_extract` | sync | JSON extraction from text |
| 6 | `inspect_page` | async | Screenshot + a11y tree of URL |
| 7 | `browser_task` | async | Goal-driven browser automation |
| 8 | `render_html` | async | Render HTML → screenshot |
| 9 | `debate` | async | Generate → critique → refine (multi-model) |
| 10 | `research` | async | Multi-page web research + synthesis |
| 11 | `crawl` | async | Multi-page extract/journey/audit |
| 12 | `compare` | sync | Visual or text diff |
| 13 | `fetch` | sync | Raw HTTP request (no LLM) |
| 14 | `search` | async | Web search via SearchBroker |
| 15 | `get_task_result` | sync | Poll async task results |

---

## Full Modifier Reference

Every modifier below can be specified in natural language after the tool name.

### `analyze`

| Modifier | Values | Default | Example |
|----------|--------|---------|--------|
| `model` | any model key | `gemini-3.5-flash` | `with model glm-5.2` |
| `system_prompt` | any string | (none) | `with system_prompt "You are a security auditor"` |
| `max_tokens` | 256–131072 | 4096 | `with max_tokens 16000` |

```
subagent analyze: <prompt>
subagent analyze with model claude-sonnet-4 and max_tokens 8000: <prompt>
subagent analyze with system_prompt "Be concise, use bullet points": <prompt>
```

---

### `vision`

| Modifier | Values | Default | Example |
|----------|--------|---------|--------|
| `model` | vision-capable key | `gemini-3.5-flash` | `with model gpt-5.4` |
| `mime_type` | `image/png`, `image/jpeg`, `image/webp`, `image/gif` | `image/png` | `with mime_type image/jpeg` |
| `system_prompt` | any string | (none) | `with system_prompt "Focus on data flows"` |
| `max_tokens` | 256–131072 | 4096 | `with max_tokens 8000` |

**Vision-capable:** all Google, all Anthropic, all OpenAI (except oss-*), llama-4-maverick.  
**NOT vision:** glm-5.2, qwen*, llama-3.x, inkling, gemma.

---

### `fan_out`

| Modifier | Values | Default | Example |
|----------|--------|---------|--------|
| `model` | any model key | `gemini-3.5-flash` | `with model claude-haiku-4.5` |
| `system_prompt` | any string | (none) | `with system_prompt "Extract named entities"` |
| `max_tokens` | 256–131072 | 4096 | `with max_tokens 2000` |

**Note:** One model for ALL prompts. For multi-model, use separate `analyze` calls.

```
subagent fan_out: ["Summarize A", "Summarize B", "Summarize C"]
subagent fan_out with model glm-5.2 and max_tokens 8000: ["Analyze X", "Analyze Y"]
```

---

### `structured_extract`

| Modifier | Values | Default | Example |
|----------|--------|---------|--------|
| `model` | any model key | `gemini-3.5-flash` | `with model gpt-5.4-mini` |
| `extraction_prompt` | extra instructions | (none) | `with extraction_prompt "Dates in ISO format"` |
| `max_tokens` | 256–131072 | 4096 | `with max_tokens 8000` |

```
subagent structured_extract from <text>, schema: {"people": [{"name": "str", "role": "str"}]}
```

---

### `inspect_page`

| Modifier | Values | Default | Example |
|----------|--------|---------|--------|
| `model` | any model key | `gemini-3.5-flash` | `with model claude-haiku-4.5` |
| `question` | specific focus | (none) | `with question "what CTAs are visible"` |

```
subagent inspect_page: https://example.com
subagent inspect_page with question "list all form fields": https://app.example.com/signup
```

---

### `browser_task`

| Modifier | Values | Default | Example |
|----------|--------|---------|--------|
| `model` | any model key | `gemini-3.5-flash` | `with model gemini-2.5-pro` |
| `max_rounds` | 1–5 | 3 | `with max_rounds 5` |

Higher `max_rounds` = more attempts at complex multi-step interactions.

```
subagent browser_task: go to https://pypi.org and find the latest mlflow version
subagent browser_task with max_rounds 5: complete the checkout flow on https://shop.example.com
```

---

### `render_html`

| Modifier | Values | Default | Example |
|----------|--------|---------|--------|
| `model` | any model key | `gemini-3.5-flash` | `with model claude-haiku-4.5` |
| `viewport_width` | pixels | 1280 | `with viewport_width 1920` |
| `viewport_height` | pixels | 900 | `with viewport_height 1080` |
| `wait_ms` | milliseconds | 500 | `with wait_ms 2000` |
| `summarize` | true/false | true | `with summarize false` |

```
subagent render_html: <html><body>...</body></html>
subagent render_html with viewport_width 390 and viewport_height 844: <mobile layout html>
```

---

### `debate`

| Modifier | Values | Default | Example |
|----------|--------|---------|--------|
| `model` | fallback for both roles | `gemini-3.5-flash` | `with model claude-sonnet-4` |
| `generator_model` | override generator only | (falls back to `model`) | `with generator_model glm-5.2` |
| `critic_model` | override critic only | (falls back to `model`) | `with critic_model claude-haiku-4.5` |
| `system_prompt` | instructions for generator | (none) | `with system_prompt "Argue from first principles"` |
| `max_tokens` | per-stage budget | 8192 | `with max_tokens 16000` |

**Stages:** Generate (model A) → Critique (model B) → Conditional Refine (model A).

```
subagent debate: Should we use event sourcing or CRUD?
subagent debate with generator_model glm-5.2 and critic_model claude-sonnet-4 and max_tokens 12000: <proposal>
```

---

### `research` ⭐

| Modifier | Values | Default | Effect |
|----------|--------|---------|--------|
| `model` | any model key | `gemini-3.5-flash` | Synthesis/planning model |
| `depth` | `quick`, `standard`, `deep` | `standard` | Controls search breadth |
| `max_searches` | 1–6 | 4 | Hard cap on search queries |
| `max_pages` | 1–10 | 5 | Pages to read/extract |
| `freshness` | `24h`, `7d`, `30d`, `1y`, null | null (any) | Recency filter |
| `include_domains` | array of domains | (none) | Restrict to these sites |
| `start_urls` | array of URLs | (none) | Seed URLs to read first |

**Depth presets:**
- `quick` — 1-2 searches, fast scan, minimal synthesis
- `standard` — 3-4 searches, moderate page reads, verified claims
- `deep` — 5-6 searches, broad fan-out strategy, thorough verification

**Search engine:** SearchBroker (DDG → Brave progressive escalation). NOT browser.

```
subagent research: best practices for MLflow model deployment 2026
subagent research with depth deep: compare Databricks vs Snowflake serverless pricing
subagent research with depth quick and freshness 7d: latest Gemini API changes
subagent research with depth deep and max_searches 6 and include_domains ["arxiv.org", "paperswithcode.com"]: neural radiance fields for urban reconstruction
subagent research with model gemini-2.5-pro and max_pages 8: <complex topic needing powerful synthesis>
subagent research with start_urls ["https://some-doc.com/guide"]: expand on this source
```

---

### `crawl`

| Modifier | Values | Default | Effect |
|----------|--------|---------|--------|
| `model` | any model key | `gemini-3.5-flash` | Extraction/summarization model |
| `mode` | `extract`, `journey`, `audit` | `extract` | Crawl strategy |
| `extract_schema` | what to pull | (none) | Fields to extract per page |
| `link_pattern` | regex/keyword | (none) | Filter which links to follow |
| `steps` | array of actions | (none) | Journey mode step sequence |
| `max_pages` | 1–20 | 10 | Pages to visit |
| `same_origin` | true/false | true | Stay on same domain? |

**Modes:**
- `extract` — Scrape structured data from each page following links
- `journey` — Walk a user flow step-by-step (form fills, clicks, navigation)
- `audit` — Check pages for issues (a11y, broken links, consistency)

**Navigation:** Uses Playwright browser (not SearchBroker).

```
subagent crawl https://docs.example.com mode extract and extract_schema "title, h2_headings[], code_blocks[]": <url>
subagent crawl https://app.example.com mode journey and steps ["Click Sign Up", "Fill email", "Submit"]: <url>
subagent crawl https://mysite.com mode audit and extract_schema "missing_alt_text, broken_links, color_contrast_issues" and max_pages 15: <url>
subagent crawl https://docs.example.com with link_pattern "api|reference" and max_pages 20: <url>
```

---

### `compare`

| Modifier | Values | Default | Example |
|----------|--------|---------|--------|
| `model` | any model key | `gemini-3.5-flash` | `with model claude-sonnet-4` |
| `focus` | `all`, `layout`, `data`, `style`, `content` | `all` | `with focus data` |
| `context` | description string | (none) | `with context "before/after migration"` |

**Input:** Either `text_a`/`text_b` (text diff) OR `image_a_b64`/`image_b_b64` (visual diff).

```
subagent compare with focus layout: <image_a> vs <image_b>
subagent compare with focus data and context "Q1 vs Q2 metrics": <text_a> vs <text_b>
```

---

### `fetch`

| Modifier | Values | Default | Example |
|----------|--------|---------|--------|
| `method` | GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS | GET | `POST` |
| `headers` | key-value object | (none) | `with headers {"Authorization": "Bearer ..."}` |
| `body` | string | (none) | `with body {"key": "value"}` |
| `timeout` | seconds | 30 | `with timeout 60` |

**No model parameter** — pure HTTP, no LLM involved.

```
subagent fetch https://api.github.com/repos/mlflow/mlflow/releases/latest
subagent fetch POST https://httpbin.org/post with body {"test": true} and timeout 60
```

---

### `search`

| Modifier | Values | Default | Example |
|----------|--------|---------|--------|
| `model` | any model key | `gemini-3.5-flash` | `with model gemini-2.5-pro` |
| `num_results` | 1–10 | 5 | `with num_results 10` |

**Search engine:** SearchBroker (DDG → Brave). NOT browser.

```
subagent search: Databricks Unity Catalog metric views YAML syntax
subagent search with num_results 10: CVPR 2026 3D reconstruction papers
```

---

### `get_task_result`

| Modifier | Values | Default | Example |
|----------|--------|---------|--------|
| `task_id` | specific task ID | (none = all tasks) | `get result for abc123` |
| `include_results` | true/false | true | `with include_results false` (previews only) |

```
subagent get result for <task_id>
subagent update                        (bulk status of all tasks)
subagent update with include_results false  (just statuses, no payloads)
```

---

## Patterns & Recipes

### ⚠️ No Chaining / Piping

Each tool call is **one message, one tool**. There is no `->` pipe or chain syntax.

```
# ✗ WRONG — chaining doesn't work
subagent research deep -> analyze with model qwen3.5-122b: <topic>

# ✓ CORRECT — two separate messages
# Message 1:
subagent research with depth deep: <topic>
# Message 2 (after result comes back):
subagent analyze with model qwen3.5-122b: Given these findings: <paste>. Synthesize.
```

For multi-step workflows, issue each tool in a separate message and reference the prior result.

---

### Multi-Model Comparison
```
# Run same prompt through 3 models, compare outputs
subagent analyze with model glm-5.2: <prompt>
subagent analyze with model claude-haiku-4.5: <prompt>
subagent analyze with model gemini-3.5-flash: <prompt>
```

### Research → Synthesize Pipeline
```
# Message 1: kick off research
subagent research with depth deep: <topic>
# Message 2: after research completes, synthesize with a different model
subagent analyze with model qwen3.5-122b: Given these findings: <paste research result>. Produce a decision memo.
```

### Adversarial Quality Check
```
# Generator writes, critic tears apart, generator improves
subagent debate with generator_model glm-5.2 and critic_model claude-sonnet-4: <architectural proposal>
```

### Structured Data Pipeline
```
# 1. Fetch raw page
subagent fetch https://some-api.com/data
# 2. Extract structured JSON from the response
subagent structured_extract from <response text>, schema: {"items": [{"name": str, "price": float}]}
```

### Bulk Analysis
```
# Process 10 items in parallel (same model)
subagent fan_out: ["Classify: <item1>", "Classify: <item2>", ... "Classify: <item10>"]
```

### Progressive Web Research
```
# Quick scan first
subagent research with depth quick: <topic>
# Then deep dive on promising angle
subagent research with depth deep and include_domains ["arxiv.org", "openaccess.thecvf.com"]: <refined query>
```

---

## Token Budgets (Current)

| Context | Budget |
|---------|--------|
| Default `max_tokens` | 4,096 |
| Thinking models effective (6× multiplier) | 24,576 |
| `max_output` ceiling (standard models) | 32,768 – 65,536 |
| `max_output` ceiling (frontier models) | 131,072 |
| Debate default per stage | 8,192 |

Thinking models (GLM 5.2, Gemini 2.5, Claude Sonnet 4+, GPT 5+, Qwen) automatically get 6× budget inflation to account for reasoning tokens.

---

## SearchBroker Internals (for debugging)

- **Progressive escalation:** DDG first → Brave only if <5 results or <3 domains
- **Rate shaping:** DDG=0.25/s (~4s gap), Brave=0.4/s (~2.5s gap)
- **Cache:** Dual-TTL (fresh + 6× stale window). Stale served on live failure.
- **Budget:** Research tool enforces `max_searches` per run
- **Coalescing:** Duplicate in-flight queries collapsed to single call

---

## Sync vs Async Tools

| Synchronous (instant result) | Asynchronous (returns task_id) |
|------------------------------|--------------------------------|
| `status` | `inspect_page` |
| `analyze` | `browser_task` |
| `vision` | `render_html` |
| `fan_out` | `debate` |
| `structured_extract` | `research` |
| `compare` | `crawl` |
| `fetch` | `search` |

Async tools return `{"task_id": "...", "status": "running"}`. Poll with `subagent get result for <task_id>` or `subagent update` for all.

---

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| `research deep -> analyze with model X:` | No chaining. Two separate messages. |
| `model qwen 3.6` | Model keys have no spaces: `qwen3.5-122b`, `qwen3-next-80b` |
| `model gemini-flash` | Use full key: `gemini-3.5-flash` (old aliases removed) |
| `model claude-sonnet` | Use versioned key: `claude-sonnet-4`, `claude-sonnet-4.5`, etc. |
| `model gpt-5.6` | Use variant: `gpt-5.6-luna`, `gpt-5.6-sol`, or `gpt-5.6-terra` |
| Expecting instant result from `research` | It's async — returns `task_id`, poll with `get_task_result` |
| `fan_out` with different models per prompt | Not supported — one model for all. Use separate `analyze` calls. |
| Sending image to `glm-5.2` or `qwen*` | These are text-only. Use `gemini-3.5-flash`, `claude-*`, or `gpt-*` for vision. |
| `max_tokens=50` on a thinking model | Thinking tokens eat the budget. Default 4096 is fine; raise if needed. |
