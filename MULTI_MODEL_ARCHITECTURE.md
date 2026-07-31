# Multi-Model MCP Subagent Architecture

**Status:** Implemented (v1.0.0 deployed)  
**Author:** Kevin Ippen + Genie Code  
**Date:** 2026-07-30  
**Current Version:** v1.0.0 (multi-model foundation + debate + list_models + get_updates)  
**Next:** v1.1.0 — multi_analyze, cascade, visual_review, agent tool-use (Phase C)

### Activation

All tools are gated: Genie Code only invokes them when the user says **"subagent"** in their message.

| Trigger | What happens |
| --- | --- |
| "subagent analyze this..." | Routes to `analyze` tool |
| "subagent debate whether..." | Routes to `debate` tool |
| "subagent inspect https://..." | Routes to `inspect_page` |
| "subagent update" | Routes to `get_updates` — bulk status check on all tasks |
| "subagent list models" | Routes to `list_models` |

---

## 1. Design Principles

1. **Tools are task-oriented, not model-oriented.** Users call `analyze`, not `gemini_analyze`. The tool name describes what you want done.
2. **Smart defaults, explicit overrides.** Each task routes to the best model for the job. Users CAN specify a model if they want — that override is always respected.
3. **Models are agents, not APIs.** When we invoke a model for a complex task (browser automation, code review, multi-step reasoning), we're not just prompting it — we're giving it a role, tools, and constraints. The quality of these "agent configurations" is what determines output quality.
4. **Async-first for heavy work.** Any task involving browser, multi-model, or long-running inference returns a task_id immediately. Retrieval is separate.
5. **Observability built in.** Every task tracks: model used, tokens consumed, latency, retries, cost estimate.

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        GENIE CODE / SUPERVISOR                       │
│  (calls MCP tools, retrieves results, orchestrates conversation)     │
└────────────────────────────────┬────────────────────────────────────┘
                                 │ JSON-RPC POST /mcp
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     LAYER 1: MCP TOOL INTERFACE                       │
│                                                                       │
│  Task Tools (what the caller sees):                                   │
│    • analyze         - text reasoning (sync)                          │
│    • vision          - image understanding (sync)                     │
│    • fan_out         - parallel prompts, same model (sync)            │
│    • structured_extract - JSON from text (sync)                       │
│    • inspect_page    - browser read-only (ASYNC)                      │
│    • browser_task    - goal-driven automation (ASYNC)                  │
│    • render_html     - local HTML render (ASYNC)                      │
│    • multi_analyze   - same prompt → N models (ASYNC)                 │
│    • debate          - model A generates, model B critiques (ASYNC)   │
│    • cascade         - fast→powerful escalation (ASYNC)               │
│    • get_task_result - retrieve async output                          │
│    • list_models     - discover available endpoints                   │
│    • list_tasks      - see running/completed tasks                    │
│    • health          - diagnostics                                    │
│                                                                       │
│  Every tool accepts optional `model` param (override default)         │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     LAYER 2: TASK ROUTER                              │
│                                                                       │
│  Model Registry:                                                      │
│    ┌─────────────────────────────────────────────────────────┐        │
│    │ key              │ endpoint                    │ caps    │        │
│    ├─────────────────────────────────────────────────────────┤        │
│    │ gemini-flash     │ databricks-gemini-3-6-flash │ V,F,T  │        │
│    │ gpt-5.6         │ databricks-gpt-5-6          │ V,F    │        │
│    │ claude-sonnet    │ databricks-claude-sonnet-4  │ V,F    │        │
│    │ llama-maverick   │ databricks-meta-llama-4-m   │ V,F    │        │
│    │ gemini-pro       │ databricks-gemini-2-5-pro   │ V,F,T  │        │
│    └─────────────────────────────────────────────────────────┘        │
│    V=vision, F=function-calling, T=thinking/reasoning                 │
│                                                                       │
│  Default Routing Table:                                               │
│    ┌───────────────────────────────────────────────────────┐          │
│    │ task              │ default_model   │ reason           │          │
│    ├───────────────────────────────────────────────────────┤          │
│    │ analyze           │ gemini-flash    │ fast, cheap      │          │
│    │ vision            │ gemini-flash    │ best multimodal  │          │
│    │ fan_out           │ gemini-flash    │ high concurrency │          │
│    │ structured_extract│ gemini-flash    │ reliable JSON    │          │
│    │ browser_planning  │ gemini-flash    │ fast iterative   │          │
│    │ browser_summarize │ gemini-flash    │ vision+text      │          │
│    │ debate_generator  │ gpt-5.6        │ creative/deep    │          │
│    │ debate_critic     │ claude-sonnet   │ analytical       │          │
│    │ cascade_fast      │ gemini-flash    │ quick filter     │          │
│    │ cascade_powerful  │ gemini-pro      │ deep reasoning   │          │
│    └───────────────────────────────────────────────────────┘          │
│                                                                       │
│  Override Resolution:                                                 │
│    1. User passes `model: "claude-sonnet"` → use that                 │
│    2. No override → look up task in routing table                     │
│    3. If model unavailable (404/down) → fallback to next in chain     │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     LAYER 3: AGENT RUNTIME                           │
│                                                                       │
│  This is the KEY layer — it defines what context and capabilities     │
│  each INVOKED MODEL receives when performing a task.                  │
│                                                                       │
│  Each task type has an AgentConfig:                                   │
│    • system_prompt: role, constraints, output format                   │
│    • tools: what sub-capabilities the model can invoke                │
│    • output_schema: how we parse/validate the response                │
│    • max_tokens: budget for this task                                 │
│    • temperature: creativity vs determinism                           │
│    • retry_policy: when/how to retry on failure                       │
│    • escalation: when to fall back to a more powerful model           │
│                                                                       │
│  (Detailed per-task configs in Section 4)                             │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     LAYER 4: INFRASTRUCTURE                          │
│                                                                       │
│  • Async Task Store (in-memory dict, task_id → status/result)         │
│  • Connection Pool (one AsyncOpenAI client per endpoint, reused)      │
│  • Rate Limiter (per-endpoint semaphore, configurable)                │
│  • Token/Cost Tracker (accumulates per task, per model)               │
│  • TTL Cleanup (expire completed tasks after 10min)                   │
│  • LD_LIBRARY_PATH management (browser sessions only)                 │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Model Registry & Discovery

