"""MCP Genie Subagent Server v1.1.0-beta.

Task-oriented tools with smart model defaults and explicit overrides.
Architecture: see MULTI_MODEL_ARCHITECTURE.md

Sync tools: analyze, vision, fan_out, structured_extract, list_models, health
Async tools: inspect_page, browser_task, render_html, debate
Retrieval: get_task_result

Design principles:
  - Tools named by task, not model
  - Routing table is single source of truth for model defaults
  - AgentConfigs reference role keys, never model names
  - Identical context across models for comparison tasks
  - Hard token ceilings, not warnings
  - Single worker (in-memory task store)
"""

import asyncio
import base64
import contextvars
import json
import logging
import os
import random
import time
from typing import Any
from uuid import uuid4

import httpx

from databricks.sdk import WorkspaceClient
from openai import AsyncOpenAI, RateLimitError, APIStatusError
from playwright.async_api import async_playwright, Page
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-genie-subagent")

# ============================================================
# CONFIG
# ============================================================
MCP_PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "mcp-genie-subagent"
SERVER_VERSION = "1.1.0"

TASK_TTL_SECONDS = int(os.environ.get("TASK_TTL_SECONDS", "600"))
MAX_FAN_OUT = 10
MAX_BROWSER_ROUNDS = int(os.environ.get("MAX_BROWSER_ROUNDS", "3"))
BROWSER_TIMEOUT_MS = int(os.environ.get("BROWSER_TIMEOUT_MS", "30000"))
SYNC_MAX_TOKENS_CAP = 8192
ALLOWED_IMAGE_MIMES = frozenset(["image/png", "image/jpeg", "image/webp", "image/gif"])

# ============================================================
# MODEL REGISTRY & ROUTING (Layer 2)
# ============================================================
MODEL_REGISTRY: dict[str, dict] = {
    "gemini-flash": {
        "endpoint": "databricks-gemini-3-6-flash",
        "provider": "google",
        "vision": True,
        "thinking": True,
        "max_context": 1_000_000,
        "max_output": 8192,
        "speed": "fast",
        "cost_tier": "low",
        "concurrency_limit": 10,
    },
    "gemini-pro": {
        "endpoint": "databricks-gemini-2-5-pro",
        "provider": "google",
        "vision": True,
        "thinking": True,
        "max_context": 1_000_000,
        "max_output": 8192,
        "speed": "slow",
        "cost_tier": "high",
        "concurrency_limit": 3,
    },
    "gpt-5.6": {
        "endpoint": "databricks-gpt-5-6",
        "provider": "openai",
        "vision": True,
        "thinking": False,
        "max_context": 128_000,
        "max_output": 16384,
        "speed": "medium",
        "cost_tier": "high",
        "concurrency_limit": 5,
    },
    "claude-sonnet": {
        "endpoint": "databricks-claude-sonnet-4",
        "provider": "anthropic",
        "vision": True,
        "thinking": False,
        "max_context": 200_000,
        "max_output": 8192,
        "speed": "medium",
        "cost_tier": "medium",
        "concurrency_limit": 5,
    },
    "llama-maverick": {
        "endpoint": "databricks-meta-llama-4-maverick",
        "provider": "meta",
        "vision": True,
        "thinking": False,
        "max_context": 128_000,
        "max_output": 8192,
        "speed": "fast",
        "cost_tier": "low",
        "concurrency_limit": 10,
    },
    "qwen-3.5-122b": {
        "endpoint": "databricks-qwen35-122b-a10b",
        "provider": "qwen",
        "vision": False,
        "thinking": True,
        "max_context": 128_000,
        "max_output": 8192,
        "min_output": 4096,
        "speed": "fast",
        "cost_tier": "low",
        "concurrency_limit": 10,
    },
    "glm-5.2": {
        "endpoint": "databricks-glm-5-2",
        "provider": "zai",
        "vision": False,
        "thinking": True,
        "max_context": 128_000,
        "max_output": 8192,
        "min_output": 4096,
        "speed": "medium",
        "cost_tier": "medium",
        "concurrency_limit": 5,
    },
}

DEFAULT_ROUTING: dict[str, str] = {
    "analyze": "gemini-flash",
    "vision": "gemini-flash",
    "fan_out": "gemini-flash",
    "structured_extract": "gemini-flash",
    "browser_planning": "gemini-flash",
    "browser_summarize": "gemini-flash",
    "debate_generator": "gemini-flash",
    "debate_critic": "gemini-flash",
    "cascade_fast": "gemini-flash",
    "cascade_powerful": "gemini-pro",
    "research_extract": "gemini-flash",
    "research_synthesize": "gemini-flash",
}

for key in list(MODEL_REGISTRY.keys()):
    env_key = f"MODEL_{key.upper().replace('-', '_').replace('.', '_')}_ENDPOINT"
    override = os.environ.get(env_key)
    if override:
        MODEL_REGISTRY[key]["endpoint"] = override
        logger.info(f"Model {key} endpoint overridden to {override}")


def resolve_model(role: str, override: str | None = None) -> dict:
    if override:
        if override not in MODEL_REGISTRY:
            raise ValueError(f"Unknown model: {override}. Available: {list(MODEL_REGISTRY.keys())}")
        return MODEL_REGISTRY[override]
    model_key = DEFAULT_ROUTING.get(role, "gemini-flash")
    return MODEL_REGISTRY[model_key]


def _model_key_for(role: str, override: str | None = None) -> str:
    if override and override in MODEL_REGISTRY:
        return override
    return DEFAULT_ROUTING.get(role, "gemini-flash")


def _workspace_host_url() -> str:
    host = os.environ.get("DATABRICKS_HOST", "").strip().rstrip("/")
    if not host:
        raise RuntimeError("DATABRICKS_HOST is not configured")
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"
    return host


# ============================================================
# INFRASTRUCTURE (Layer 4)
# ============================================================

_server_dir = os.path.dirname(os.path.abspath(__file__))
_bundled_libs = os.environ.get("BROWSER_LIBS_PATH", os.path.join(_server_dir, "libs"))

_token_cache: dict = {"token": None, "expires_at": 0}
_semaphores: dict[str, asyncio.Semaphore] = {}
_current_user_token: contextvars.ContextVar[str] = contextvars.ContextVar("user_token", default="")

_tasks: dict[str, dict] = {}


def _get_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    w = WorkspaceClient()
    headers = w.config.authenticate()
    token = headers.get("Authorization", "").replace("Bearer ", "")
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + 3500
    logger.info("Refreshed SP OAuth token")
    return token


def _get_semaphore(model_key: str) -> asyncio.Semaphore:
    if model_key not in _semaphores:
        limit = MODEL_REGISTRY.get(model_key, {}).get("concurrency_limit", 10)
        _semaphores[model_key] = asyncio.Semaphore(limit)
    return _semaphores[model_key]


def _create_task(tool_name: str, params_summary: str = "", parent_task_id: str | None = None, max_tokens_total: int = 16384) -> str:
    task_id = str(uuid4())[:12]
    _tasks[task_id] = {
        "task_id": task_id,
        "status": "running",
        "tool": tool_name,
        "params_summary": params_summary,
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
        "parent_task_id": parent_task_id,
        "tokens_used": 0,
        "max_tokens_total": max_tokens_total,
        "model_used": None,
    }
    return task_id


def _complete_task(task_id: str, result: Any, model_used: str = "", tokens: int = 0):
    if task_id in _tasks:
        _tasks[task_id]["status"] = "done"
        _tasks[task_id]["finished_at"] = time.time()
        _tasks[task_id]["result"] = result
        _tasks[task_id]["model_used"] = model_used
        _tasks[task_id]["tokens_used"] = tokens


def _fail_task(task_id: str, error: str):
    if task_id in _tasks:
        _tasks[task_id]["status"] = "error"
        _tasks[task_id]["finished_at"] = time.time()
        _tasks[task_id]["error"] = error


def _cleanup_old_tasks():
    now = time.time()
    expired = [tid for tid, t in _tasks.items()
               if t["finished_at"] and (now - t["finished_at"]) > TASK_TTL_SECONDS]
    for tid in expired:
        del _tasks[tid]


# ============================================================
# CORE MODEL CALL (Layer 3)
# ============================================================

