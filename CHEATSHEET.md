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

## The 15 Tools

### 1. `status` — Health & Registry

Check server health, list all models, see routing defaults.

```
subagent status
```

Returns: model list with vision/thinking/speed/cost flags, current routing table, task counts.

---

### 2. `analyze` — Text/Reasoning (Synchronous)

General-purpose LLM call. Summarization, analysis, code review, classification.

```
subagent analyze: Summarize the tradeoffs between Elo and TrueSkill ranking systems
subagent analyze with model claude-sonnet-4: Review this architecture for security flaws: <paste>
subagent analyze with model glm-5.2: Explain the O(n²) complexity problem in pairwise evaluation
```

**Parameters:** `prompt` (required), `model`, `system_prompt`, `max_tokens` (default 4096)

---

### 3. `vision` — Image Analysis (Synchronous)

Send a base64 image + prompt. Requires a vision-capable model.

```
subagent vision: What's in this architecture diagram? <attached image>
subagent vision with model gemini-3.5-flash: Describe the data flow in this screenshot
```

**Parameters:** `image_base64` (required), `prompt` (required), `model`, `mime_type` (default `image/png`), `max_tokens`

**Vision-capable models:** All Google, all Anthropic, all OpenAI (except oss-*), llama-4-maverick. NOT: glm-5.2, qwen*, llama-3.x, inkling, gemma.

---

### 4. `fan_out` — Parallel Prompts (Synchronous)

Run up to 10 independent prompts concurrently on the SAME model.

```
subagent fan_out: ["Summarize paper A", "Summarize paper B", "Summarize paper C"]
subagent fan_out with model claude-haiku-4.5: ["Extract entities from chunk 1", "Extract entities from chunk 2"]
```

**Parameters:** `prompts` (required, array max 10), `model`, `system_prompt`, `max_tokens`

**Note:** All prompts use the same model. For multi-model comparison, use separate `analyze` calls.

---

### 5. `structured_extract` — JSON Extraction (Synchronous)

Extract structured data from text given a JSON schema.

```
subagent structured_extract from this meeting transcript, extract {"attendees": [string], "decisions": [string], "action_items": [{"owner": string, "task": string, "due": string}]}
```

**Parameters:** `text` (required), `schema_json` (required, JSON Schema string), `model`, `extraction_prompt`, `max_tokens`

---

### 6. `inspect_page` — Headless Browser Screenshot (Async)

Open a URL → get accessibility tree + screenshot + AI summary.

```
subagent inspect_page: https://docs.databricks.com/en/machine-learning/index.html
subagent inspect_page with question "what nav items are visible": https://example.com
```

**Parameters:** `url` (required), `question`, `model`

**Returns:** `task_id` → poll with `get_task_result`

---

### 7. `browser_task` — Goal-Driven Browser Agent (Async)

Navigate + interact with a page to accomplish a goal.

```
subagent browser_task: go to https://pypi.org and find the latest version of mlflow
subagent browser_task with max_rounds 5: fill out the signup form on https://example.com with test data
```

**Parameters:** `url` (required), `goal` (required), `max_rounds` (1-5, default 3), `model`

**Returns:** `task_id` → poll with `get_task_result`

---

### 8. `render_html` — Local HTML Render (Async)

Render HTML/CSS/JS in headless browser → screenshot + a11y tree.

```
subagent render_html: <html><body><h1>Test</h1><canvas id="chart"></canvas></body></html>
```

**Parameters:** `html` (required), `css`, `js`, `viewport_width/height`, `wait_ms`, `model`, `summarize` (bool)

**Returns:** `task_id` → poll with `get_task_result`

---

### 9. `debate` — Adversarial Generate/Critique/Refine (Async)

Model A generates → Model B critiques → Model A refines (if critique warrants it).

```
subagent debate: Should we use event sourcing or CRUD for the order service?
subagent debate with generator_model glm-5.2 and critic_model claude-haiku-4.5: Is VLM-as-judge better than geometric metrics for 3D mesh QA?
```

**Parameters:** `prompt` (required), `generator_model`, `critic_model`, `model` (fallback for both), `system_prompt`, `max_tokens` (default 8192)

**Returns:** `task_id` → poll with `get_task_result`

**Result contains:** `generation`, `critique`, `refinement` (or `refinement_skipped` if critique found no issues), `models_used`, `total_tokens`