### 3.1 Static Registry (configured at deploy)

```python
MODEL_REGISTRY = {
    "gemini-flash": {
        "endpoint": "databricks-gemini-3-6-flash",
        "provider": "google",
        "vision": True,
        "thinking": True,
        "max_context": 1_000_000,
        "max_output": 8192,
        "speed": "fast",         # fast | medium | slow
        "cost_tier": "low",     # low | medium | high
        "concurrency_limit": 10,
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
}
```

### 3.2 Dynamic Discovery (runtime)

```python
async def tool_list_models():
    """Discover what's actually available right now."""
    results = []
    for key, config in MODEL_REGISTRY.items():
        # Probe endpoint health (lightweight)
        available = await _probe_endpoint(config["endpoint"])
        results.append({
            "key": key,
            "endpoint": config["endpoint"],
            "available": available,
            "vision": config["vision"],
            "speed": config["speed"],
            "cost_tier": config["cost_tier"],
        })
    return results
```

### 3.3 Override Mechanism

Every tool schema includes:
```json
"model": {
    "type": "string",
    "description": "Override the default model. Use list_models to see available options.",
    "default": null
}
```

Resolution logic:
```python
def resolve_model(task_type: str, user_override: str | None) -> ModelConfig:
    if user_override:
        if user_override not in MODEL_REGISTRY:
            raise ValueError(f"Unknown model: {user_override}. Use list_models.")
        return MODEL_REGISTRY[user_override]
    return MODEL_REGISTRY[DEFAULT_ROUTING[task_type]]
```

---

## 4. Agent Configurations (The Interesting Part)

This is where quality lives. Each task type defines exactly what context, tools, and constraints the invoked model receives.

### 4.1 Browser Planning Agent

**Used by:** `browser_task` (the model that decides what to click/fill/navigate)  
**Default model:** `gemini-flash` (fast iteration, good vision)  
**Why this config matters:** The model is acting as an autonomous browser operator. It needs to understand UI state, plan actions, and recover from failures.