def extract_text(provider: str, content: Any) -> str:
    """Provider-aware text extraction from model response content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return str(content)


async def _call_model(role: str, messages: list[dict], max_tokens: int = 1024,
                      override: str | None = None) -> dict:
    """Core model call with routing, semaphore, retry."""
    model_key = _model_key_for(role, override)
    config = MODEL_REGISTRY[model_key]
    token = _get_token()
    host = _workspace_host_url()
    transport = httpx.AsyncClient(trust_env=False)
    client = AsyncOpenAI(
        api_key=token,
        base_url=f"{host}/serving-endpoints",
        max_retries=0,
        http_client=transport,
    )
    sem = _get_semaphore(model_key)

    max_tokens = min(
        max(max_tokens, config.get("min_output", 0)),
        config["max_output"],
    )
    start = time.time()
    retries = 0
    max_retries = 3
    last_error: Exception | None = None

    try:
        while retries <= max_retries:
            async with sem:
                try:
                    response = await client.chat.completions.create(
                        model=config["endpoint"],
                        messages=messages,
                        max_tokens=max_tokens,
                    )
                    elapsed = time.time() - start
                    text = extract_text(
                        config["provider"], response.choices[0].message.content
                    )
                    usage = response.usage
                    return {
                        "text": text,
                        "model": model_key,
                        "endpoint": config["endpoint"],
                        "usage": {
                            "prompt_tokens": usage.prompt_tokens if usage else 0,
                            "completion_tokens": usage.completion_tokens if usage else 0,
                            "total_tokens": (
                                (usage.prompt_tokens or 0)
                                + (usage.completion_tokens or 0)
                                if usage
                                else 0
                            ),
                        },
                        "latency_s": round(elapsed, 3),
                        "retries": retries,
                    }
                except RateLimitError as e:
                    last_error = e
                    retries += 1
                    if retries > max_retries:
                        break
                    await asyncio.sleep(1.0 * (2 ** (retries - 1)) + random.uniform(0, 0.5))
                except APIStatusError as e:
                    if e.status_code >= 500:
                        last_error = e
                        retries += 1
                        if retries > max_retries:
                            break
                        await asyncio.sleep(
                            1.0 * (2 ** (retries - 1)) + random.uniform(0, 0.5)
                        )
                    else:
                        return {
                            "text": "",
                            "error": f"API error {e.status_code}: {e.message}",
                            "model": model_key,
                        }
                except Exception as e:
                    cause = f"; cause={type(e.__cause__).__name__}: {e.__cause__}" if e.__cause__ else ""
                    return {
                        "text": "",
                        "error": f"{type(e).__name__}: {e}{cause}",
                        "model": model_key,
                    }

        return {
            "text": "",
            "error": (
                f"{type(last_error).__name__}: {last_error}"
                if last_error
                else "Unknown error"
            ),
            "model": model_key,
        }
    finally:
        await client.close()


# ============================================================
# BROWSER ENGINE
# ============================================================

async def _get_aria_tree(page: Page, max_depth: int = 6) -> str:
    try:
        snapshot = await page.accessibility.snapshot()
        if not snapshot:
            return "[empty accessibility tree]"
        return _format_aria_node(snapshot, depth=0, max_depth=max_depth)
    except Exception as e:
        return f"[ARIA extraction error: {e}]"


def _format_aria_node(node: dict, depth: int = 0, max_depth: int = 6) -> str:
    if depth > max_depth:
        return ""
    indent = "  " * depth
    role = node.get("role", "")
    name = node.get("name", "")
    value = node.get("value", "")
    parts = [f"{indent}[{role}]"]
    if name:
        parts.append(f' "{name}"')
    if value:
        parts.append(f" value={value}")
    line = "".join(parts)
    lines = [line]
    for child in node.get("children", []):
        child_text = _format_aria_node(child, depth + 1, max_depth)
        if child_text:
            lines.append(child_text)
    return "\n".join(lines)


async def _take_screenshot_b64(page: Page) -> str:
    buf = await page.screenshot(full_page=False, type="png")
    return base64.b64encode(buf).decode("utf-8")


async def _execute_action(page: Page, action: dict) -> str:
    act = action.get("action", "").lower()
    locator_str = action.get("locator", "")
    value = action.get("value", "")
    try:
        if act == "click":
            await page.locator(locator_str).first.click(timeout=BROWSER_TIMEOUT_MS)
            return f"clicked: {locator_str}"
        elif act in ("fill", "type"):
            await page.locator(locator_str).first.fill(value, timeout=BROWSER_TIMEOUT_MS)
            return f"filled: {locator_str}"
        elif act == "press":
            loc = page.locator(locator_str) if locator_str else page
            await loc.press(value, timeout=BROWSER_TIMEOUT_MS)
            return f"pressed: {value}"
        elif act in ("navigate", "goto"):
            url = action.get("url", value)
            await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
            return f"navigated to: {url}"
        elif act == "scroll":
            direction = action.get("direction", value or "down")
            await page.mouse.wheel(0, 500 if direction == "down" else -500)
            await asyncio.sleep(0.5)
            return f"scrolled {direction}"
        elif act == "wait":
            ms = int(action.get("ms", value or "1000"))
            await asyncio.sleep(min(ms, 5000) / 1000)
            return f"waited {ms}ms"
        elif act == "select":
            await page.locator(locator_str).first.select_option(value, timeout=BROWSER_TIMEOUT_MS)
            return f"selected: {value}"
        elif act == "hover":
            await page.locator(locator_str).first.hover(timeout=BROWSER_TIMEOUT_MS)
            return f"hovered: {locator_str}"
        else:
            return f"unknown action: {act}"
    except Exception as e:
        return f"FAILED {act} on {locator_str}: {e}"


_browser_installed = False


async def _ensure_browser_installed():
    global _browser_installed
    if _browser_installed:
        return
    import pathlib
    expected_bin = pathlib.Path("/home/app/.cache/ms-playwright/chromium-1105/chrome-linux/chrome")
    if expected_bin.exists():
        _browser_installed = True
        return
    import subprocess
    clean_env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    result = subprocess.run(["playwright", "install", "chromium"],
                           capture_output=True, text=True, timeout=180, env=clean_env)
    if result.returncode != 0:
        raise RuntimeError(f"Chromium install failed: {result.stderr[:300]}")
    if not expected_bin.exists():
        raise RuntimeError(f"Binary not at {expected_bin}")
    _browser_installed = True


async def _pre_authenticate_databricks_app(url: str, token: str) -> list[dict]:
    if not token:
        return []
    try:
        from urllib.parse import urlparse
        cookies = []
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0,
                                     headers={"Authorization": f"Bearer {token}"}) as client:
            resp = await client.get(url)
            parsed = urlparse(str(resp.url))
            for name, value in resp.cookies.items():
                cookies.append({"name": name, "value": value, "domain": parsed.hostname, "path": "/"})
            for cookie in client.cookies.jar:
                if cookie.name not in {c["name"] for c in cookies}:
                    cookies.append({"name": cookie.name, "value": cookie.value,
                                    "domain": cookie.domain or parsed.hostname, "path": cookie.path or "/"})
            if resp.status_code == 200 and "Sign In" in resp.text[:500]:
                return []
        return cookies
    except Exception:
        return []


def _is_databricks_app_url(url: str) -> bool:
    return "databricksapps.com" in url or "databricksapps.net" in url


async def _run_browser_session(url: str, user_token: str, task_fn, viewport_w=1280, viewport_h=900):
    await _ensure_browser_installed()
    pre_auth_cookies = []
    effective_token = user_token
    if _is_databricks_app_url(url):
        if user_token:
            pre_auth_cookies = await _pre_authenticate_databricks_app(url, user_token)
        if not pre_auth_cookies:
            sp_token = _get_token()
            if sp_token and sp_token != user_token:
                pre_auth_cookies = await _pre_authenticate_databricks_app(url, sp_token)
                if pre_auth_cookies:
                    effective_token = sp_token

    _old_ld = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = f"{_bundled_libs}:{_old_ld}" if _old_ld else _bundled_libs
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            ctx_opts = {"viewport": {"width": viewport_w, "height": viewport_h}}
            if effective_token:
                ctx_opts["extra_http_headers"] = {"Authorization": f"Bearer {effective_token}"}
            context = await browser.new_context(**ctx_opts)
            if pre_auth_cookies:
                await context.add_cookies(pre_auth_cookies)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
                await page.wait_for_timeout(1000)
                result = await task_fn(page)
            finally:
                await context.close()
                await browser.close()
    finally:
        if _old_ld:
            os.environ["LD_LIBRARY_PATH"] = _old_ld
        else:
            os.environ.pop("LD_LIBRARY_PATH", None)
    return result


BROWSER_AGENT_SYSTEM = """You are a browser automation agent. Output ONLY valid JSON.
{"done": bool, "result": "string if done", "actions": [{"action": "click|fill|press|navigate|scroll|wait|select|hover", "locator": "role=|text=|css=", "value": "", "reasoning": ""}]}
Use role=, text=, css= locators. Max 5 actions per batch. No XPath."""


async def _browser_plan_and_execute(page: Page, goal: str, max_rounds: int, model_override: str | None = None) -> dict:
    action_log = []
    for round_num in range(1, max_rounds + 1):
        aria = await _get_aria_tree(page)
        screenshot_b64 = await _take_screenshot_b64(page)
        messages = [
            {"role": "system", "content": BROWSER_AGENT_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": f"GOAL: {goal}\nURL: {page.url}\nROUND: {round_num}/{max_rounds}\nACTIONS: {json.dumps(action_log[-10:])}\nARIA:\n{aria[:8000]}"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
            ]},
        ]
        result = await _call_model("browser_planning", messages, max_tokens=2048, override=model_override)
        if result.get("error"):
            return {"error": result["error"], "action_log": action_log, "rounds": round_num}
        raw = result["text"].strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        try:
            plan = json.loads(raw)
        except json.JSONDecodeError:
            action_log.append({"round": round_num, "error": "invalid JSON"})
            continue
        if plan.get("done"):
            return {"done": True, "result": plan.get("result", ""), "url": page.url,
                    "rounds": round_num, "action_log": action_log, "screenshot_b64": screenshot_b64}
        for act in plan.get("actions", [])[:5]:
            outcome = await _execute_action(page, act)
            action_log.append({"round": round_num, "action": act.get("action"),
                               "locator": act.get("locator", ""), "outcome": outcome})
            await page.wait_for_timeout(500)
        await page.wait_for_timeout(1000)
    screenshot_b64 = await _take_screenshot_b64(page)
    return {"done": False, "result": "Max rounds reached.", "url": page.url,
            "rounds": max_rounds, "action_log": action_log, "screenshot_b64": screenshot_b64}


# ============================================================
# TOOL SCHEMAS (Layer 1)
# ============================================================

_MODEL_PARAM = {"type": "string", "description": "Override default model. Use status tool to see options.", "default": None}

TOOL_SCHEMAS = [
    {"name": "status",
     "description": "Only invoke when the user says 'subagent' in their message. Server diagnostics: health, available models, routing defaults, and task counts. Say 'subagent status' to check.",
     "inputSchema": {"type": "object", "properties": {}, "required": []}},
    {"name": "analyze",
     "description": "Only invoke when the user says 'subagent' in their message. General text/reasoning. Send a prompt for summarization, analysis, code review, classification, or any reasoning task.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string", "description": "The user prompt to send."},
         "system_prompt": {"type": "string", "description": "Optional system instructions.", "default": ""},
         "model": _MODEL_PARAM,
         "max_tokens": {"type": "integer", "description": "Max response tokens (min 256).", "default": 1024},
     }, "required": ["prompt"]}},
    {"name": "vision",
     "description": "Only invoke when the user says 'subagent' in their message. Analyze an image with vision. Send a base64-encoded image and a prompt.",
     "inputSchema": {"type": "object", "properties": {
         "image_base64": {"type": "string", "description": "Base64-encoded image data."},
         "prompt": {"type": "string", "description": "What to analyze about the image."},
         "mime_type": {"type": "string", "description": "Image MIME type.", "default": "image/png"},
         "system_prompt": {"type": "string", "description": "Optional system instructions.", "default": ""},
         "model": _MODEL_PARAM,
         "max_tokens": {"type": "integer", "description": "Max response tokens (min 256).", "default": 1024},
     }, "required": ["image_base64", "prompt"]}},
    {"name": "fan_out",
     "description": "Only invoke when the user says 'subagent' in their message. Run up to 10 independent prompts concurrently. Returns JSON array of results.",
     "inputSchema": {"type": "object", "properties": {
         "prompts": {"type": "array", "items": {"type": "string"}, "description": "List of prompts (max 10)."},
         "system_prompt": {"type": "string", "description": "System instructions applied to all.", "default": ""},
         "model": _MODEL_PARAM,
         "max_tokens": {"type": "integer", "description": "Max tokens per response.", "default": 1024},
     }, "required": ["prompts"]}},
    {"name": "structured_extract",
     "description": "Only invoke when the user says 'subagent' in their message. Extract structured JSON from text. Provide text and a JSON schema for the output.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "Source text to extract from."},
         "schema_json": {"type": "string", "description": "JSON Schema string for output structure."},
         "extraction_prompt": {"type": "string", "description": "Additional extraction instructions.", "default": ""},
         "model": _MODEL_PARAM,
         "max_tokens": {"type": "integer", "description": "Max tokens.", "default": 1024},
     }, "required": ["text", "schema_json"]}},
    {"name": "inspect_page",
     "description": "Only invoke when the user says 'subagent' in their message. Open a URL in a headless browser and return the page's accessibility tree, a screenshot, and an AI-generated summary. ASYNC: returns a task_id immediately. Use get_task_result to retrieve the output.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "The URL to inspect."},
         "question": {"type": "string", "description": "Optional: specific question about page content.", "default": ""},
         "model": _MODEL_PARAM,
     }, "required": ["url"]}},
    {"name": "browser_task",
     "description": "Only invoke when the user says 'subagent' in their message. Navigate to a URL and accomplish a goal using an AI-driven browser agent. ASYNC: returns a task_id immediately. Use get_task_result to retrieve the output.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "Starting URL to navigate to."},
         "goal": {"type": "string", "description": "What to accomplish on this page."},
         "max_rounds": {"type": "integer", "description": "Max plan-execute rounds (1-5). Default 3.", "default": 3},
         "model": _MODEL_PARAM,
     }, "required": ["url", "goal"]}},
    {"name": "render_html",
     "description": "Only invoke when the user says 'subagent' in their message. Render HTML/CSS/JS locally in a headless browser and return a screenshot plus accessibility tree. ASYNC: returns a task_id immediately. Use get_task_result to retrieve the output.",
     "inputSchema": {"type": "object", "properties": {
         "html": {"type": "string", "description": "Full HTML document or fragment to render."},
         "css": {"type": "string", "description": "Optional CSS to inject.", "default": ""},
         "js": {"type": "string", "description": "Optional JavaScript to execute after render.", "default": ""},
         "viewport_width": {"type": "integer", "description": "Viewport width in pixels.", "default": 1280},
         "viewport_height": {"type": "integer", "description": "Viewport height in pixels.", "default": 900},
         "wait_ms": {"type": "integer", "description": "Milliseconds to wait after render.", "default": 500},
         "summarize": {"type": "boolean", "description": "If true, AI describes the rendered page.", "default": True},
         "model": _MODEL_PARAM,
     }, "required": ["html"]}},
    {"name": "debate",
     "description": "Only invoke when the user says 'subagent' in their message. Adversarial review: Model A generates, Model B critiques, Model A refines (conditional). ASYNC: returns a task_id immediately.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string", "description": "The prompt/question to debate."},
         "system_prompt": {"type": "string", "description": "Optional system instructions for generator.", "default": ""},
         "generator_model": {"type": "string", "description": "Override generator model.", "default": None},
         "critic_model": {"type": "string", "description": "Override critic model.", "default": None},
         "max_tokens": {"type": "integer", "description": "Max tokens per stage.", "default": 2048},
     }, "required": ["prompt"]}},
    {"name": "research",
     "description": "Only invoke when the user says 'subagent' in their message. Multi-page research: visit pages, extract relevant facts, follow promising links, and synthesize findings. ASYNC: returns a task_id immediately.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "The research question or topic."},
         "start_urls": {"type": "array", "items": {"type": "string"}, "description": "URLs to start from.", "default": []},
         "max_pages": {"type": "integer", "description": "Max pages to visit (1-10).", "default": 5},
         "model": {"type": "string", "description": "Override default model.", "default": None},
     }, "required": ["query"]}},
    {"name": "crawl",
     "description": "Only invoke when the user says 'subagent' in their message. Multi-page browser tool with 3 modes: 'extract' (structured data from each page), 'journey' (walk a user flow step-by-step), 'audit' (check pages for issues). ASYNC: returns a task_id immediately.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "Starting URL (or first step URL for journey mode)."},
         "mode": {"type": "string", "description": "Crawl mode: 'extract' (default, structured extraction per page), 'journey' (sequential user flow walkthrough), 'audit' (quality/consistency check).", "default": "extract", "enum": ["extract", "journey", "audit"]},
         "extract_schema": {"type": "string", "description": "For extract mode: what to pull from each page. For journey: evaluation criteria. For audit: what to check.", "default": ""},
         "link_pattern": {"type": "string", "description": "Extract mode: regex/keyword to filter links. Journey mode: ignored. Audit mode: scope filter.", "default": ""},
         "steps": {"type": "array", "items": {"type": "string"}, "description": "Journey mode only: ordered list of actions/goals for each step (e.g. ['Click Login', 'Fill username field', 'Submit form']).", "default": []},
         "max_pages": {"type": "integer", "description": "Max pages to visit (1-20).", "default": 10},
         "same_origin": {"type": "boolean", "description": "Only follow links on the same domain.", "default": True},
         "model": {"type": "string", "description": "Override default model.", "default": None},
     }, "required": ["url"]}},
    {"name": "compare",
     "description": "Only invoke when the user says 'subagent' in their message. Compare two images or two text blocks and return a structured diff. Use for visual regression, before/after checks, or text comparison.",
     "inputSchema": {"type": "object", "properties": {
         "image_a_b64": {"type": "string", "description": "Base64-encoded first image (for visual diff).", "default": None},
         "image_b_b64": {"type": "string", "description": "Base64-encoded second image (for visual diff).", "default": None},
         "text_a": {"type": "string", "description": "First text block (for text diff).", "default": None},
         "text_b": {"type": "string", "description": "Second text block (for text diff).", "default": None},
         "focus": {"type": "string", "description": "What to focus on: layout, data, style, content, or all.", "default": "all"},
         "context": {"type": "string", "description": "Optional context about what these are (e.g. 'dashboard before/after deploy').", "default": ""},
         "model": {"type": "string", "description": "Override default model.", "default": None},
     }, "required": []}},
    {"name": "fetch",
     "description": "Only invoke when the user says 'subagent' in their message. Make an HTTP request to any URL and return the response. Use for testing APIs, checking endpoints, or retrieving data from URLs.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string", "description": "The URL to request."},
         "method": {"type": "string", "description": "HTTP method.", "default": "GET", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]},
         "headers": {"type": "object", "description": "Optional request headers as key-value pairs.", "default": None},
         "body": {"type": "string", "description": "Optional request body (for POST/PUT/PATCH).", "default": None},
         "timeout": {"type": "integer", "description": "Request timeout in seconds.", "default": 30},
     }, "required": ["url"]}},
    {"name": "search",
     "description": "Only invoke when the user says 'subagent' in their message. Search the web for information. Uses model's native web search grounding when available, falls back to browser-based search. ASYNC: returns a task_id immediately.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "The search query."},
         "num_results": {"type": "integer", "description": "Desired number of results (best effort).", "default": 5},
         "model": {"type": "string", "description": "Override model for search. Models with native search grounding work best.", "default": None},
     }, "required": ["query"]}},
    {"name": "get_task_result",
     "description": "Only invoke when the user says 'subagent' in their message. Retrieve async task results. With task_id: get one task. Without task_id: bulk status of all tasks (say 'subagent update').",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string", "description": "A specific task ID. If omitted, returns status of ALL tasks.", "default": None},
         "include_results": {"type": "boolean", "description": "For bulk mode: include full result data (true) or just previews (false).", "default": True},
     }, "required": []}},
]


# ============================================================
# TOOL IMPLEMENTATIONS
# ============================================================

async def tool_status(**kwargs) -> str:
    """Combined health + model registry + task summary."""
    import pathlib
    _cleanup_old_tasks()
    bundled_libs = pathlib.Path(_bundled_libs)
    chromium_bin = pathlib.Path("/home/app/.cache/ms-playwright/chromium-1105/chrome-linux/chrome")
    running = [t for t in _tasks.values() if t["status"] == "running"]
    done = [t for t in _tasks.values() if t["status"] == "done"]
    errored = [t for t in _tasks.values() if t["status"] == "error"]

    models = []
    for key, config in MODEL_REGISTRY.items():
        roles = [r for r, m in DEFAULT_ROUTING.items() if m == key]
        models.append({
            "key": key, "endpoint": config["endpoint"], "provider": config["provider"],
            "vision": config["vision"], "thinking": config.get("thinking", False),
            "speed": config["speed"], "cost_tier": config["cost_tier"],
            "concurrency_limit": config["concurrency_limit"],
            "default_for_roles": roles,
        })

    return json.dumps({
        "ok": True, "server": SERVER_NAME, "version": SERVER_VERSION,
        "host_set": bool(os.environ.get("DATABRICKS_HOST")),
        "sp_credentials": bool(os.environ.get("DATABRICKS_CLIENT_ID")),
        "chromium_installed": chromium_bin.exists(),
        "bundled_libs_exists": bundled_libs.exists(),
        "models": models,
        "routing": DEFAULT_ROUTING,
        "tasks": {"running": len(running), "done": len(done), "errored": len(errored), "total": len(_tasks)},
    })


async def tool_analyze(prompt: str, system_prompt: str = "", model: str = None, max_tokens: int = 1024, **kw) -> str:
    max_tokens = max(256, min(max_tokens, SYNC_MAX_TOKENS_CAP))
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    result = await _call_model("analyze", messages, max_tokens=max_tokens, override=model)
    if result.get("error"):
        return json.dumps({"error": result["error"], "model": result.get("model")})
    return result["text"]


async def tool_vision(image_base64: str, prompt: str, mime_type: str = "image/png",
                      system_prompt: str = "", model: str = None, max_tokens: int = 1024, **kw) -> str:
    if mime_type not in ALLOWED_IMAGE_MIMES:
        return json.dumps({"error": f"Unsupported MIME: {mime_type}"})
    if ";base64," in image_base64:
        image_base64 = image_base64.split(";base64,", 1)[1]
    max_tokens = max(256, min(max_tokens, SYNC_MAX_TOKENS_CAP))
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}},
    ]})
    result = await _call_model("vision", messages, max_tokens=max_tokens, override=model)
    if result.get("error"):
        return json.dumps({"error": result["error"], "model": result.get("model")})
    return result["text"]


async def tool_fan_out(prompts: list, system_prompt: str = "", model: str = None, max_tokens: int = 1024, **kw) -> str:
    if len(prompts) > MAX_FAN_OUT:
        return json.dumps({"error": f"Max {MAX_FAN_OUT} prompts."})
    if not prompts:
        return json.dumps({"error": "No prompts provided."})
    max_tokens = max(256, min(max_tokens, SYNC_MAX_TOKENS_CAP))

    async def _one(idx, p):
        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": p})
        r = await _call_model("fan_out", msgs, max_tokens=max_tokens, override=model)
        return {"index": idx, "prompt_preview": p[:80], **r}

    results = await asyncio.gather(*[_one(i, p) for i, p in enumerate(prompts)], return_exceptions=True)
    output = []
    for i, r in enumerate(results):
        output.append({"index": i, "error": str(r)} if isinstance(r, Exception) else r)
    return json.dumps(output, indent=2)


async def tool_structured_extract(text: str, schema_json: str, extraction_prompt: str = "",
                                   model: str = None, max_tokens: int = 1024, **kw) -> str:
    max_tokens = max(256, min(max_tokens, SYNC_MAX_TOKENS_CAP))
    try:
        schema = json.loads(schema_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid schema JSON: {e}"})
    system = "You are a structured data extraction engine. Return ONLY valid JSON matching the schema. No explanation, no markdown."
    if extraction_prompt:
        system += f"\n\nAdditional: {extraction_prompt}"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Schema:\n{json.dumps(schema, indent=2)}\n\nText:\n{text}"},
    ]
    result = await _call_model("structured_extract", messages, max_tokens=max_tokens, override=model)
    if result.get("error"):
        return json.dumps({"error": result["error"]})
    raw = result["text"].strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        return json.dumps(json.loads(raw), indent=2)
    except json.JSONDecodeError:
        return json.dumps({"warning": "Not valid JSON", "raw_text": raw})


# --- ASYNC TOOL WORKERS ---

async def _do_inspect_page(task_id: str, url: str, question: str, user_token: str, model_override: str | None):
    try:
        async def _inspect(page: Page):
            aria = await _get_aria_tree(page)
            screenshot_b64 = await _take_screenshot_b64(page)
            prompt_text = f"Describe this web page.\nURL: {page.url}\n"
            if question:
                prompt_text += f"\nSpecifically: {question}\n"
            prompt_text += f"\nARIA Tree:\n{aria[:4000]}"
            messages = [
                {"role": "system", "content": "You are a web page analyst. Describe the page content concisely."},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
                ]},
            ]
            summary_result = await _call_model("browser_summarize", messages, max_tokens=1024, override=model_override)
            return json.dumps({
                "url": page.url, "title": await page.title(),
                "summary": summary_result.get("text", ""),
                "aria_tree": aria[:6000], "screenshot_b64": screenshot_b64,
                "model": summary_result.get("model", ""),
            })
        result = await _run_browser_session(url, user_token, _inspect)
        _complete_task(task_id, result)
    except Exception as e:
        _fail_task(task_id, str(e))


async def _do_browser_task(task_id: str, url: str, goal: str, max_rounds: int, user_token: str, model_override: str | None):
    try:
        async def _task(page: Page):
            return json.dumps(await _browser_plan_and_execute(page, goal, max_rounds, model_override))
        result = await _run_browser_session(url, user_token, _task)
        _complete_task(task_id, result)
    except Exception as e:
        _fail_task(task_id, str(e))


async def _do_render_html(task_id: str, html: str, css: str, js: str,
                          vw: int, vh: int, wait_ms: int, summarize: bool, model_override: str | None):
    try:
        await _ensure_browser_installed()
        if "<html" not in html.lower() and "<body" not in html.lower():
            style = f"<style>{css}</style>" if css else ""
            html = f'<!DOCTYPE html><html><head><meta charset="utf-8">{style}</head><body>{html}</body></html>'
        elif css:
            html = html.replace("</head>", f"<style>{css}</style></head>", 1)

        _old_ld = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = f"{_bundled_libs}:{_old_ld}" if _old_ld else _bundled_libs
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
                context = await browser.new_context(viewport={"width": vw, "height": vh})
                page = await context.new_page()
                try:
                    await page.set_content(html, wait_until="domcontentloaded")
                    if js:
                        await page.evaluate(js)
                    await page.wait_for_timeout(min(max(wait_ms, 100), 5000))
                    aria = await _get_aria_tree(page)
                    screenshot_b64 = await _take_screenshot_b64(page)
                    result = {"rendered": True, "viewport": f"{vw}x{vh}",
                              "aria_tree": aria[:6000], "screenshot_b64": screenshot_b64}
                    if summarize:
                        messages = [
                            {"role": "system", "content": "Describe the rendered UI concisely."},
                            {"role": "user", "content": [
                                {"type": "text", "text": f"ARIA:\n{aria[:4000]}"},
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
                            ]},
                        ]
                        sr = await _call_model("browser_summarize", messages, max_tokens=512, override=model_override)
                        result["summary"] = sr.get("text", "")
                        if sr.get("error"):
                            result["summary_error"] = sr["error"]
                    _complete_task(task_id, json.dumps(result))
                finally:
                    await context.close()
                    await browser.close()
        finally:
            if _old_ld:
                os.environ["LD_LIBRARY_PATH"] = _old_ld
            else:
                os.environ.pop("LD_LIBRARY_PATH", None)
    except Exception as e:
        _fail_task(task_id, str(e))


async def _do_debate(task_id: str, prompt: str, system_prompt: str,
                     generator_model: str | None, critic_model: str | None, max_tokens: int):
    """Debate: generate -> critique -> conditional refine. Fixed 1 round, max 2."""
    try:
        total_tokens = 0
        budget = max_tokens * 4

        # Stage 1: Generate
        gen_messages = []
        if system_prompt:
            gen_messages.append({"role": "system", "content": system_prompt})
        else:
            gen_messages.append({"role": "system", "content": "Generate a thorough, specific response."})
        gen_messages.append({"role": "user", "content": prompt})

        gen_result = await _call_model("debate_generator", gen_messages, max_tokens=max_tokens, override=generator_model)
        total_tokens += gen_result.get("usage", {}).get("total_tokens", 0)
        if gen_result.get("error"):
            _fail_task(task_id, f"Generator failed: {gen_result['error']}")
            return

        generation = gen_result["text"]

        # Stage 2: Critique
        critic_system = """You are a critical reviewer. Given an original request and a response:
