"""Header security: strip sensitive headers from outbound requests.

Prevents leaking credentials, session tokens, and internal routing info
when making requests to arbitrary external URLs.
"""

from __future__ import annotations

from typing import Optional

# Headers that must never be forwarded to external URLs
_SENSITIVE_HEADERS: frozenset[str] = frozenset([
    "authorization",
    "cookie",
    "set-cookie",
    "x-databricks-token",
    "x-forwarded-for",
    "x-real-ip",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
])

# Headers that may carry internal routing info
_INTERNAL_HEADERS: frozenset[str] = frozenset([
    "x-request-id",
    "x-correlation-id",
    "x-trace-id",
    "x-databricks-org-id",
    "x-databricks-workspace-id",
])


def sanitize_outbound_headers(
    headers: dict[str, str],
    *,
    allow_auth: bool = False,
    target_is_databricks: bool = False,
) -> dict[str, str]:
    """Remove sensitive headers from an outbound request.

    Args:
        headers: The request headers dict (case-insensitive keys).
        allow_auth: If True, preserve Authorization header (for Databricks-internal calls).
        target_is_databricks: If True, preserve internal routing headers.

    Returns:
        Cleaned headers dict.
    """
    cleaned = {}
    for key, value in headers.items():
        key_lower = key.lower()

        # Always strip sensitive headers (unless explicitly allowed)
        if key_lower in _SENSITIVE_HEADERS:
            if key_lower == "authorization" and allow_auth:
                cleaned[key] = value
            continue

        # Strip internal headers unless targeting Databricks
        if key_lower in _INTERNAL_HEADERS and not target_is_databricks:
            continue

        cleaned[key] = value

    return cleaned


def build_browser_headers(
    url: str,
    token: Optional[str] = None,
    *,
    is_databricks_url: bool = False,
) -> dict[str, str]:
    """Build safe headers for a browser context.

    Only injects Authorization for Databricks-controlled domains.
    """
    headers = {}

    if token and is_databricks_url:
        headers["Authorization"] = f"Bearer {token}"

    return headers
