"""MCP Gemini Subagent Server - Minimal bootstrap.

Prove MCP handshake works in Genie Code before adding Gemini tools.
"""

import asyncio
import base64
import json
import logging
import os
import random
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from openai import AsyncOpenAI, RateLimitError, APIStatusError
from starlette.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Minimal MCP server - health tool only
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Module-level async client and semaphore
# ---------------------------------------------------------------------------

_client: AsyncOpenAI | None = None
_semaphore: asyncio.Semaphore | None = None


def _get_client() -> AsyncOpenAI:
    """Lazy-init the async OpenAI client for connection reuse."""
    global _client
    if _client is None:
        workspace_url = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
        token = os.environ.get("DATABRICKS_TOKEN", "")
        _client = AsyncOpenAI(
            api_key=token,
            base_url=f"{workspace_url}/serving-endpoints",
            max_retries=0,  # We handle retries ourselves for metrics
        )
    return _client


def _get_semaphore() -> asyncio.Semaphore:
    """Lazy-init the concurrency semaphore."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(GEMINI_CONCURRENCY)
    return _semaphore


# ---------------------------------------------------------------------------
# Response normalization
# ---------------------------------------------------------------------------


def _extract_text(content: Any) -> str:
    """Extract plain text from Gemini structured response.

    Gemini 3.6 Flash returns content as a list of dicts:
      [{"type": "text", "text": "...", "thoughtSignature": "..."}]
    This extracts only the text parts, excluding thoughtSignature.
    """
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


# ---------------------------------------------------------------------------
# Core call with retry and metrics
# ---------------------------------------------------------------------------


async def _call_gemini(
    messages: list[dict],
    max_tokens: int = GEMINI_DEFAULT_MAX_TOKENS,
    response_format: dict | None = None,
) -> dict:
    """Make a single Gemini call with semaphore, retry, and metrics."""
    client = _get_client()
    sem = _get_semaphore()

    start = time.time()
    retries = 0
    last_error: Exception | None = None

    while retries <= GEMINI_RETRY_MAX:
        async with sem:
            try:
                kwargs: dict[str, Any] = {
                    "model": GEMINI_MODEL,
                    "messages": messages,
                    "max_tokens": max_tokens,
                }
                if response_format:
                    kwargs["response_format"] = response_format

                response = await client.chat.completions.create(**kwargs)
                elapsed = time.time() - start
                text = _extract_text(response.choices[0].message.content)
                usage = response.usage

                logger.info(
                    "gemini_call",
                    extra={
                        "latency_s": round(elapsed, 3),
                        "retries": retries,
                        "prompt_tokens": usage.prompt_tokens if usage else 0,
                        "completion_tokens": usage.completion_tokens if usage else 0,
                        "total_tokens": usage.total_tokens if usage else 0,
                    },
                )

                return {
                    "text": text,
                    "usage": {
                        "prompt_tokens": usage.prompt_tokens if usage else 0,
                        "completion_tokens": usage.completion_tokens if usage else 0,
                        "total_tokens": usage.total_tokens if usage else 0,
                    },
                    "latency_s": round(elapsed, 3),
                    "retries": retries,
                    "model": GEMINI_MODEL,
                }

            except RateLimitError as e:
                last_error = e
                retries += 1
                if retries > GEMINI_RETRY_MAX:
                    break
                delay = GEMINI_RETRY_BASE_DELAY * (2 ** (retries - 1)) + random.uniform(0, 0.5)
                logger.warning(f"Rate limited, retry {retries}/{GEMINI_RETRY_MAX} after {delay:.1f}s")
                await asyncio.sleep(delay)

            except APIStatusError as e:
                if e.status_code >= 500:
                    last_error = e
                    retries += 1
                    if retries > GEMINI_RETRY_MAX:
                        break
                    delay = GEMINI_RETRY_BASE_DELAY * (2 ** (retries - 1)) + random.uniform(0, 0.5)
                    logger.warning(f"Server error {e.status_code}, retry {retries}/{GEMINI_RETRY_MAX}")
                    await asyncio.sleep(delay)
                else:
                    raise

    elapsed = time.time() - start
    error_msg = str(last_error) if last_error else "Unknown error"
    logger.error(f"gemini_call_failed after {retries} retries: {error_msg}")
    return {
        "text": "",
        "error": error_msg,
        "latency_s": round(elapsed, 3),
        "retries": retries,
        "model": GEMINI_MODEL,
    }


# ---------------------------------------------------------------------------
# FastMCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "mcp-gemini-subagent",
    instructions=(
        "Gemini 3.6 Flash subagent for text reasoning, vision analysis, "
        "parallel fan-out, and structured extraction. Use these tools to "
        "delegate work to Gemini when you need fast multimodal analysis."
    ),
)


@mcp.tool()
async def gemini_analyze(
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = GEMINI_DEFAULT_MAX_TOKENS,
) -> str:
    """General text/reasoning subagent. Send a prompt to Gemini 3.6 Flash and
    get a text response. Use for summarization, analysis, code review,
    classification, or any reasoning task.

    Args:
        prompt: The user prompt to send to Gemini.
        system_prompt: Optional system instructions to guide Gemini's behavior.
        max_tokens: Maximum tokens for the response (min 256, default 1024).
    """
    max_tokens = max(256, max_tokens)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    result = await _call_gemini(messages, max_tokens=max_tokens)
    if result.get("error"):
        return json.dumps({"error": result["error"], "retries": result["retries"]})
    return result["text"]


@mcp.tool()
async def gemini_vision(
    image_base64: str,
    prompt: str,
    mime_type: str = "image/png",
    system_prompt: str = "",
    max_tokens: int = GEMINI_DEFAULT_MAX_TOKENS,
) -> str:
    """Analyze an image with Gemini 3.6 Flash vision. Send a base64-encoded
    image and a prompt describing what to analyze or extract.

    Args:
        image_base64: Base64-encoded image data (no data URI prefix needed).
        prompt: What to analyze or describe about the image.
        mime_type: Image MIME type. Allowed: image/png, image/jpeg, image/webp, image/gif.
        system_prompt: Optional system instructions.
        max_tokens: Maximum tokens for the response (min 256, default 1024).
    """
    if mime_type not in ALLOWED_IMAGE_MIMES:
        return json.dumps({"error": f"Unsupported MIME type: {mime_type}. Allowed: {list(ALLOWED_IMAGE_MIMES)}"})

    # Strip data URI prefix if accidentally included
    if ";base64," in image_base64:
        image_base64 = image_base64.split(";base64,", 1)[1]

    max_tokens = max(256, max_tokens)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
            },
        ],
    })

    result = await _call_gemini(messages, max_tokens=max_tokens)
    if result.get("error"):
        return json.dumps({"error": result["error"], "retries": result["retries"]})
    return result["text"]


@mcp.tool()
async def gemini_fan_out(
    prompts: list[str],
    system_prompt: str = "",
    max_tokens: int = GEMINI_DEFAULT_MAX_TOKENS,
) -> str:
    """Run multiple independent prompts concurrently through Gemini 3.6 Flash.
    Returns all results as a JSON array. Use for parallel analysis, batch
    classification, or divide-and-conquer reasoning.

    Args:
        prompts: List of prompts to execute in parallel (max 10).
        system_prompt: Optional system instructions applied to all prompts.
        max_tokens: Maximum tokens per response (min 256, default 1024).
    """
    if len(prompts) > MAX_FAN_OUT:
        return json.dumps({"error": f"Maximum {MAX_FAN_OUT} prompts per fan-out. Got {len(prompts)}."})
    if not prompts:
        return json.dumps({"error": "No prompts provided."})

    max_tokens = max(256, max_tokens)

    async def _run_one(idx: int, prompt: str) -> dict:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        result = await _call_gemini(messages, max_tokens=max_tokens)
        return {"index": idx, "prompt_preview": prompt[:80], **result}

    tasks = [_run_one(i, p) for i, p in enumerate(prompts)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    output = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            output.append({"index": i, "error": str(r), "text": ""})
        else:
            output.append(r)

    return json.dumps(output, indent=2)


@mcp.tool()
async def gemini_structured_extract(
    text: str,
    schema_json: str,
    extraction_prompt: str = "",
    max_tokens: int = GEMINI_DEFAULT_MAX_TOKENS,
) -> str:
    """Extract structured JSON from text using Gemini 3.6 Flash. Provide the
    text and a JSON schema describing the desired output structure. Gemini will
    attempt to return valid JSON conforming to the schema.

    Args:
        text: The source text to extract information from.
        schema_json: A JSON string describing the output schema (JSON Schema format).
        extraction_prompt: Optional additional instructions for extraction.
        max_tokens: Maximum tokens for the response (min 256, default 1024).
    """
    max_tokens = max(256, max_tokens)

    # Validate schema is parseable
    try:
        schema = json.loads(schema_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid schema JSON: {e}"})

    system = (
        "You are a structured data extraction engine. Extract information from the "
        "provided text and return ONLY valid JSON matching the schema. No explanation, "
        "no markdown, no code fences — just the JSON object."
    )
    if extraction_prompt:
        system += f"\n\nAdditional instructions: {extraction_prompt}"

    user_content = (
        f"Schema:\n```json\n{json.dumps(schema, indent=2)}\n```\n\n"
        f"Text to extract from:\n\n{text}"
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]

    result = await _call_gemini(messages, max_tokens=max_tokens)
    if result.get("error"):
        return json.dumps({"error": result["error"], "retries": result["retries"]})

    # Attempt to validate the output as JSON
    raw_text = result["text"].strip()
    # Strip markdown code fences if model included them despite instructions
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        raw_text = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    try:
        parsed = json.loads(raw_text)
        return json.dumps(parsed, indent=2)
    except json.JSONDecodeError:
        # Return raw text with a warning so caller can decide
        return json.dumps({
            "warning": "Response was not valid JSON. Raw text included.",
            "raw_text": raw_text,
            "usage": result.get("usage"),
        })


# ---------------------------------------------------------------------------
# Health check tool
# ---------------------------------------------------------------------------


@mcp.tool()
async def health() -> str:
    """Check the health of the Gemini subagent MCP server. Returns status,
    model configuration, and a quick connectivity test result."""
    # Quick connectivity probe
    result = await _call_gemini(
        [{"role": "user", "content": "Reply with exactly: OK"}],
        max_tokens=256,
    )
    return json.dumps({
        "status": "healthy" if not result.get("error") else "degraded",
        "model": GEMINI_MODEL,
        "concurrency_limit": GEMINI_CONCURRENCY,
        "probe_response": result.get("text", "")[:50],
        "probe_latency_s": result.get("latency_s"),
        "error": result.get("error"),
    }, indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
    logger.info(f"Starting mcp-gemini-subagent on 0.0.0.0:{port}/mcp")
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