- Identify factual errors, logical gaps, or missing context
- Rate overall quality (1-5)
- If the response is good, output ONLY: "NO_SUBSTANTIVE_ISSUES: [brief praise]"
- If issues exist, list them concisely with suggested fixes
Be constructive. Focus on substantive issues only."""

        critic_messages = [
            {"role": "system", "content": critic_system},
            {"role": "user", "content": f"ORIGINAL REQUEST:\n{prompt}\n\nRESPONSE TO REVIEW:\n{generation}"},
        ]
        critic_result = await _call_model("debate_critic", critic_messages, max_tokens=max_tokens, override=critic_model)
        total_tokens += critic_result.get("usage", {}).get("total_tokens", 0)

        critique = critic_result.get("text", "") if not critic_result.get("error") else f"Critic error: {critic_result['error']}"

        # Stage 3: Conditional refinement
        refinement = None
        skip_reason = None
        if "NO_SUBSTANTIVE_ISSUES" in critique:
            skip_reason = "no substantive issues"
        elif critic_result.get("error"):
            skip_reason = "critic failed"
        elif total_tokens >= budget:
            skip_reason = "budget exhausted"
        else:
            refine_messages = list(gen_messages)
            refine_messages.append({"role": "assistant", "content": generation})
            refine_messages.append({"role": "user", "content": f"A reviewer found these issues. Please revise:\n\n{critique}"})
            refine_result = await _call_model("debate_generator", refine_messages, max_tokens=max_tokens, override=generator_model)
            total_tokens += refine_result.get("usage", {}).get("total_tokens", 0)
            refinement = refine_result.get("text", "") if not refine_result.get("error") else None
            if refine_result.get("error"):
                skip_reason = "refinement failed"

        output = {
            "stages": {
                "generation": {"text": generation, "model": gen_result.get("model")},
                "critique": {"text": critique, "model": critic_result.get("model"),
                             "has_issues": "NO_SUBSTANTIVE_ISSUES" not in critique},
                "refinement": {"text": refinement, "model": gen_result.get("model"),
                               "skipped": refinement is None, "reason": skip_reason},
            },
            "total_tokens": total_tokens,
            "recommended": refinement if refinement else generation,
        }
        _complete_task(task_id, json.dumps(output, indent=2),
                       model_used=f"{gen_result.get('model')}+{critic_result.get('model')}", tokens=total_tokens)
    except Exception as e:
        _fail_task(task_id, str(e))


# --- ASYNC TOOL ENTRY POINTS ---

async def tool_inspect_page(url: str, question: str = "", model: str = None, **kw) -> str:
    user_token = _current_user_token.get()
    task_id = _create_task("inspect_page", url[:80])
    asyncio.create_task(_do_inspect_page(task_id, url, question, user_token, model))
    return json.dumps({"task_id": task_id, "status": "running", "tool": "inspect_page"})


async def tool_browser_task(url: str, goal: str, max_rounds: int = 3, model: str = None, **kw) -> str:
    user_token = _current_user_token.get()
    max_rounds = min(max(1, max_rounds), MAX_BROWSER_ROUNDS)
    task_id = _create_task("browser_task", f"{url[:40]} | {goal[:40]}")
    asyncio.create_task(_do_browser_task(task_id, url, goal, max_rounds, user_token, model))
    return json.dumps({"task_id": task_id, "status": "running", "tool": "browser_task"})


async def tool_render_html(html: str, css: str = "", js: str = "",
                           viewport_width: int = 1280, viewport_height: int = 900,
                           wait_ms: int = 500, summarize: bool = True, model: str = None, **kw) -> str:
    task_id = _create_task("render_html", f"html[{len(html)}chars] summarize={summarize}")
    asyncio.create_task(_do_render_html(task_id, html, css, js, viewport_width, viewport_height, wait_ms, summarize, model))
    return json.dumps({"task_id": task_id, "status": "running", "tool": "render_html"})


async def tool_debate(prompt: str, system_prompt: str = "",
                      generator_model: str = None, critic_model: str = None,
                      max_tokens: int = 2048, **kw) -> str:
    task_id = _create_task("debate", prompt[:60])
    asyncio.create_task(_do_debate(task_id, prompt, system_prompt, generator_model, critic_model, max_tokens))
    return json.dumps({"task_id": task_id, "status": "running", "tool": "debate"})



# --- RESEARCH CRAWL TOOL ---

async def _research_extract_page(url, query, model_override, user_token):
    """Visit one page, extract facts and links."""
    try:
        async def _extract_fn(page):
            aria = await _get_aria_tree(page)
            title = await page.title()
            page_url = page.url

            prompt_parts = [
                "You are a research assistant. Extract relevant info from this page.",
                "",
                "QUERY: " + query,
                "URL: " + page_url,
                "TITLE: " + title,
                "",
                "Return JSON with: facts (array of strings), relevant_links (up to 3 URLs to follow), relevance (high/medium/low).",
                "Return ONLY valid JSON. Example:",
                '{"facts": ["fact1"], "relevant_links": ["https://..."], "relevance": "high"}',
                "",
                "ARIA Tree (first 6000 chars):",
                aria[:6000],
            ]
            prompt_text = "\n".join(prompt_parts)

            messages = [
                {"role": "system", "content": "Extract research findings. Return ONLY valid JSON."},
                {"role": "user", "content": prompt_text},
            ]
            result = await _call_model("analyze", messages, max_tokens=1024, override=model_override)

            output = {"url": page_url, "title": title, "facts": [], "relevant_links": [], "tokens": 0}
            output["tokens"] = result.get("usage", {}).get("total_tokens", 0)

            if not result.get("error"):
                raw = result["text"].strip()
                if raw.startswith("```"):
                    lines = raw.split("\n")
                    raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
                try:
                    extracted = json.loads(raw)
                    output["facts"] = extracted.get("facts", [])
                    output["relevant_links"] = extracted.get("relevant_links", [])[:3]
                    output["relevance"] = extracted.get("relevance", "unknown")
                except json.JSONDecodeError:
                    output["facts"] = [raw[:300]]

            return json.dumps(output)

        result_str = await _run_browser_session(url, user_token, _extract_fn)
        return json.loads(result_str)
    except Exception as e:
        return {"url": url, "error": str(e), "facts": [], "relevant_links": [], "tokens": 0}


async def _do_research(task_id, query, start_urls, max_pages, model_override, user_token):
    """Multi-page research: visit pages, extract, follow links, synthesize."""
    try:
        pages_visited = []
        urls_queue = list(start_urls) if start_urls else []
        urls_seen = set()
        total_tokens = 0

        # If no start URLs, generate search URL
        if not urls_queue:
            urls_queue = ["https://www.google.com/search?q=" + query.replace(" ", "+")]

        # Visit pages breadth-first
        page_count = 0
        while urls_queue and page_count < max_pages:
            url = urls_queue.pop(0)
            if url in urls_seen:
                continue
            urls_seen.add(url)
            page_count += 1

            page_data = await _research_extract_page(url, query, model_override, user_token)
            total_tokens += page_data.get("tokens", 0)
            pages_visited.append(page_data)

            # Queue discovered links
            if page_count < max_pages:
                for link in page_data.get("relevant_links", []):
                    if link not in urls_seen and link.startswith("http"):
                        urls_queue.append(link)

        # Synthesize
        all_facts = []
        for p in pages_visited:
            if p.get("facts"):
                source = p.get("title") or p.get("url", "Unknown")
                for fact in p["facts"]:
                    all_facts.append(source + ": " + fact)

        synthesis_text = ""
        if all_facts:
            synth_parts = [
                "Synthesize these research findings into a clear answer.",
                "",
                "QUERY: " + query,
                "",
                "FINDINGS:",
            ]
            for f in all_facts[:30]:
                synth_parts.append("- " + f)
            synth_parts.append("")
            synth_parts.append("Provide a structured synthesis with citations.")

            messages = [
                {"role": "system", "content": "Synthesize research findings clearly."},
                {"role": "user", "content": "\n".join(synth_parts)},
            ]
            synth_result = await _call_model("analyze", messages, max_tokens=2048, override=model_override)
            total_tokens += synth_result.get("usage", {}).get("total_tokens", 0)
            synthesis_text = synth_result.get("text", "") if not synth_result.get("error") else ""

        output = {
            "query": query,
            "pages_visited": len(pages_visited),
            "pages": [{"url": p.get("url", ""), "title": p.get("title", ""),
                       "facts": p.get("facts", []), "error": p.get("error")}
                      for p in pages_visited],
            "synthesis": synthesis_text,
            "total_tokens": total_tokens,
        }
        _complete_task(task_id, json.dumps(output, indent=2),
                       model_used=model_override or "gemini-flash", tokens=total_tokens)
    except Exception as e:
        _fail_task(task_id, str(e))


async def tool_research(query, start_urls=None, max_pages=5, model=None, **kw):
    user_token = _current_user_token.get()
    max_pages = min(max(1, max_pages), 10)
    start_urls = start_urls or []
    task_id = _create_task("research", query[:60])
    asyncio.create_task(_do_research(task_id, query, start_urls, max_pages, model, user_token))
    return json.dumps({"task_id": task_id, "status": "running", "tool": "research"})



# --- CRAWL TOOL (STRUCTURED EXTRACTION) ---

async def _crawl_extract_page(url, extract_schema, model_override, user_token):
    """Visit one page and extract structured data per the schema."""
    try:
        async def _extract_fn(page):
            aria = await _get_aria_tree(page)
            title = await page.title()
            page_url = page.url

            prompt_parts = [
                "You are a structured data extraction agent.",
                "Extract data from this page according to the schema below.",
                "",
                "EXTRACT SCHEMA: " + extract_schema,
                "",
                "URL: " + page_url,
                "TITLE: " + title,
                "",
                "Return JSON with exactly two keys:",
                '  "data": the extracted object (or array of objects if multiple items on page)',
                '  "links": array of ALL href URLs found on this page (max 50)',
                "",
                "Return ONLY valid JSON. If the page has no matching data, return:",
                '{"data": null, "links": [...]}',
                "",
                "ARIA Tree (first 7000 chars):",
                aria[:7000],
            ]
            prompt_text = "\n".join(prompt_parts)

            messages = [
                {"role": "system", "content": "Extract structured data. Return ONLY valid JSON."},
                {"role": "user", "content": prompt_text},
            ]
            result = await _call_model("analyze", messages, max_tokens=2048, override=model_override)

            output = {"url": page_url, "title": title, "data": None, "links": [], "tokens": 0}
            output["tokens"] = result.get("usage", {}).get("total_tokens", 0)

            if not result.get("error"):
                raw = result["text"].strip()
                if raw.startswith("```"):
                    lines = raw.split("\n")
                    raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
                try:
                    extracted = json.loads(raw)
                    output["data"] = extracted.get("data")
                    output["links"] = extracted.get("links", [])[:50]
                except json.JSONDecodeError:
                    output["error"] = "Failed to parse extraction result"

            return json.dumps(output)

        result_str = await _run_browser_session(url, user_token, _extract_fn)
        return json.loads(result_str)
    except Exception as e:
        return {"url": url, "error": str(e), "data": None, "links": [], "tokens": 0}


def _link_matches_pattern(link, pattern, base_url):
    """Check if a link matches the crawl pattern."""
    if not pattern:
        return True
    try:
        import re as _re
        if _re.search(pattern, link, _re.IGNORECASE):
            return True
    except Exception:
        # Fallback: simple substring match
        if pattern.lower() in link.lower():
            return True
    return False


def _is_same_origin(link, base_url):
    """Check if link is on the same domain as base_url."""
    try:
        from urllib.parse import urlparse
        base_host = urlparse(base_url).netloc
        link_host = urlparse(link).netloc
        return link_host == base_host
    except Exception:
        return False


async def _do_crawl(task_id, start_url, link_pattern, extract_schema,
                    max_pages, same_origin, model_override, user_token):
    """Structured crawl: spider pages matching pattern, extract same schema from each."""
    try:
        pages_data = []
        urls_queue = [start_url]
        urls_seen = set()
        total_tokens = 0

        page_count = 0
        while urls_queue and page_count < max_pages:
            url = urls_queue.pop(0)
            if url in urls_seen:
                continue
            urls_seen.add(url)
            page_count += 1

            page_result = await _crawl_extract_page(url, extract_schema, model_override, user_token)
            total_tokens += page_result.get("tokens", 0)
            pages_data.append(page_result)

            # Queue discovered links that match the pattern
            if page_count < max_pages:
                for link in page_result.get("links", []):
                    if link in urls_seen or not link.startswith("http"):
                        continue
                    if same_origin and not _is_same_origin(link, start_url):
                        continue
                    if not _link_matches_pattern(link, link_pattern, start_url):
                        continue
                    urls_queue.append(link)

        # Build output — uniform structured results
        output = {
            "start_url": start_url,
            "link_pattern": link_pattern,
            "extract_schema": extract_schema,
            "pages_crawled": len(pages_data),
            "results": [{"url": p.get("url", ""), "title": p.get("title", ""),
                         "data": p.get("data"), "error": p.get("error")}
                        for p in pages_data],
            "total_tokens": total_tokens,
        }
        _complete_task(task_id, json.dumps(output, indent=2),
                       model_used=model_override or "gemini-flash", tokens=total_tokens)
    except Exception as e:
        _fail_task(task_id, str(e))


async def tool_crawl(url, mode="extract", extract_schema="", link_pattern="",
                     steps=None, max_pages=10, same_origin=True, model=None, **kw):
    user_token = _current_user_token.get()
    max_pages = min(max(1, max_pages), 20)
    steps = steps or []

    if mode == "journey":
        summary = "journey: " + url[:30] + " (" + str(len(steps)) + " steps)"
        task_id = _create_task("crawl", summary)
        asyncio.create_task(_do_journey(task_id, url, steps, extract_schema, model, user_token))
    elif mode == "audit":
        summary = "audit: " + url[:40]
        task_id = _create_task("crawl", summary)
        asyncio.create_task(_do_audit(task_id, url, extract_schema, link_pattern,
                                      max_pages, same_origin, model, user_token))
    else:  # extract (default)
        summary = url[:40] + " | " + extract_schema[:30]
        task_id = _create_task("crawl", summary)
        asyncio.create_task(_do_crawl(task_id, url, link_pattern, extract_schema,
                                      max_pages, same_origin, model, user_token))
    return json.dumps({"task_id": task_id, "status": "running", "tool": "crawl", "mode": mode})



# --- JOURNEY MODE (sequential user flow) ---

async def _do_journey(task_id, start_url, steps, criteria, model_override, user_token):
    """Walk a user flow step-by-step, evaluating each step."""
    try:
        if not steps:
            _fail_task(task_id, "Journey mode requires 'steps' array (list of actions/goals).")
            return

        step_results = []
        total_tokens = 0

        async def _journey_fn(page):
            nonlocal total_tokens
            results_inner = []

            for i, step_goal in enumerate(steps):
                step_num = i + 1
                # Execute this step using browser automation
                step_result = await _browser_plan_and_execute(page, step_goal, max_rounds=3, model_override=model_override)

                # Evaluate the result
                aria = await _get_aria_tree(page)
                title = await page.title()
                current_url = page.url

                eval_parts = [
                    "Evaluate this step in a user journey.",
                    "",
                    "STEP " + str(step_num) + "/" + str(len(steps)) + ": " + step_goal,
                    "CURRENT URL: " + current_url,
                    "PAGE TITLE: " + title,
                    "STEP OUTCOME: " + ("Success" if step_result.get("done") else "Incomplete"),
                    "STEP RESULT: " + str(step_result.get("result", ""))[:300],
                ]
                if criteria:
                    eval_parts.append("")
                    eval_parts.append("EVALUATION CRITERIA: " + criteria)
                eval_parts.extend([
                    "",
                    "Return JSON:",
                    '{"success": true/false, "observation": "what happened", "issues": ["any UX issues"], "screenshot_description": "brief description of current state"}',
                    "",
                    "ARIA Tree (first 4000 chars):",
                    aria[:4000],
                ])

                messages = [
                    {"role": "system", "content": "Evaluate user journey steps. Return ONLY valid JSON."},
                    {"role": "user", "content": "\n".join(eval_parts)},
                ]
                eval_result = await _call_model("analyze", messages, max_tokens=512, override=model_override)
                total_tokens += eval_result.get("usage", {}).get("total_tokens", 0)

                evaluation = {}
                if not eval_result.get("error"):
                    raw = eval_result["text"].strip()
                    if raw.startswith("```"):
                        lines = raw.split("\n")
                        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
                    try:
                        evaluation = json.loads(raw)
                    except json.JSONDecodeError:
                        evaluation = {"observation": raw[:200]}

                results_inner.append({
                    "step": step_num,
                    "goal": step_goal,
                    "url": current_url,
                    "title": title,
                    "completed": step_result.get("done", False),
                    "evaluation": evaluation,
                })

            return json.dumps(results_inner)

        result_str = await _run_browser_session(start_url, user_token, _journey_fn)
        step_results = json.loads(result_str)

        output = {
            "mode": "journey",
            "start_url": start_url,
            "steps_total": len(steps),
            "steps_completed": sum(1 for s in step_results if s.get("completed")),
            "results": step_results,
            "total_tokens": total_tokens,
        }
        _complete_task(task_id, json.dumps(output, indent=2),
                       model_used=model_override or "gemini-flash", tokens=total_tokens)
    except Exception as e:
        _fail_task(task_id, str(e))


# --- AUDIT MODE (quality/consistency check) ---

async def _do_audit(task_id, start_url, checks_description, link_pattern,
                    max_pages, same_origin, model_override, user_token):
    """Visit pages and check for quality issues."""
    try:
        if not checks_description:
            checks_description = "broken links, missing images, accessibility issues, inconsistent styling, stale content"

        pages_audited = []
        urls_queue = [start_url]
        urls_seen = set()
        total_tokens = 0
        all_issues = []

        page_count = 0
        while urls_queue and page_count < max_pages:
            url = urls_queue.pop(0)
            if url in urls_seen:
                continue
            urls_seen.add(url)
            page_count += 1

            try:
                page_result = await _audit_single_page(url, checks_description, model_override, user_token)
                total_tokens += page_result.get("tokens", 0)
                pages_audited.append(page_result)

                # Collect issues
                for issue in page_result.get("issues", []):
                    all_issues.append({"url": url, "issue": issue})

                # Queue links for more auditing
                if page_count < max_pages:
                    for link in page_result.get("links", []):
                        if link in urls_seen or not link.startswith("http"):
                            continue
                        if same_origin and not _is_same_origin(link, start_url):
                            continue
                        if link_pattern and not _link_matches_pattern(link, link_pattern, start_url):
                            continue
                        urls_queue.append(link)
            except Exception as e:
                pages_audited.append({"url": url, "error": str(e), "issues": []})

        output = {
            "mode": "audit",
            "start_url": start_url,
            "checks": checks_description,
            "pages_audited": len(pages_audited),
            "total_issues": len(all_issues),
            "issues": all_issues,
            "pages": [{"url": p.get("url", ""), "title": p.get("title", ""),
                       "score": p.get("score"), "issues_count": len(p.get("issues", []))}
                      for p in pages_audited],
            "total_tokens": total_tokens,
        }
        _complete_task(task_id, json.dumps(output, indent=2),
                       model_used=model_override or "gemini-flash", tokens=total_tokens)
    except Exception as e:
        _fail_task(task_id, str(e))


async def _audit_single_page(url, checks_description, model_override, user_token):
    """Audit a single page for quality issues."""
    try:
        async def _audit_fn(page):
            aria = await _get_aria_tree(page)
            title = await page.title()
            page_url = page.url

            audit_parts = [
                "You are a web quality auditor. Check this page for issues.",
                "",
                "CHECKS TO PERFORM: " + checks_description,
                "URL: " + page_url,
                "TITLE: " + title,
                "",
                "Return JSON:",
                '{"issues": ["issue 1", "issue 2"], "score": 1-10, "links": ["url1", "url2"], "summary": "brief assessment"}',
                "",
                "issues: specific problems found (empty array if none)",
                "score: 1 (terrible) to 10 (perfect)",
                "links: page links to audit next (max 5)",
                "summary: one-line assessment",
                "",
                "ARIA Tree (first 7000 chars):",
                aria[:7000],
            ]

            messages = [
                {"role": "system", "content": "Audit web pages for quality issues. Return ONLY valid JSON."},
                {"role": "user", "content": "\n".join(audit_parts)},
            ]
            result = await _call_model("analyze", messages, max_tokens=1024, override=model_override)

            output = {"url": page_url, "title": title, "issues": [], "links": [], "score": None, "tokens": 0}
            output["tokens"] = result.get("usage", {}).get("total_tokens", 0)

            if not result.get("error"):
                raw = result["text"].strip()
                if raw.startswith("```"):
                    lines = raw.split("\n")
                    raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
                try:
                    extracted = json.loads(raw)
                    output["issues"] = extracted.get("issues", [])
                    output["links"] = extracted.get("links", [])[:5]
                    output["score"] = extracted.get("score")
                    output["summary"] = extracted.get("summary", "")
                except json.JSONDecodeError:
                    output["issues"] = [raw[:300]]

            return json.dumps(output)

        result_str = await _run_browser_session(url, user_token, _audit_fn)
        return json.loads(result_str)
    except Exception as e:
        return {"url": url, "error": str(e), "issues": [], "links": [], "tokens": 0}


# --- COMPARE TOOL ---

async def tool_compare(image_a_b64: str = None, image_b_b64: str = None,
                       text_a: str = None, text_b: str = None,
                       focus: str = "all", context: str = "", model: str = None, **kw) -> str:
    """Visual or textual diff between two inputs."""
    is_visual = bool(image_a_b64 and image_b_b64)
    is_text = bool(text_a is not None and text_b is not None)

    if not is_visual and not is_text:
        return json.dumps({"error": "Provide either (image_a_b64 + image_b_b64) for visual diff, or (text_a + text_b) for text diff."})

    focus_instruction = {
        "layout": "Focus on layout changes: positioning, spacing, alignment, size differences.",
        "data": "Focus on data changes: numbers, values, labels, chart data that differ.",
        "style": "Focus on style changes: colors, fonts, borders, backgrounds, visual treatment.",
        "content": "Focus on content changes: text additions, removals, wording differences.",
        "all": "Analyze all aspects: layout, data, style, and content differences.",
    }.get(focus, "Analyze all aspects of difference.")

    context_line = f"\nContext: {context}" if context else ""

    system = f"""You are a precise diff analyst. Compare the two inputs (A and B) and produce a structured diff.{context_line}

