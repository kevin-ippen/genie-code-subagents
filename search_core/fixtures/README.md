# Parser Fixtures

Saved HTML responses from each search engine backend, used for:
1. Parser contract tests (CI)
2. Regression detection when upgrading ddgs
3. Documenting what blocking/consent pages look like

## Directory structure

```
fixtures/
├── brave/
│   ├── normal_results.html      # Standard 10-result page
│   ├── no_results.html          # Zero-result response
│   └── blocked.html             # Bot challenge / consent wall
├── mojeek/
│   ├── normal_results.html
│   ├── no_results.html
│   └── blocked.html
└── duckduckgo/
    ├── normal_results.html
    ├── no_results.html
    └── blocked.html
```

## Capture process

1. Execute a search via the adapter with a known query
2. Save the raw HTML response before parsing
3. Record the query, date, and response headers in the fixture filename or sidecar

## Contract

For each fixture:
- `normal_results.html` → parser returns ≥5 results with title+url+snippet
- `no_results.html` → parser returns empty list (NOT an error)
- `blocked.html` → parser raises a typed ProviderBlockedError

If a parser returns an empty list from a `normal_results` fixture,
the contract test fails → parser needs updating.
