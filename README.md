# Genie Code Subagents

MCP server providing AI-powered tools as subagents for Databricks Genie Code.

## Architecture

A Starlette-based MCP server (JSON-RPC over POST `/mcp`) deployed as a Databricks App.
Connects to multiple foundation model endpoints via Databricks Model Serving.

## Tools (15)

| Tool | Mode | Description |
|------|------|-------------|
| `status` | sync | Server health, model registry, task summary |
| `analyze` | sync | General text/reasoning |
| `vision` | sync | Image understanding |
| `fan_out` | sync | Parallel prompts (up to 10) |
| `structured_extract` | sync | JSON extraction from text |
| `compare` | sync | Visual or textual diff |
| `fetch` | sync | HTTP client with security blocklist |
| `search` | async | Web search (native grounding + browser fallback) |
| `research` | async | Multi-page breadth-first research + synthesis |
| `crawl` | async | Structured multi-page extraction |
| `inspect_page` | async | Browser page inspection |
| `browser_task` | async | Goal-driven browser automation |
| `render_html` | async | Local HTML rendering |
| `debate` | async | Adversarial multi-model review |
| `get_task_result` | sync | Retrieve async results (single or bulk) |

## Models

| Key | Endpoint | Speed | Cost |
|-----|----------|-------|------|
| gemini-flash | databricks-gemini-3-6-flash | fast | low |
| gemini-pro | databricks-gemini-2-5-pro | slow | high |
| gpt-5.6 | databricks-gpt-5-6 | medium | high |
| claude-sonnet | databricks-claude-sonnet-4 | medium | medium |
| llama-maverick | databricks-meta-llama-4-maverick | fast | low |

## Deployment

```bash
# Via Databricks SDK
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import AppDeployment, AppDeploymentMode

w = WorkspaceClient()
w.apps.deploy_and_wait(
    app_name="mcp-gemini-subagent",
    app_deployment=AppDeployment(
        source_code_path="/Workspace/Users/kevin.ippen@databricks.com/mcp-gemini-subagent",
        mode=AppDeploymentMode.SNAPSHOT
    )
)
```

## Browser Libraries

The `libs/` directory (not committed) contains Ubuntu 22.04 Jammy .so files required
for headless Chromium. Generate them by running the `download_browser_libs` notebook.

## Version

Current: **v1.1.0-beta** (15 tools, multi-model routing)