{focus_instruction}

Return valid JSON with this structure:
{{
  "summary": "one-sentence overview of the key difference",
  "similarity_pct": 0-100,
  "changes": [
    {{"category": "added|removed|changed|unchanged", "element": "what changed", "details": "description", "severity": "minor|moderate|major"}}
  ],
  "recommendation": "brief assessment of whether changes are intentional/expected or potentially problematic"
}}

Be specific. Reference exact elements, positions, values. If inputs are identical, say so."""

    if is_visual:
        # Strip data URI prefix if present
        if ";base64," in image_a_b64:
            image_a_b64 = image_a_b64.split(";base64,", 1)[1]
        if ";base64," in image_b_b64:
            image_b_b64 = image_b_b64.split(";base64,", 1)[1]

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": "Compare Image A (first) with Image B (second). What changed?"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_a_b64}"}},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b_b64}"}},
            ]},
        ]
    else:
        # Text diff
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Compare Text A with Text B. What changed?\n\n--- TEXT A ---\n{text_a[:20000]}\n\n--- TEXT B ---\n{text_b[:20000]}"},
        ]

    result = await _call_model("vision" if is_visual else "analyze", messages, max_tokens=2048, override=model)
    if result.get("error"):
        return json.dumps({"error": result["error"], "model": result.get("model")})

    raw = result["text"].strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
    try:
        parsed = json.loads(raw)
        parsed["model"] = result.get("model")
        parsed["mode"] = "visual" if is_visual else "text"
        parsed["focus"] = focus
        return json.dumps(parsed, indent=2)
    except json.JSONDecodeError:
        return json.dumps({"raw_analysis": raw, "model": result.get("model"), "mode": "visual" if is_visual else "text"})



# --- SEARCH TOOL ---

async def _do_search(task_id: str, query: str, num_results: int, model_override: str | None, user_token: str):
    """Web search: try native grounding first, fallback to browser-based search."""
    try:
        # Strategy 1: Use model with native web search grounding
        # Gemini and GPT support grounding/search via their native capabilities
        # We ask the model to search and synthesize — if the endpoint supports grounding,
        # the response will include real web data
        search_system = """You have access to web search. Search for the query and provide:
