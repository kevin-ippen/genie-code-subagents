# Search Engine Parser Fixtures

Versioned suite of real HTML responses captured through the production HTTP path.

## Structure

```
tests/fixtures/search/
├── brave/
│   ├── normal-page-1.html       # Standard results page
│   ├── normal-page-1.json       # Metadata sidecar
│   ├── sparse-results.html      # 0-2 results (genuine)
│   ├── sparse-results.json
│   ├── date-filtered.html       # Freshness/timelimit active
│   └── blocked.html             # Challenge/consent page (if captured)
├── mojeek/
│   └── ...
└── duckduckgo/
    └── ...
```

## JSON Sidecar Schema

```json
{
  "backend": "brave",
  "scenario": "normal-page-1",
  "query": "open source deep research agent",
  "captured_at": "2026-08-05T17:00:00Z",
  "upstream_ddgs_version": "7.6.1",
  "request_url": "https://search.brave.com/search?...",
  "final_url": "...",
  "status_code": 200,
  "body_sha256": "...",
  "body_length": 45000,
  "parsed_result_count": 10,
  "expected": {
    "outcome": "ok",
    "minimum_results": 5,
    "required_fields": ["title", "url"]
  }
}
```

## Capture

```bash
python scripts/capture_search_fixture.py \
    --backend brave \
    --scenario normal-page-1 \
    --query "open source deep research agent"
```

Use `--overwrite` to replace an existing fixture. Without it, the
script refuses to silently overwrite known-good fixtures.

## Contract

Each fixture defines expected behavior in its JSON sidecar:
- `outcome: "ok"` → parser MUST return ≥ `minimum_results` results
- `outcome: "no_results"` → parser MUST return empty list (NOT an error)
- `outcome: "blocked"` → parser MUST raise/return SearchOutcome.BLOCKED
- `outcome: "rate_limited"` → parser MUST recognize rate limiting
- `outcome: "parser_drift"` → parser extracted nothing from a substantial page

If a parser returns empty from an `ok` fixture → **the parser broke**.
This is distinguishable from genuine no-results.

## What NOT to save

- Cookies
- Authorization headers or tokens
- Proxy credentials
- Caller identifiers
- Ephemeral request IDs irrelevant to parsing