---

### 10. `research` — Multi-Page Web Research (Async)

Plan sub-queries → search (via SearchBroker/DDG) → read pages → synthesize → verify claims.

```
subagent research: What are the best practices for deploying MLflow models on Kubernetes in 2026?
subagent research with depth deep and max_searches 6: Compare Databricks vs Snowflake serverless pricing models
subagent research with model gemini-2.5-pro: Latest advances in neural radiance fields for urban reconstruction
```

**Parameters:** `query` (required), `depth` (`quick`|`standard`|`deep`), `max_searches` (1-6, default 4), `max_pages` (1-10, default 5), `freshness` (`24h`|`7d`|`30d`|`1y`|null), `include_domains` (array), `start_urls` (array), `model`

**Search method:** Uses SearchBroker (DDG/Brave progressive escalation), NOT browser. Pages read via httpx direct fetch.

**Returns:** `task_id` → poll with `get_task_result`

**Result contains:** `answer` (synthesized), `sources` (cited URLs), `claims` (extracted + verified), `stats` (searches, pages read, passages, tokens)

---

### 11. `crawl` — Multi-Page Extract/Journey/Audit (Async)

Three modes for navigating multiple pages:

**Extract mode** — scrape structured data from each page:
```
subagent crawl https://docs.databricks.com/en/delta/index.html extract schema="title, description, code_examples[]"
```

**Journey mode** — walk a user flow step by step:
```
subagent crawl https://app.example.com journey steps=["Click Login", "Fill email with test@test.com", "Submit"]
```

**Audit mode** — check pages for issues:
```
subagent crawl https://my-dashboard.com audit schema="accessibility_issues, broken_links, missing_alt_text"
```

**Parameters:** `url` (required), `mode` (`extract`|`journey`|`audit`), `extract_schema`, `link_pattern` (regex for link filtering), `steps` (journey mode), `max_pages` (1-20, default 10), `same_origin` (bool, default true), `model`

**Navigation method:** Uses Playwright browser (navigates actual pages).

**Returns:** `task_id` → poll with `get_task_result`

---

### 12. `compare` — Visual or Text Diff (Synchronous)

Compare two images OR two text blocks.

```
subagent compare these two SQL queries: <text_a> vs <text_b>
subagent compare with focus layout: <image_a> vs <image_b>
```

**Parameters:** `text_a`/`text_b` OR `image_a_b64`/`image_b_b64`, `focus` (`all`|`layout`|`data`|`style`|`content`), `context`, `model`

---

### 13. `fetch` — Raw HTTP Request (Synchronous)

Make any HTTP request and get the response.

```
subagent fetch https://api.github.com/repos/mlflow/mlflow/releases/latest
subagent fetch POST https://httpbin.org/post with body {"key": "value"}
```

**Parameters:** `url` (required), `method` (GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS), `headers` (object), `body` (string), `timeout` (default 30s)

**No model parameter** — this is pure HTTP, no LLM involved.

---

### 14. `search` — Web Search (Async)

Search the web for information. Uses SearchBroker (DDG/Brave).

```
subagent search: Databricks Unity Catalog metric views YAML syntax
subagent search with model gemini-2.5-pro: latest CVPR 2026 papers on 3D reconstruction
```

**Parameters:** `query` (required), `num_results` (1-10, default 5), `model`

**Search method:** SearchBroker (progressive escalation: DDG first, Brave fallback). NOT browser-based.

**Returns:** `task_id` → poll with `get_task_result`

---

### 15. `get_task_result` — Poll Async Results

Retrieve results from any async tool (debate, research, crawl, search, inspect_page, browser_task, render_html).

```
subagent get result for <task_id>
subagent update (gets ALL task statuses)
```

**Parameters:** `task_id` (specific task, or omit for bulk status)

---

## Patterns & Recipes

### Multi-Model Comparison
```
# Run same prompt through 3 models, compare outputs
subagent analyze with model glm-5.2: <prompt>
subagent analyze with model claude-haiku-4.5: <prompt>
subagent analyze with model gemini-3.5-flash: <prompt>
```

### Research → Synthesize Pipeline
```
# 1. Search for sources
subagent research with depth deep: <topic>
# 2. After result, synthesize with a powerful model
subagent analyze with model gemini-2.5-pro: Given these findings: <paste research result>. Produce a decision memo.
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