```python
BROWSER_PLANNING_AGENT = AgentConfig(
    system_prompt="""You are a browser automation agent...""",  # existing
    
    # TOOLS THE MODEL CAN INVOKE (sub-capabilities):
    tools=[
        # The model can ask for these during planning:
        "screenshot"     # request a fresh screenshot
        "aria_tree"      # request current ARIA state
        "execute_action" # click/fill/press/navigate/scroll
        "wait"           # pause for page to settle
        "evaluate_js"    # run arbitrary JS for state inspection
    ],
    
    # What we give it per turn:
    context_injection=[
        "current_url",
        "page_title", 
        "aria_tree (first 8000 chars)",
        "screenshot (base64 PNG)",
        "action_history (last 10 actions + outcomes)",
        "round_number / max_rounds",
    ],
    
    output_schema={
        "done": "boolean",
        "result": "string (if done)",
        "actions": [{ "action", "locator", "value", "reasoning" }],
    },
    
    max_tokens=2048,
    temperature=0.2,  # deterministic action planning
    
    # Escalation: if gemini-flash fails 2 rounds, switch to more powerful
    escalation_policy={
        "trigger": "2 consecutive rounds with no progress",
        "escalate_to": "gpt-5.6",  # better spatial reasoning
    },
)
```

**Open questions for refinement:**
- Should the browser agent have access to `evaluate_js` for reading DOM state that ARIA doesn't expose? (Current answer: yes, for form values, hidden state)
- Should it see the FULL ARIA tree or a filtered/summarized version? (Large pages produce 50K+ char trees)
- Should it have memory across sessions? (e.g., "last time I was on this page, the login button was at...")

---

### 4.2 Page Summarization Agent

