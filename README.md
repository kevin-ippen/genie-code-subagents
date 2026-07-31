# MCP Genie Subagent

A multi-model MCP server that extends [Databricks Genie Code](https://docs.databricks.com/en/notebooks/genie-code.html) with 15 AI-powered tools — web research, browser automation, structured extraction, multi-model debate, and more.

## Quick Start

### 1. Clone to your Databricks workspace

```bash
# In your Databricks workspace, create a Git folder pointing to this repo
# Or copy the files to: /Workspace/Users/<your-email>/mcp-genie-subagent/
```

### 2. Generate browser libraries

Run the `download_browser_libs` notebook (included) on any cluster to generate the `libs/` directory. These are Ubuntu 22.04 (Jammy, glibc 2.35) shared libraries required for headless Chromium.

### 3. Create the Databricks App

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import App

w = WorkspaceClient()
w.apps.create_and_wait(app=App(name="mcp-genie-subagent"))
```

### 4. Deploy

```python
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import AppDeployment, AppDeploymentMode

w = WorkspaceClient()
w.apps.deploy_and_wait(
    app_name="mcp-genie-subagent",
    app_deployment=AppDeployment(
        source_code_path="/Workspace/Users/<your-email>/mcp-genie-subagent",
        mode=AppDeploymentMode.SNAPSHOT
    )
)
```

### 5. Connect MCP Connector

In Databricks Assistant settings → MCP Connectors → Add:
- **URL:** `https://mcp-genie-subagent-<workspace-id>.aws.databricksapps.com/mcp`

### 6. Use it

Say **"subagent"** in any Genie Code message to activate:
```
"subagent research best practices for Delta Lake optimization"
"subagent crawl https://docs.example.com extract_schema='title, summary'"
"subagent analyze this code for performance issues"
```

---

## Architecture

```
Genie Code → POST /mcp (JSON-RPC) → Starlette App → Model Serving Endpoints
```

- **Protocol:** MCP 2025-03-26 (JSON-RPC, NOT SSE/streaming)
- **Framework:** Pure Starlette (NOT FastMCP, NOT FastAPI)
- **Workers:** 1 (required for in-memory task store)
- **Auth:** OAuth via X-Forwarded-Access-Token header
- **Models:** Routed via Databricks Foundation Model endpoints

---

## Tools (15)

### Sync (return immediately)

| Tool | Purpose |
|------|---------|
| `status` | Server health, model registry, running tasks |
| `analyze` | Text reasoning, summarization, code review |
| `vision` | Image understanding |
| `fan_out` | Parallel prompts (up to 10) |
| `structured_extract` | JSON extraction with schema |
| `compare` | Visual or textual diff with scoring |
| `fetch` | HTTP client (GET/POST/PUT/DELETE) |

### Async (return `task_id` → poll with `get_task_result`)

| Tool | Purpose |
|------|---------|
| `search` | Web search with synthesis |
| `research` | Multi-page deep research (breadth-first crawl + LLM synthesis) |
| `crawl` | 3 modes: **extract** (structured scraping), **journey** (user flow testing), **audit** (quality checks) |
| `inspect_page` | Browser page snapshot + ARIA tree + summary |
| `browser_task` | Goal-driven browser automation (plan → execute → verify) |
| `render_html` | Render HTML string, screenshot + describe |
| `debate` | Adversarial: generate → critique → refine |
| `get_task_result` | Retrieve async results (single task or bulk status) |

---

## Model Registry

The server routes to 5 foundation models via Databricks Model Serving:

| Key | Default Endpoint | Provider | Speed | Cost |
|-----|-----------------|----------|-------|------|
| `gemini-flash` | databricks-gemini-3-6-flash | Google | fast | low |
| `gemini-pro` | databricks-gemini-2-5-pro | Google | slow | high |
| `gpt-5.6` | databricks-gpt-5-6 | OpenAI | medium | high |
| `claude-sonnet` | databricks-claude-sonnet-4 | Anthropic | medium | medium |
| `llama-maverick` | databricks-meta-llama-4-maverick | Meta | fast | low |

Override any tool's default: `"subagent analyze ... model=claude-sonnet"`

### Custom Endpoints

Override any model's endpoint via environment variables in `app.yaml`:
```yaml
env:
  - name: MODEL_GEMINI_FLASH_ENDPOINT
    value: "your-custom-endpoint-name"
```

---

## Configuration (app.yaml)

```yaml
command:
  - sh
  - -c
  - "uvicorn server:app --host 0.0.0.0 --port $DATABRICKS_APP_PORT --workers 1"
env:
  - name: DEFAULT_MODEL
    value: "databricks-gemini-3-6-flash"
  - name: DEFAULT_CONCURRENCY
    value: "10"
  - name: DEFAULT_MAX_TOKENS
    value: "1024"
  - name: BROWSER_LIBS_PATH
    value: "/app/python/source_code/libs"
```

| Env Var | Purpose | Default |
|---------|---------|---------|
| `DEFAULT_MODEL` | Fallback model endpoint | databricks-gemini-3-6-flash |
| `DEFAULT_CONCURRENCY` | Per-model concurrency limit | 10 |
| `DEFAULT_MAX_TOKENS` | Default max tokens for sync calls | 1024 |
| `BROWSER_LIBS_PATH` | Path to bundled .so files | /app/python/source_code/libs |
| `MAX_BROWSER_ROUNDS` | Max plan-execute iterations | 3 |
| `BROWSER_TIMEOUT_MS` | Per-action browser timeout | 30000 |
| `TASK_TTL_SECONDS` | Task result expiration | 600 |

---

## Prerequisites

- Databricks workspace with **Foundation Model APIs** enabled (pay-per-token endpoints)
- The app's auto-created Service Principal needs `CAN_QUERY` on model serving endpoints (granted by default for `databricks-*` pay-per-token endpoints)
- For browser tools: the `libs/` directory must be present (run `download_browser_libs` notebook)

---

## File Structure

```
├── server.py              # Single-file MCP server (~1930 lines)
├── app.yaml               # Databricks App config
├── requirements.txt       # Python dependencies
├── app.py                 # Legacy (unused)
├── install_deps.py        # Legacy (unused)
├── MULTI_MODEL_ARCHITECTURE.md  # Design documentation
├── download_browser_libs  # Notebook to generate libs/
├── .gitignore
├── README.md              # This file
└── libs/                  # (gitignored) Chromium shared libraries
```

---

## Known Limitations

- **15-tool limit:** MCP connectors in Genie Code support max 15 tools
- **Single worker:** In-memory task store requires `--workers 1`
- **Browser libs:** Must be regenerated if container base OS changes
- **Stale schemas:** After redeploy, disconnect + reconnect the MCP connector in Assistant settings
- **Auth wall:** Cannot test with PATs — requires OAuth (only works via Genie Code or `x-forwarded-access-token`)

---

## Version

**v1.1.0** — 15 tools, 5 models, multi-model routing, browser automation, web research, crawl (extract/journey/audit)

## License

Internal use. Contact repo owner for access.