1. A list of the top results with title, URL, and a brief snippet
2. A synthesis paragraph summarizing the key findings

Format your response as JSON:
{
  "results": [{"title": "...", "url": "...", "snippet": "..."}],
  "synthesis": "...",
  "sources_used": true
}

If you cannot actually search the web (no grounding available), respond with:
{"results": [], "synthesis": "", "sources_used": false, "error": "no_grounding"}"""

        messages = [
            {"role": "system", "content": search_system},
            {"role": "user", "content": f"Search for: {query}\n\nReturn up to {num_results} results."},
        ]
        result = await _call_model("analyze", messages, max_tokens=2048, override=model_override)

        if result.get("error"):
            # Strategy 2: Fallback to browser-based search
            await _browser_search_fallback(task_id, query, num_results, user_token, model_override)
            return

        raw = result["text"].strip()
        # Try to parse JSON response
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
        try:
            parsed = json.loads(raw)
            if parsed.get("sources_used") == False or parsed.get("error") == "no_grounding":
                # Model couldn't actually search — fallback to browser
                await _browser_search_fallback(task_id, query, num_results, user_token, model_override)
                return
            # Success with native grounding
            parsed["method"] = "native_grounding"
            parsed["model"] = result.get("model")
            parsed["query"] = query
            _complete_task(task_id, json.dumps(parsed, indent=2),
                          model_used=result.get("model", ""), tokens=result.get("usage", {}).get("total_tokens", 0))
        except json.JSONDecodeError:
            # Model returned free text — wrap it
            output = {"query": query, "method": "native_grounding", "model": result.get("model"),
                      "results": [], "synthesis": raw, "sources_used": True}
            _complete_task(task_id, json.dumps(output, indent=2),
                          model_used=result.get("model", ""), tokens=result.get("usage", {}).get("total_tokens", 0))

    except Exception as e:
        _fail_task(task_id, str(e))


async def _browser_search_fallback(task_id: str, query: str, num_results: int, user_token: str, model_override: str | None):
    """Fallback: use browser to search and extract results."""
    try:
        search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}&num={num_results}"

        async def _extract_results(page):
            await page.wait_for_timeout(2000)
            aria = await _get_aria_tree(page)
            screenshot_b64 = await _take_screenshot_b64(page)

            extract_prompt = f"""Extract search results from this Google search results page.