**Used by:** `inspect_page` (summarize what's on a page), `render_html` (describe rendered UI)  
**Default model:** `gemini-flash` (fast, good vision)  
**Why this config matters:** This is a read-only analysis task — the model shouldn't try to interact, just describe.

```python
PAGE_SUMMARY_AGENT = AgentConfig(
    system_prompt="""You are a web page analyst. Describe the page content,
    layout, and key information concisely. Focus on:
    - Page purpose/type (dashboard, form, article, app)
    - Key data shown (numbers, charts, tables)
    - Navigation state (what page/section is active)
    - Any errors or loading states
    Do NOT suggest actions. Just describe what you see.""",
    
    tools=[],  # NO tools — pure observation
    
    context_injection=[
        "url",
        "page_title",
        "aria_tree (first 4000 chars)",
        "screenshot (base64 PNG)",
        "user_question (optional — specific thing to look for)",
    ],
    
    output_schema="free_text",  # unstructured summary
    max_tokens=1024,
    temperature=0.3,
)
```

---

### 4.3 Multi-Model Analyze

**Used by:** `multi_analyze` tool  
**Purpose:** Send the same prompt to N models, return all responses for comparison  
**Why this matters:** Different models have different strengths — seeing divergence reveals uncertainty

```python
MULTI_ANALYZE_CONFIG = {
    # Each model gets the SAME prompt but may get different system context
    # based on what it's good at
    "per_model_system_addendum": {
        "gemini-flash": "You are known for speed and efficiency. Be concise.",
        "gpt-5.6": "You are known for depth and nuance. Be thorough.",
        "claude-sonnet": "You are known for careful analysis. Note caveats.",
    },
    
    # Output includes:
    # - All individual responses
    # - Agreement/disagreement summary
    # - Confidence indicator (high agreement = high confidence)
    "post_processing": "consensus_check",
}
```

**MCP Tool Schema:**
```json
{
    "name": "multi_analyze",
    "description": "Send the same prompt to multiple models and compare responses. Returns all outputs plus a consensus summary. Use for high-stakes decisions or when you want diverse perspectives.",
    "inputSchema": {
        "properties": {
            "prompt": {"type": "string"},
            "system_prompt": {"type": "string", "default": ""},
            "models": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Which models to query. Default: all available.",
                "default": null
            },
            "max_tokens": {"type": "integer", "default": 1024}
        },
        "required": ["prompt"]
    }
}
```

---

### 4.4 Debate Tool (Adversarial Review)

**Used by:** `debate` tool  
**Purpose:** Model A generates, Model B critiques, optionally Model A refines  
**Why this matters:** Self-critique finds errors that single-model can't see

```python
DEBATE_CONFIG = {
    "generator": {
        "default_model": "gpt-5.6",  # creative, generative
        "system_prompt": """Generate a thorough response to the user's request.
        Be specific and detailed.""",
        "max_tokens": 2048,
    },
    "critic": {
        "default_model": "claude-sonnet",  # analytical, careful
        "system_prompt": """You are a critical reviewer. You've been given:
        1. The original request
        2. Another model's response
        
        Your job:
        - Identify factual errors, logical gaps, or missing context
        - Rate confidence (1-5) in the response's correctness
        - Suggest specific improvements
        - If the response is good, say so concisely
        
        Be constructive, not pedantic. Focus on substantive issues.""",
        "max_tokens": 1024,
    },
    "refinement": {
        # Optional third pass: generator sees critique and improves
        "enabled": True,
        "system_addendum": "A reviewer found these issues with your response. Please revise, addressing each point.",
        "max_tokens": 2048,
    },
    "rounds": 1,  # 1 round = generate → critique → refine
}
```

**MCP Tool Schema:**
```json
{
    "name": "debate",
    "description": "Adversarial review: one model generates a response, another critiques it, then the first refines. Returns all stages. Use for important decisions, code review, or when accuracy matters more than speed.",
    "inputSchema": {
        "properties": {
            "prompt": {"type": "string"},
            "system_prompt": {"type": "string", "default": ""},
            "generator_model": {"type": "string", "default": null},
            "critic_model": {"type": "string", "default": null},
            "include_refinement": {"type": "boolean", "default": true}
        },
        "required": ["prompt"]
    }
}
```

---

### 4.5 Cascade Tool (Fast→Powerful Escalation)

**Used by:** `cascade` tool  
**Purpose:** Try fast/cheap model first. If it's uncertain or the task is complex, automatically escalate to a more powerful (expensive) model.  
**Why this matters:** 80% of tasks can be handled by a fast model. Only escalate when needed.

```python
CASCADE_CONFIG = {
    "fast_model": "gemini-flash",
    "powerful_model": "gemini-pro",  # or gpt-5.6
    
    # The fast model is asked to self-assess confidence
    "fast_system_addendum": """
    After your response, on a new line, output CONFIDENCE: X
    where X is 1-5 (1=very uncertain, 5=very confident).
    Be honest — say 1-2 if the question is ambiguous, requires
    specialized knowledge you're unsure about, or is complex.
    """,
    
    # Escalation triggers:
    "escalate_if": [
        "confidence <= 2",
        "response contains 'I'm not sure'",
        "response contains 'I don't have enough information'",
        "token_count > 1500 (model is rambling = uncertain)",
    ],
    
    # When escalating, the powerful model sees:
    "powerful_context": [
        "original_prompt",
        "fast_model_response (for reference, may be wrong)",
        "reason_for_escalation",
    ],
}
```

---

### 4.6 Vision Comparison (Multi-Model Visual Review)

**Used by:** `render_html` with multi-model, or a new `visual_review` tool  
**Purpose:** Multiple models look at the same screenshot and provide different perspectives  
**Why this matters:** UI review benefits from diverse "eyes" — one model catches layout issues, another catches accessibility, another catches data accuracy

```python
VISUAL_REVIEW_CONFIG = {
    "reviewers": [
        {
            "model": "gemini-flash",
            "role": "layout_reviewer",
            "system_prompt": """Review this UI for layout and visual design:
            - Alignment and spacing issues
            - Color contrast / readability
            - Responsive design concerns
            - Visual hierarchy""",
        },
        {
            "model": "claude-sonnet",
            "role": "data_reviewer", 
            "system_prompt": """Review this UI for data accuracy and completeness:
            - Are numbers/charts consistent?
            - Is anything obviously wrong or missing?
            - Are labels clear and unambiguous?""",
        },
        {
            "model": "gpt-5.6",
            "role": "ux_reviewer",
            "system_prompt": """Review this UI for user experience:
            - Is the information hierarchy clear?
            - Can a user accomplish their goal efficiently?
            - Are there confusing elements?
            - Accessibility concerns?""",
        },
    ],
    # All reviewers see the same screenshot + ARIA tree
    # Results are returned as structured reviewer reports
}
```

---

## 5. The Deep Question: Tools for Invoked Models

When we call a model to perform a task, what capabilities should IT have access to? This is the difference between a dumb prompt and a capable agent.

### 5.1 Current State (v0.6.0)

| Task | What the model receives | What it can do |
|------|------------------------|----------------|
| browser_task | ARIA + screenshot + goal | Output action JSON (we execute) |
| inspect_page | ARIA + screenshot | Output free text summary |
| analyze | User prompt | Output free text |
| vision | Image + prompt | Output free text |

The models are **stateless prompt-response** — they get input, produce output, done.

### 5.2 Future State (v1.0.0)

Models become **agents with tools** — they can call back into our system:

| Task | Model receives | Model can invoke |
|------|---------------|------------------|
| browser_task | ARIA + screenshot + goal + history | `execute_action`, `screenshot`, `evaluate_js`, `wait` |
| code_review | Code + context | `search_docs`, `run_lint`, `check_types` |
| data_analysis | Schema + sample | `run_query`, `get_stats`, `plot` |
| research | Question + sources | `web_search`, `read_page`, `summarize` |

### 5.3 Implementation: Tool-Use Within Agent Calls

For models that support function calling (most do via OpenAI-compat):

```python
async def _call_model_with_tools(
    model_key: str,
    messages: list[dict],
    tools: list[dict],          # OpenAI-format tool definitions
    tool_handlers: dict,        # name → async callable
    max_iterations: int = 5,
) -> dict:
    """Call a model with tool-use loop (agentic execution)."""
    config = resolve_model(model_key)
    client = get_client(config)
    
    for i in range(max_iterations):
        response = await client.chat.completions.create(
            model=config["endpoint"],
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        
        msg = response.choices[0].message
        
        # If model produced final text (no tool calls), we're done
        if not msg.tool_calls:
            return {"text": msg.content, "iterations": i + 1}
        
        # Execute tool calls and feed results back
        messages.append(msg)  # assistant message with tool_calls
        for tool_call in msg.tool_calls:
            handler = tool_handlers[tool_call.function.name]
            result = await handler(**json.loads(tool_call.function.arguments))
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": str(result),
            })
    
    return {"text": "", "error": "max iterations reached"}
```

### 5.4 Safety Boundaries

**What models should NEVER be able to do:**
- Access the host filesystem
- Make network requests outside Databricks
- Modify the task store or other tasks
- Access other users' tokens or credentials
- Run arbitrary code without sandboxing

**What models CAN do (scoped per task):**
- Read browser state (ARIA, screenshots, DOM)
- Execute pre-defined browser actions (click, fill, etc.)
- Call other Databricks serving endpoints
- Access task-specific context passed in

---

## 6. New Tool Specifications

### 6.1 Tool: `analyze`

Replaces: `gemini_analyze`  
Default model: `gemini-flash`  
Mode: Synchronous  

```json
{
    "name": "analyze",
    "description": "General text/reasoning subagent. Send a prompt for summarization, analysis, code review, classification, or any reasoning task.",
    "inputSchema": {
        "properties": {
            "prompt": {"type": "string", "description": "The prompt to send."},
            "system_prompt": {"type": "string", "default": ""},
            "model": {"type": "string", "description": "Override default model.", "default": null},
            "max_tokens": {"type": "integer", "default": 1024}
        },
        "required": ["prompt"]
    }
}
```

### 6.2 Tool: `multi_analyze`

New tool.  
Mode: ASYNC (multiple model calls)  

```json
{
    "name": "multi_analyze",
    "description": "Send the same prompt to multiple models in parallel. Returns all responses plus a consensus/divergence summary. ASYNC: returns task_id.",
    "inputSchema": {
        "properties": {
            "prompt": {"type": "string"},
            "system_prompt": {"type": "string", "default": ""},
            "models": {"type": "array", "items": {"type": "string"}, "description": "Models to query. Null = all available.", "default": null},
            "max_tokens": {"type": "integer", "default": 1024}
        },
        "required": ["prompt"]
    }
}
```

### 6.3 Tool: `debate`

New tool.  
Mode: ASYNC (multi-model sequential)  

```json
{
    "name": "debate",
    "description": "Adversarial review pipeline: Model A generates → Model B critiques → Model A refines. Returns all stages. ASYNC: returns task_id.",
    "inputSchema": {
        "properties": {
            "prompt": {"type": "string"},
            "system_prompt": {"type": "string", "default": ""},
            "generator_model": {"type": "string", "default": null},
            "critic_model": {"type": "string", "default": null},
            "include_refinement": {"type": "boolean", "default": true},
            "max_tokens": {"type": "integer", "default": 2048}
        },
        "required": ["prompt"]
    }
}
```

### 6.4 Tool: `cascade`

New tool.  
Mode: ASYNC (sequential, conditional)  

```json
{
    "name": "cascade",
    "description": "Try a fast model first. If it's uncertain, automatically escalate to a more powerful model. Cost-efficient for mixed-difficulty tasks. ASYNC: returns task_id.",
    "inputSchema": {
        "properties": {
            "prompt": {"type": "string"},
            "system_prompt": {"type": "string", "default": ""},
            "fast_model": {"type": "string", "default": null},
            "powerful_model": {"type": "string", "default": null},
            "confidence_threshold": {"type": "integer", "description": "Escalate if confidence <= this (1-5).", "default": 2},
            "max_tokens": {"type": "integer", "default": 2048}
        },
        "required": ["prompt"]
    }
}
```

### 6.5 Tool: `visual_review`

New tool.  
Mode: ASYNC (multi-model parallel on same image)  

```json
{
    "name": "visual_review",
    "description": "Multiple specialized reviewers analyze the same image/screenshot from different angles (layout, data, UX). Returns structured reviews. ASYNC: returns task_id.",
    "inputSchema": {
        "properties": {
            "image_base64": {"type": "string", "description": "Base64-encoded image."},
            "context": {"type": "string", "description": "What this image shows (e.g., 'a dashboard for sales data')."},
            "reviewers": {"type": "array", "items": {"type": "string"}, "description": "Which review perspectives. Default: all.", "default": null},
            "custom_criteria": {"type": "string", "description": "Additional review criteria.", "default": ""}
        },
        "required": ["image_base64", "context"]
    }
}
```

### 6.6 Tool: `list_models`

New tool.  
Mode: Synchronous  

```json
{
    "name": "list_models",
    "description": "Discover available model endpoints, their capabilities, and current status.",
    "inputSchema": {"properties": {}, "required": []}
}
```

---

## 7. Roadmap

### v1.0.0 — Multi-Model Foundation ✅ SHIPPED

1. Model registry (5 models) + routing table + `resolve_model()`
2. `_call_model()` with per-model semaphore + retry
3. Renamed tools: `analyze`, `vision`, `fan_out`, `structured_extract`
4. Optional `model` override on every tool
5. `list_models` (sync)
6. `debate` (async: generate → critique → conditional refine)
7. `get_updates` (bulk task status — triggered by "subagent update")
8. "subagent" activation gate on all tool descriptions
9. Single-worker pinned (`--workers 1`)
10. No backward-compat aliases

### v1.1.0 — World Interaction Tools (IN PROGRESS)

These fill the biggest gaps in OOTB Genie Code: no internet access, no HTTP client, no visual regression, no deep research, no large doc handling, no background work.

**Shipped:**
- `search` — web search via native grounding + browser fallback ✅
- `fetch` — HTTP client with security blocklist ✅
- `compare` — visual/textual diff ✅
- `research` — multi-page breadth-first crawl + synthesis ✅
- `crawl` — structured multi-page extraction with link pattern filtering ✅

**Remaining:**
- `journey` — user flow walkthrough
- `audit` — consistency/quality check
- `digest` — large document processing
- `monitor` — background polling

#### `search` — Web Search via Native Model Tools

Gemini and GPT have built-in web search (grounding). Expose it via function calling:

```python
# Call model with native search tool enabled
response = await client.chat.completions.create(
    model=config["endpoint"],
    messages=messages,
    tools=[{"type": "function", "function": {"name": "google_search", ...}}],
    tool_choice="auto",
)
```

Fallback: if endpoint doesn't support native search, use `browser_task` with search goal.

- **Mode:** ASYNC (search + summarize can take time)
- **Params:** `query`, `num_results`, `model` (default: model with best search grounding)
- **Returns:** structured results with sources, snippets, synthesis

#### `fetch` — HTTP Client / API Probe

Raw HTTP capability. Genie Code has *zero* ability to hit arbitrary URLs.

```python
async def tool_fetch(url, method="GET", headers=None, body=None, timeout=30):
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(method, url, headers=headers, content=body)
        return {"status": resp.status_code, "headers": dict(resp.headers),
                "body": resp.text[:50000], "elapsed_ms": resp.elapsed.total_seconds() * 1000}
```

- **Mode:** Sync (fast, single request)
- **Params:** `url`, `method`, `headers`, `body`, `timeout`
- **Security:** Block internal Databricks API paths, localhost, metadata endpoints

#### `compare` — Visual / Textual Diff

Feed two screenshots or two text blocks to vision; get structured diff.

- **Mode:** Sync (single vision call)
- **Params:** `image_a_b64`, `image_b_b64` OR `text_a`, `text_b`; `focus` (optional: "layout", "data", "style")
- **Returns:** structured diff with categories (added, removed, changed, unchanged)
- **Use cases:** visual regression testing, before/after deploy checks, PR review

#### `research` — Multi-Page Research Crawl ✅ SHIPPED

Breadth-first multi-page research with fact extraction and synthesis.

- **Mode:** ASYNC
- **Params:** `query`, `start_urls` (default: Google search), `max_pages` (1-10, default 5), `model`
- **Behavior:**
  1. If no `start_urls`, starts with a Google search for the query
  2. Visits each URL with browser, extracts ARIA tree
  3. LLM extracts: facts (relevant to query), relevant links to follow (max 3)
  4. Follows discovered links breadth-first until `max_pages` reached
  5. Synthesizes all findings into a structured research report
- **Returns:** `{query, pages_visited, pages: [{url, title, facts, error}], synthesis, total_tokens}`
- **Implementation:** `_research_extract_page()` + `_do_research()` + `tool_research()`

```python
async def tool_research(query, start_urls=None, max_pages=5, model=None):
    # ASYNC: returns task_id immediately
    # Background: visits pages, extracts facts via LLM, follows links, synthesizes
```

#### Remaining Crawl Modes (TODO)

| Tool | Purpose | Behavior |
| --- | --- | --- |
| `journey` | User flow walkthrough | Follow a defined path (login → dashboard → report), evaluate UX at each step. |
| `audit` | Consistency/quality check | Visit N pages, check for broken links, stale content, inconsistencies. |

All are ASYNC. All accept `url`, `depth`/`max_pages`, and mode-specific params.

```python
# journey mode
async def tool_journey(start_url, steps, evaluate_criteria=None, model=None):
    """Walk a user journey, screenshot + assess each step."""

# audit mode
async def tool_audit(url, checks=["broken_links", "consistency", "accessibility"], max_pages=10, model=None):
    """Multi-page quality audit with structured findings."""
```

#### `digest` — Large Document Processing

Chunk + map-reduce for documents that overwhelm context windows.

- **Mode:** ASYNC (multiple model calls)
- **Params:** `text` OR `url`, `goal` (summarize | extract | answer), `question` (if goal=answer), `max_tokens_per_chunk`, `model`
- **Approach:**
  1. Chunk input into overlapping segments (~4K tokens each)
  2. `fan_out` summarize/extract each chunk in parallel
  3. Synthesize chunk results into final output
  4. If synthesis is too long, recurse (tree-reduce)
- **Use cases:** "summarize this 200-page PDF", "find all mentions of X in this doc", "answer this question from the full text"

#### `monitor` — Background Polling

Long-running async task that checks a condition periodically.

- **Mode:** ASYNC (long-lived task, updates its own result over time)
- **Params:** `url` OR `check_fn`, `condition` (natural language: "returns 200", "contains 'deployed'"), `interval_seconds` (min 30), `timeout_minutes` (max 60), `model`
- **Behavior:**
  1. Creates task immediately
  2. Polls at interval, updates task result with latest check
  3. When condition met → task status = "done" with alert
  4. On timeout → task status = "done" with "condition not met"
  5. `get_updates` surfaces alerts naturally
- **Constraints:** Single-worker means max ~5 concurrent monitors. TTL still applies (10min default, extended for monitors).
- **Use cases:** "watch this deploy and tell me when it's healthy", "ping this endpoint until it returns 200"

### v1.2.0 — Multi-Model Comparison Tools

1. `multi_analyze` — identical prompt to N models, consensus check
2. `cascade` — fast→powerful escalation on disagreement (two-sample at temp 0.7)
3. `visual_review` — multi-perspective image analysis (layout/data/UX reviewers)
4. Agent tool-use: `_call_model_with_tools` agentic loop

### v1.3.0 — Production Hardening

1. Response caching (key on endpoint+messages hash, temp=0 only)
2. ARIA compaction (single pass, keep interactive/labeled, drop presentational)
3. Connection pooling (reuse AsyncOpenAI clients)
4. Fallback chains (if model X 5xx, try model Y)
5. Module split if server.py > 2000 lines
6. Observability: per-model latency/error/token dashboards

---

## 8. Design Decisions (Resolved)

1. **AgentConfigs reference role keys, never model names** — routing table is single source of truth
2. **`multi_analyze`: identical prompts to all models** — no per-model addenda (divergence = model signal)
3. **`cascade`: two-sample disagreement at temp 0.7** — not self-reported confidence (miscalibrated)
4. **Browser agent: keep JSON-output-then-execute** — NOT function calling (adds latency, loses validation boundary)
5. **No backward-compat aliases** — Genie Code is the only caller
6. **Hard token budget ceiling** — not dollar warnings. Tasks fail cleanly at ceiling
7. **Single-worker constraint** — hard constraint for v1.x (in-memory task store)
8. **`parent_task_id` field** — minimal task chaining, budget inheritance
9. **`extract_text(provider, response)`** — per-provider branches, CI-tested
10. **Debate: fixed 1 round, max 2** — refinement conditional on critic flagging something substantive
11. **"subagent" activation gate** — all tool descriptions gated so Genie Code only invokes on explicit user intent
12. **`get_updates` for bulk retrieval** — single call surfaces all task states, natural trigger is "subagent update"

## 9. Open Questions

1. **Native search tool support:** Do Databricks serving endpoints pass through Google/Bing search grounding? Need to probe each endpoint. Fallback is browser-based search.
2. **`fetch` security boundary:** Block localhost, 169.254.x.x (metadata), internal Databricks APIs. Allow everything else? Or whitelist?
3. **Monitor TTL extension:** Monitors need longer TTL than 10min. Separate TTL config? Or dynamic based on `timeout_minutes`?
4. **Crawl depth vs. cost:** Deep crawls (10+ pages) could burn significant tokens on ARIA extraction + summarization. Budget ceiling per crawl?
5. **Digest chunking strategy:** Fixed-size chunks vs. semantic (paragraph/section) boundaries? Semantic is better but harder to implement.

---

## 9. File Structure (Target)

```
/mcp-gemini-subagent/
├── server.py              # Main MCP server (routes, JSON-RPC)
├── models.py              # Model registry, routing, client management
├── agents.py              # AgentConfig definitions per task type
├── tools/
│   ├── __init__.py
│   ├── sync_tools.py      # analyze, vision, fan_out, structured_extract, list_models
│   ├── browser_tools.py   # inspect_page, browser_task, render_html
│   ├── multi_tools.py     # multi_analyze, debate, cascade, visual_review
│   └── task_store.py      # get_task_result, list_tasks, cleanup
├── browser/
│   ├── __init__.py
│   ├── engine.py          # Playwright session management
│   ├── actions.py         # Action execution
│   └── auth.py            # Pre-auth, token management
├── app.yaml
├── requirements.txt
├── install_deps.py
└── libs/                  # Chromium system libraries
```

---

## 10. Success Criteria

- [ ] All existing tools work with model override
- [ ] `list_models` shows available endpoints with health status
- [ ] `multi_analyze` returns responses from 3+ models within 30s
- [ ] `debate` produces measurably better outputs than single-model
- [ ] `cascade` uses expensive model < 30% of the time
- [ ] Browser tools still work (no regression)
- [ ] Async pattern works for all new tools
- [ ] Connection errors on one model don't break other models
- [ ] Total server.py stays under 2000 lines (split into modules if needed)
