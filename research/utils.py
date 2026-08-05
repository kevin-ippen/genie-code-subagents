"""Shared utilities for research orchestration."""

from __future__ import annotations

import json
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def extract_json(text: str) -> Optional[dict]:
    """Robustly extract a JSON object from model output.

    Handles:
    - Pure JSON
    - JSON wrapped in ```json ... ``` code fences
    - JSON preceded by conversational preamble ("Here is the result:\n{...")
    - JSON with trailing commentary
    - Nested JSON with proper brace matching

    Returns None if no valid JSON object found.
    """
    text = text.strip()

    # 1. Strip code fences (```json or ``` or ```JSON)
    fence_pattern = re.compile(r"^```(?:json|JSON)?\s*\n(.*?)\n```\s*$", re.DOTALL)
    m = fence_pattern.match(text)
    if m:
        text = m.group(1).strip()

    # 2. Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3. Find the outermost { ... } by brace-depth tracking
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    end = -1
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        c = text[i]
        if escape_next:
            escape_next = False
            continue
        if c == "\\":
            escape_next = True
            continue
        if c == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end == -1:
        return None

    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return None
