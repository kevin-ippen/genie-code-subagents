# MCP Genie Subagent — Comprehensive Cheat Sheet

**Version:** v1.1.0 | **Tools:** 15 | **Models:** 5 | **Deployment:** `01f18ce726b21aa780e90702019a625b`
**URL:** `https://mcp-genie-subagent-1444828305810485.aws.databricksapps.com`
**Trigger:** All tools gated — say **"subagent"** in your message to activate.

---

## Quick Reference: All 15 Tools

### Sync Tools (return immediately)

| Tool | Purpose | Key Params | Default Model |
|------|---------|------------|---------------|
| `status` | Server health + model registry + task counts | — | — |
| `analyze` | Text reasoning, summarization, code review | `prompt`, `system_prompt`, `model`, `max_tokens` | gemini-flash |
| `vision` | Image understanding | `image_base64`, `prompt`, `model` | gemini-flash |
| `fan_out` | Parallel prompts (same model, up to 10) | `prompts[]`, `system_prompt`, `model` | gemini-flash |
| `structured_extract` | JSON extraction from text | `text`, `schema`, `model` | gemini-flash |
| `compare` | Visual or textual diff | `image_a_b64`+`image_b_b64` OR `text_a`+`text_b`, `focus` | gemini-flash |
| `fetch` | HTTP client (GET/POST/PUT/DELETE) | `url`, `method`, `headers`, `body`, `timeout` | — |

### Async Tools (return `task_id` → poll with `get_task_result`)

| Tool | Purpose | Key Params | Default Model |
|------|---------|------------|---------------|
| `search` | Web search (native grounding + browser fallback) | `query`, `num_results`, `model` | gemini-flash |
| `research` | Multi-page deep research with synthesis | `query`, `start_urls`, `max_pages` (1-10), `model` | gemini-flash |
| `crawl` | Multi-mode browser tool (3 modes below) | `url`, `mode`, see modes | gemini-flash |
| `inspect_page` | Read-only page snapshot + summary | `url`, `question`, `model` | gemini-flash |
| `browser_task` | Goal-driven browser automation | `url`, `goal`, `model` | gemini-flash |
| `render_html` | Render HTML string, screenshot + describe | `html`, `question`, `viewport_width` | gemini-flash |
| `debate` | Adversarial: generate → critique → refine | `prompt`, `generator_model`, `critic_model` | flash/flash |

### Retrieval

| Tool | Purpose | Key Params |
|------|---------|------------|
| `get_task_result` | Retrieve async results | `task_id` (single) or omit (bulk status) |

---

## Crawl Tool — 3 Modes

```
crawl(url, mode="extract|journey|audit", ...)
```

| Mode | What It Does | Extra Params | Returns |
|------|-------------|--------------|--------|
| **`extract`** (default) | Spider pages, extract same schema from each | `extract_schema`, `link_pattern`, `max_pages`, `same_origin` | `{pages_crawled, results: [{url, title, data}]}` |
| **`journey`** | Walk user flow step-by-step in one browser session | `steps[]` (ordered action list) | `{steps_completed, results: [{step, goal, completed, evaluation}]}` |
| **`audit`** | Visit pages, score quality, report issues | `extract_schema` (checks to run), `link_pattern`, `max_pages` | `{pages_audited, total_issues, issues: [{url, issue}]}` |

**Examples:**
- Extract: `crawl(url="https://shop.com/category", extract_schema="product name, price, rating", link_pattern="/product/")`
- Journey: `crawl(url="https://app.com/login", mode="journey", steps=["Login with test@test.com", "Navigate to Settings", "Change timezone"])`
- Audit: `crawl(url="https://docs.site.com", mode="audit", extract_schema="broken links, missing images, accessibility", max_pages=10)`

---

## Model Registry

