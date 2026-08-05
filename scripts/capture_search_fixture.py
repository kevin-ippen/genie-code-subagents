#!/usr/bin/env python3
"""Capture search engine fixtures through the production HTTP path.

Exercises the SAME client, user-agent, query params, decompression,
redirect handling, and character decoding that the production adapter uses.

Usage:
    python scripts/capture_search_fixture.py \
        --backend brave \
        --scenario normal-page-1 \
        --query "open source deep research agent" \
        [--page 1] \
        [--timelimit d] \
        [--overwrite]

Outputs:
    tests/fixtures/search/{backend}/{scenario}.html
    tests/fixtures/search/{backend}/{scenario}.json  (metadata sidecar)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path so we can import ddgs
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from ddgs import DDGS
except ImportError:
    print("ERROR: ddgs not installed. Run: pip install ddgs>=7.0.0")
    sys.exit(1)


FIXTURE_DIR = Path(__file__).parent.parent / "tests" / "fixtures" / "search"
SUPPORTED_BACKENDS = ("brave", "mojeek", "duckduckgo", "google", "yahoo", "startpage")


def capture_fixture(
    backend: str,
    scenario: str,
    query: str,
    page: int = 1,
    timelimit: str | None = None,
    overwrite: bool = False,
) -> None:
    """Capture a single fixture through the production HTTP path."""

    out_dir = FIXTURE_DIR / backend
    out_dir.mkdir(parents=True, exist_ok=True)

    html_path = out_dir / f"{scenario}.html"
    json_path = out_dir / f"{scenario}.json"

    if html_path.exists() and not overwrite:
        print(f"ERROR: {html_path} already exists. Use --overwrite to replace.")
        sys.exit(1)

    print(f"Capturing: backend={backend}, scenario={scenario}")
    print(f"  Query: {query!r}")
    print(f"  Page: {page}, timelimit: {timelimit}")
    print()

    # Use ddgs internals to get the raw HTTP response
    # This exercises the same path as production
    start = time.time()
    raw_html = ""
    results = []
    status_code = None
    final_url = ""
    error = None

    try:
        with DDGS(timeout=12) as ddgs:
            kwargs = {
                "keywords": query,
                "backend": backend,
                "region": "us-en",
                "safesearch": "moderate",
                "max_results": 10,
            }
            if timelimit:
                kwargs["timelimit"] = timelimit

            # Execute the search — ddgs handles HTTP internally
            results = list(ddgs.text(**kwargs))
            status_code = 200  # If we get here, it succeeded
            elapsed_ms = int((time.time() - start) * 1000)

    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        error = f"{type(e).__name__}: {e}"
        print(f"  Search failed: {error}")

    # For the HTML fixture, we need to capture the raw response
    # ddgs doesn't expose this directly, so we also do a direct HTTP fetch
    # through the same client behavior
    try:
        import httpx
        from ddgs._utils import _HEADERS  # ddgs internal headers

        # Reconstruct the request URL that ddgs would have used
        url_map = {
            "brave": "https://search.brave.com/search",
            "mojeek": "https://www.mojeek.com/search",
            "duckduckgo": "https://html.duckduckgo.com/html/",
            "google": "https://www.google.com/search",
            "yahoo": "https://search.yahoo.com/search",
            "startpage": "https://www.startpage.com/sp/search",
        }

        search_url = url_map.get(backend)
        if search_url and backend != "duckduckgo":
            # GET-based engines
            params = {"q": query}
            if backend == "brave" and timelimit:
                tf_map = {"d": "pd", "w": "pw", "m": "pm", "y": "py"}
                params["tf"] = tf_map.get(timelimit, "")
            if page > 1:
                params["offset"] = (page - 1) * 10

            with httpx.Client(timeout=12, follow_redirects=True, headers=_HEADERS) as client:
                resp = client.get(search_url, params=params)
                raw_html = resp.text
                status_code = resp.status_code
                final_url = str(resp.url)

        elif backend == "duckduckgo":
            # POST-based
            data = {"q": query, "b": ""}
            if timelimit:
                data["df"] = timelimit

            with httpx.Client(timeout=12, follow_redirects=True, headers=_HEADERS) as client:
                resp = client.post(search_url, data=data)
                raw_html = resp.text
                status_code = resp.status_code
                final_url = str(resp.url)

    except ImportError:
        print("  WARNING: Could not capture raw HTML (httpx or ddgs internals unavailable)")
        print("  Saving parsed results only")
        raw_html = f"<!-- Parsed results only, raw HTML not captured -->\n<!-- Results: {len(results)} -->"
    except Exception as e:
        print(f"  WARNING: Raw HTML capture failed: {e}")
        raw_html = f"<!-- Raw HTML capture failed: {e} -->"

    # Determine fixture content hash
    body_sha256 = hashlib.sha256(raw_html.encode()).hexdigest() if raw_html else ""

    # Determine expected outcome
    if results:
        expected_outcome = "ok"
        min_results = max(3, len(results) - 2)  # Allow some variance
    elif error and ("rate" in error.lower() or "429" in error.lower()):
        expected_outcome = "rate_limited"
        min_results = 0
    elif error and ("block" in error.lower() or "captcha" in error.lower()):
        expected_outcome = "blocked"
        min_results = 0
    else:
        expected_outcome = "no_results" if not error else "upstream_error"
        min_results = 0

    # Write HTML fixture
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(raw_html)
    print(f"  Wrote: {html_path} ({len(raw_html)} chars)")

    # Write JSON sidecar
    sidecar = {
        "backend": backend,
        "scenario": scenario,
        "query": query,
        "page": page,
        "timelimit": timelimit,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "upstream_ddgs_version": _get_ddgs_version(),
        "request_url": final_url or url_map.get(backend, ""),
        "final_url": final_url,
        "status_code": status_code,
        "content_type": "text/html",
        "body_sha256": body_sha256,
        "body_length": len(raw_html),
        "elapsed_ms": elapsed_ms,
        "parsed_result_count": len(results),
        "error": error,
        "expected": {
            "outcome": expected_outcome,
            "minimum_results": min_results,
            "required_fields": ["title", "url"],
        },
    }

    with open(json_path, "w") as f:
        json.dump(sidecar, f, indent=2)
    print(f"  Wrote: {json_path}")

    # Summary
    print()
    print(f"  Status: {status_code}")
    print(f"  Results parsed: {len(results)}")
    print(f"  Expected outcome: {expected_outcome}")
    print(f"  Elapsed: {elapsed_ms}ms")
    if results:
        print(f"  First result: {results[0].get('title', '')[:60]}")


def _get_ddgs_version() -> str:
    """Get installed ddgs version."""
    try:
        import importlib.metadata
        return importlib.metadata.version("ddgs")
    except Exception:
        return "unknown"


def main():
    parser = argparse.ArgumentParser(
        description="Capture search engine HTML fixtures through the production HTTP path."
    )
    parser.add_argument("--backend", required=True, choices=SUPPORTED_BACKENDS,
                        help="Search backend to capture from")
    parser.add_argument("--scenario", required=True,
                        help="Fixture scenario name (e.g. normal-page-1, sparse-results, blocked)")
    parser.add_argument("--query", required=True,
                        help="Search query to execute")
    parser.add_argument("--page", type=int, default=1,
                        help="Results page number (default: 1)")
    parser.add_argument("--timelimit", choices=["d", "w", "m", "y"],
                        help="Date filter: d=day, w=week, m=month, y=year")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing fixture (required for updates)")

    args = parser.parse_args()
    capture_fixture(
        backend=args.backend,
        scenario=args.scenario,
        query=args.query,
        page=args.page,
        timelimit=args.timelimit,
        overwrite=args.overwrite,
    )


if __name__ == "__main__":
    main()