Query: {query}

Return JSON:
{{"results": [{{"title": "...", "url": "...", "snippet": "..."}}], "synthesis": "brief summary of what was found"}}

Look at both the ARIA tree and screenshot. Extract up to {num_results} results."""

            messages = [
                {"role": "system", "content": "You are a search result extractor. Return ONLY valid JSON."},
                {"role": "user", "content": [
                    {"type": "text", "text": f"{extract_prompt}\n\nARIA:\n{aria[:6000]}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
                ]},
            ]
            extract_result = await _call_model("browser_summarize", messages, max_tokens=2048, override=model_override)
            return json.dumps({
                "query": query, "method": "browser_search",
                "model": extract_result.get("model"),
                "raw_extraction": extract_result.get("text", ""),
                "screenshot_b64": screenshot_b64,
            })

        result = await _run_browser_session(search_url, user_token, _extract_results)
        _complete_task(task_id, result)
    except Exception as e:
        _fail_task(task_id, f"Browser search fallback failed: {e}")


async def tool_search(query: str, num_results: int = 5, model: str = None, **kw) -> str:
    user_token = _current_user_token.get()
    num_results = min(max(1, num_results), 10)
    task_id = _create_task("search", query[:60])
    asyncio.create_task(_do_search(task_id, query, num_results, model, user_token))
    return json.dumps({"task_id": task_id, "status": "running", "tool": "search"})



# --- SECURITY BLOCKLIST FOR FETCH ---
_BLOCKED_HOSTS = frozenset(["localhost", "127.0.0.1", "0.0.0.0", "[::1]", "metadata.google.internal"])
_BLOCKED_PREFIXES = ("169.254.", "10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
                     "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.",
                     "172.27.", "172.28.", "172.29.", "172.30.", "172.31.")
_BLOCKED_PATH_PATTERNS = ("/api/2.0/", "/api/2.1/", "/serving-endpoints/")


def _is_url_blocked(url: str) -> str | None:
    """Check if URL should be blocked. Returns reason string or None if allowed."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL"
    host = (parsed.hostname or "").lower()
    if host in _BLOCKED_HOSTS:
        return f"Blocked host: {host}"
    if any(host.startswith(p) for p in _BLOCKED_PREFIXES):
        return f"Blocked private IP: {host}"
    path = parsed.path or ""
    if any(pat in path for pat in _BLOCKED_PATH_PATTERNS):
        return f"Blocked Databricks API path: {path}"
    return None