| Key | Endpoint | Provider | Vision | Thinking | Speed | Cost | Concurrency |
|-----|----------|----------|--------|----------|-------|------|-------------|
| `gemini-flash` | databricks-gemini-3-6-flash | Google | ✓ | ✓ | fast | low | 10 |
| `gemini-pro` | databricks-gemini-2-5-pro | Google | ✓ | ✓ | slow | high | 3 |
| `gpt-5.6` | databricks-gpt-5-6 | OpenAI | ✓ | — | medium | high | 5 |
| `claude-sonnet` | databricks-claude-sonnet-4 | Anthropic | ✓ | — | medium | medium | 5 |
| `llama-maverick` | databricks-meta-llama-4-maverick | Meta | ✓ | — | fast | low | 10 |

**Override any tool's model:** add `model: "claude-sonnet"` (or any key above)

---

## Default Routing Table

| Role | Model | Reason |
|------|-------|--------|
| analyze, vision, fan_out, structured_extract | gemini-flash | Fast, cheap, reliable |
| browser_planning, browser_summarize | gemini-flash | Fast iteration, good vision |
| debate_generator, debate_critic | gemini-flash | Quick for both sides |
| research_extract, research_synthesize | gemini-flash | High concurrency |
| cascade_fast | gemini-flash | Quick first pass |
| cascade_powerful | gemini-pro | Deep reasoning escalation |

---

## Trigger Patterns

| You Say | Tool Called | What Happens |
|---------|-------------|---------------|
| "subagent analyze this code..." | `analyze` | Text reasoning |
| "subagent search for X" | `search` | Web search → results + synthesis |
| "subagent research X" | `research` | Multi-page deep dive |
| "subagent crawl https://..." | `crawl` (extract) | Spider + structured extraction |
| "subagent inspect https://..." | `inspect_page` | Screenshot + ARIA summary |
| "subagent compare these images" | `compare` | Visual diff |
| "subagent fetch https://api.com/v1/..." | `fetch` | Raw HTTP request |
| "subagent debate whether X" | `debate` | Generate → critique → refine |
| "subagent update" | `get_task_result` (no id) | Bulk status of all tasks |
| "subagent status" | `status` | Server health + model info |

---

## Architecture at a Glance

```
┌─────────────────────────────────────────────────────────────┐
│  GENIE CODE (calls tools when user says "subagent")         │
└──────────────────────────┬──────────────────────────────────┘
                           │ POST /mcp (JSON-RPC)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  MCP SERVER (Pure Starlette, single worker)                 │
│                                                             │
│  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐  │
│  │ Sync    │  │ Async    │  │ Browser  │  │ Task Store │  │
│  │ Tools   │  │ Tools    │  │ Engine   │  │ (in-mem)   │  │
│  └────┬────┘  └────┬─────┘  └────┬─────┘  └──────┬─────┘  │
│       │            │             │               │         │
│       ▼            ▼             ▼               ▼         │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  _call_model(role, messages, max_tokens, override)  │   │
│  │  → resolve_model() → per-model semaphore → OpenAI   │   │
│  └─────────────────────────────────────────────────────┘   │
│       │                                                     │
│       ▼                                                     │
│  ┌──────────────────────────────────────────────┐          │
│  │  Databricks Model Serving Endpoints          │          │
│  │  (5 models, OAuth via SP credentials)        │          │
│  └──────────────────────────────────────────────┘          │
└─────────────────────────────────────────────────────────────┘
```

---

## Security

### Fetch Blocklist
- **Hosts:** localhost, 127.0.0.1, 0.0.0.0, [::1], metadata.google.internal
- **Prefixes:** 169.254.x, 10.x, 192.168.x, 172.16-31.x
- **Paths:** `/api/2.0/`, `/api/2.1/`, `/serving-endpoints/`

### Auth
- User token: `x-forwarded-access-token` header (OAuth from Genie Code)
- SP token: `WorkspaceClient().get_oauth_token()` for model serving
- Browser sessions: user token injected for Databricks app pre-auth

---

## Task Lifecycle (Async Tools)

```
1. Call async tool → returns {task_id: "uuid"} immediately
2. Background: tool executes (browser, multi-page, multi-model)
3. Poll: get_task_result(task_id="uuid") → running/done/error
4. Bulk: get_task_result() (no id) → all tasks summary
5. TTL: tasks expire after 10 minutes
```

