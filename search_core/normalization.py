"""URL normalization and deduplication.

Canonicalizes URLs so the same page doesn't appear multiple times
in search results from different providers or query variations.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from .models import SearchResult

# Query params that are purely tracking/analytics — safe to strip
_TRACKING_PARAMS: frozenset[str] = frozenset([
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
    "_ga", "_gl", "__hsfp", "__hssc", "__hstc",
    "source", "campaign", "medium",
])


def canonicalize_url(url: str) -> str:
    """Normalize a URL to its canonical form.

    Removes tracking params, normalizes scheme/host casing,
    strips trailing slashes, sorts remaining query params.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    # Normalize scheme and host to lowercase
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()

    # Remove default ports
    if host.endswith(":80") and scheme == "http":
        host = host[:-3]
    elif host.endswith(":443") and scheme == "https":
        host = host[:-4]

    # Normalize path: remove trailing slash (except root)
    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    # Remove tracking query params, sort the rest
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    cleaned_params = {
        k: v for k, v in query_params.items()
        if k.lower() not in _TRACKING_PARAMS
    }
    # Sort for deterministic output
    sorted_query = urlencode(sorted(cleaned_params.items()), doseq=True) if cleaned_params else ""

    # Strip fragment (anchors don't identify different content for dedup)
    canonical = urlunparse((scheme, host, path, "", sorted_query, ""))
    return canonical


def url_fingerprint(url: str) -> str:
    """Generate a short fingerprint for a canonical URL (for dedup lookups)."""
    canonical = canonicalize_url(url)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def deduplicate_results(results: list[SearchResult]) -> list[SearchResult]:
    """Remove duplicate search results, keeping the highest-ranked occurrence.

    Deduplication uses canonical URL fingerprints.
    """
    seen_fingerprints: set[str] = set()
    unique: list[SearchResult] = []

    for result in results:
        fp = url_fingerprint(result.url)
        if fp in seen_fingerprints:
            continue
        seen_fingerprints.add(fp)

        # Update the canonical_url field
        result.canonical_url = canonicalize_url(result.url)
        unique.append(result)

    return unique


def extract_domain(url: str) -> str:
    """Extract the registrable domain from a URL."""
    try:
        host = urlparse(url).netloc.lower()
        # Strip www. prefix
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""