async def tool_fetch(url: str, method: str = "GET", headers: dict = None,
                     body: str = None, timeout: int = 30, **kw) -> str:
    """HTTP client — make arbitrary requests to external URLs."""
    blocked = _is_url_blocked(url)
    if blocked:
        return json.dumps({"error": blocked, "url": url})

    timeout = min(max(5, timeout), 60)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.request(
                method=method.upper(),
                url=url,
                headers=headers or {},
                content=body.encode() if body else None,
            )
            # Truncate large bodies
            body_text = resp.text
            truncated = len(body_text) > 50000
            if truncated:
                body_text = body_text[:50000]

            # Filter response headers to useful subset
            useful_headers = {k: v for k, v in resp.headers.items()
                             if k.lower() in ("content-type", "content-length", "server",
                                              "x-request-id", "location", "set-cookie",
                                              "cache-control", "etag", "last-modified")}

            return json.dumps({
                "status": resp.status_code,
                "url": str(resp.url),
                "method": method.upper(),
                "headers": useful_headers,
                "body": body_text,
                "body_truncated": truncated,
                "elapsed_ms": round(resp.elapsed.total_seconds() * 1000, 1),
            })
    except httpx.TimeoutException:
        return json.dumps({"error": f"Timeout after {timeout}s", "url": url})
    except httpx.ConnectError as e:
        return json.dumps({"error": f"Connection failed: {e}", "url": url})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}", "url": url})