---

## Key Technical Details

| Aspect | Value |
|--------|-------|
| Framework | Starlette (NOT FastMCP, NOT FastAPI) |
| Protocol | JSON-RPC over POST /mcp |
| MCP Version | 2025-03-26 |
| Worker count | 1 (--workers 1, required for in-memory task store) |
| Sync max tokens | 2048 cap (min 256) |
| Browser | Playwright + Chromium headless |
| Browser rounds | Max 3 per goal |
| Browser timeout | 30s per action |
| ARIA tree cap | 8000 chars in browser planning |
| Fan out limit | 10 parallel prompts |
| Research pages | 1-10 (default 5) |
| Crawl pages | 1-20 (default 10) |
| Task TTL | 600s (10 min) |
| Libs path | /app/python/source_code/libs (LD_LIBRARY_PATH set ONLY during browser sessions) |
| Container | Ubuntu 22.04, glibc 2.35 (Jammy) |

---

## Deployment

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import AppDeployment, AppDeploymentMode

w = WorkspaceClient()
deployment = w.apps.deploy_and_wait(
    app_name="mcp-genie-subagent",
    app_deployment=AppDeployment(
        source_code_path="/Workspace/Users/kevin.ippen@databricks.com/mcp-genie-subagent",
        mode=AppDeploymentMode.SNAPSHOT
    )
)
```

**After deploy:** Disconnect + reconnect MCP connector in Assistant settings (known bug: stale tool schemas).

---

## File Layout

```
/Workspace/Users/kevin.ippen@databricks.com/mcp-genie-subagent/
├── server.py              (1931 lines, single-file server)
├── app.yaml               (uvicorn --workers 1)
├── requirements.txt       (starlette, uvicorn, openai, httpx, playwright, databricks-sdk)
├── app.py                 (unused legacy)
├── install_deps.py        (unused legacy)
├── MULTI_MODEL_ARCHITECTURE.md  (design doc)
├── CHEAT_SHEET.md         (this file)
├── download_browser_libs  (notebook — regenerates libs/)
└── libs/                  (30 .so files, glibc 2.35, gitignored)
```

---

## Roadmap

| Version | Status | Features |
|---------|--------|----------|
| v1.0.0 | ✅ Shipped | Multi-model foundation, 9 core tools, routing, debate |
| v1.1.0 | ✅ Current | +search, fetch, compare, research, crawl (3 modes), tool merges = 15 tools |
| v1.1.0 | 🔜 Next | +digest (large doc processing), +monitor (background polling) |
| v1.2.0 | Planned | multi_analyze, cascade, visual_review, agent tool-use |
| v1.3.0 | Planned | Caching, ARIA compaction, connection pooling, fallback chains, module split |

---

## Common Patterns

### Research a topic
```
"subagent research what are the best practices for Delta Lake Z-ordering in 2026"
→ search tool finds starting URLs → visits 5 pages → extracts facts → synthesizes report
```

### Crawl & extract structured data
```
"subagent crawl https://news.ycombinator.com with extract_schema 'title, url, points, comments' and link_pattern '/item'"
→ spiders pages matching pattern → returns uniform JSON per page
```

### Test a user journey
```
"subagent crawl https://myapp.com mode=journey steps=['Click Sign Up', 'Fill form with test data', 'Submit and verify confirmation']"
→ walks through each step in one browser session → evaluates success per step
```

### Audit a site
```
"subagent crawl https://docs.mycompany.com mode=audit max_pages=10"
→ visits 10 pages → checks for broken links, missing images, accessibility → scores each page
```

### Compare before/after
```
"subagent compare these two screenshots" (attach image_a and image_b)
→ structured diff with similarity %, categorized changes, recommendations
```

### Quick API probe
```
"subagent fetch https://api.github.com/repos/databricks/spark method=GET"
→ raw HTTP response: status, headers, body
```

### Get all task statuses
```
"subagent update"
→ bulk status: {running: [...], done: [...], errored: [...]}
```