async def tool_get_task_result(task_id: str = None, include_results: bool = True, **kw) -> str:
    """If task_id given: single task result. If omitted: bulk status of all tasks."""
    _cleanup_old_tasks()

    # --- Single task mode ---
    if task_id:
        task = _tasks.get(task_id)
        if not task:
            return json.dumps({"error": f"Task '{task_id}' not found."})
        if task["status"] == "running":
            return json.dumps({"task_id": task_id, "status": "running", "tool": task["tool"],
                               "elapsed_seconds": round(time.time() - task["started_at"], 1)})
        elif task["status"] == "error":
            return json.dumps({"task_id": task_id, "status": "error", "tool": task["tool"],
                               "error": task["error"],
                               "elapsed_seconds": round((task["finished_at"] or time.time()) - task["started_at"], 1)})
        else:
            elapsed = round(task["finished_at"] - task["started_at"], 1)
            try:
                data = json.loads(task["result"]) if isinstance(task["result"], str) else task["result"]
            except (json.JSONDecodeError, TypeError):
                data = task["result"]
            return json.dumps({"task_id": task_id, "status": "done", "tool": task["tool"],
                               "elapsed_seconds": elapsed, "model_used": task.get("model_used"),
                               "tokens_used": task.get("tokens_used", 0), "data": data})

    # --- Bulk mode (no task_id) ---
    if not _tasks:
        return json.dumps({"message": "No tasks in store.", "running": 0, "done": 0, "errored": 0})

    running = []
    done = []
    errored = []

    for tid, task in _tasks.items():
        entry = {"task_id": tid, "tool": task["tool"], "params_summary": task["params_summary"]}
        if task["status"] == "running":
            entry["elapsed_seconds"] = round(time.time() - task["started_at"], 1)
            running.append(entry)
        elif task["status"] == "error":
            entry["error"] = task["error"]
            entry["elapsed_seconds"] = round((task["finished_at"] or time.time()) - task["started_at"], 1)
            errored.append(entry)
        else:  # done
            entry["elapsed_seconds"] = round(task["finished_at"] - task["started_at"], 1)
            entry["model_used"] = task.get("model_used")
            entry["tokens_used"] = task.get("tokens_used", 0)
            if include_results:
                try:
                    entry["data"] = json.loads(task["result"]) if isinstance(task["result"], str) else task["result"]
                except (json.JSONDecodeError, TypeError):
                    entry["data"] = task["result"]
            else:
                raw = task["result"] if isinstance(task["result"], str) else json.dumps(task["result"] or "")
                entry["preview"] = raw[:200] + ("..." if len(raw) > 200 else "")
            done.append(entry)

    return json.dumps({
        "summary": f"{len(running)} running, {len(done)} done, {len(errored)} errored",
        "running": running,
        "done": done,
        "errored": errored,
    }, indent=2)


TOOL_IMPL = {
    "status": tool_status,
    "analyze": tool_analyze,
    "vision": tool_vision,
    "fan_out": tool_fan_out,
    "structured_extract": tool_structured_extract,
    "inspect_page": tool_inspect_page,
    "browser_task": tool_browser_task,
    "render_html": tool_render_html,
    "debate": tool_debate,
    "research": tool_research,
    "crawl": tool_crawl,
    "compare": tool_compare,
    "search": tool_search,
    "fetch": tool_fetch,
    "get_task_result": tool_get_task_result,
}


# ============================================================
# JSON-RPC + MCP PROTOCOL
# ============================================================

def jsonrpc_ok(id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}

def jsonrpc_err(id: Any, code: int, msg: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": msg}}

def handle_initialize(id, params):
    return jsonrpc_ok(id, {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "Multi-model subagent with browser automation. Tools are task-oriented (analyze, vision, "
            "debate, etc.) with smart model defaults and explicit overrides. Browser tools are ASYNC: "
            "they return a task_id immediately. Call get_task_result to retrieve output."
        ),
    })

def handle_notifications_initialized(id, params):
    return None

def handle_tools_list(id, params):
    return jsonrpc_ok(id, {"tools": TOOL_SCHEMAS})

async def handle_tools_call(id, params):
    name = params.get("name", "")
    args = params.get("arguments", {})
    if name not in TOOL_IMPL:
        return jsonrpc_err(id, -32602, f"Unknown tool: {name}")
    try:
        text = await TOOL_IMPL[name](**args)
        return jsonrpc_ok(id, {"content": [{"type": "text", "text": text}], "isError": False})
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}")
        return jsonrpc_ok(id, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})

METHODS = {
    "initialize": handle_initialize,
    "notifications/initialized": handle_notifications_initialized,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
}


# ============================================================
# ROUTES
# ============================================================

async def mcp_endpoint(request: Request) -> JSONResponse:
    user_token = request.headers.get("x-forwarded-access-token", "")
    if not user_token:
        user_token = request.headers.get("authorization", "").replace("Bearer ", "")
    _current_user_token.set(user_token)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(jsonrpc_err(None, -32700, "Parse error"), status_code=200)
    rid = body.get("id")
    method = body.get("method", "")
    params = body.get("params", {})
    handler = METHODS.get(method)
    if not handler:
        return JSONResponse(jsonrpc_err(rid, -32601, f"Unknown method: {method}"), status_code=200)
    if asyncio.iscoroutinefunction(handler):
        result = await handler(rid, params)
    else:
        result = handler(rid, params)
    if result is None:
        return JSONResponse(content={}, status_code=202)
    return JSONResponse(result, status_code=200)

async def health_endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "server": SERVER_NAME, "version": SERVER_VERSION})

async def index(request: Request) -> JSONResponse:
    return JSONResponse({"name": SERVER_NAME, "version": SERVER_VERSION, "mcp": "/mcp", "tools": list(TOOL_IMPL.keys())})

app = Starlette(
    routes=[Route("/", index), Route("/health", health_endpoint), Route("/mcp", mcp_endpoint, methods=["POST"])],
    middleware=[Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], expose_headers=["*"])],
)
