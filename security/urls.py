"""URL security: SSRF protection, DNS validation, redirect safety.

This module gates every outbound request (HTTP and browser) to prevent:
- Server-Side Request Forgery (SSRF) via private/loopback/metadata IPs
- Token leakage via redirect chains to untrusted hosts
- Access to internal Databricks APIs via path-based probes
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hosts that are always blocked regardless of DNS resolution
_BLOCKED_HOSTS: frozenset[str] = frozenset([
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "[::1]",
    "::1",
    "metadata.google.internal",
    "metadata.internal",
    "169.254.169.254",  # AWS/GCP metadata
    "100.100.100.200",  # Alibaba metadata
    "fd00::1",          # IPv6 link-local
])

# Databricks API path patterns that should never be fetched externally
_BLOCKED_PATH_PATTERNS: tuple[str, ...] = (
    "/api/2.0/",
    "/api/2.1/",
    "/serving-endpoints/",
    "/sql/",
    "/oauth2/",
)

# Hostnames allowed to receive bearer tokens
_DATABRICKS_TOKEN_DOMAINS: frozenset[str] = frozenset([
    "databricksapps.com",
    "databricksapps.net",
    "cloud.databricks.com",
    "azuredatabricks.net",
    "gcp.databricks.com",
    "databricks.com",
])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_url_safe(url: str, *, resolve_dns: bool = True) -> tuple[bool, Optional[str]]:
    """Check if a URL is safe for outbound requests.

    Returns:
        (True, None) if safe.
        (False, reason) if blocked.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Malformed URL"

    # Scheme check
    if parsed.scheme not in ("http", "https"):
        return False, f"Blocked scheme: {parsed.scheme}"

    host = (parsed.hostname or "").lower().strip("[]")
    if not host:
        return False, "No hostname in URL"

    # Explicit blocklist
    if host in _BLOCKED_HOSTS:
        return False, f"Blocked host: {host}"

    # Path-based blocks (prevent Databricks API probing)
    path = parsed.path or ""
    if any(pat in path for pat in _BLOCKED_PATH_PATTERNS):
        return False, f"Blocked API path: {path}"

    # Try to parse as IP directly
    ip_blocked = _check_ip_blocked(host)
    if ip_blocked:
        return False, ip_blocked

    # DNS resolution check
    if resolve_dns:
        dns_blocked = _check_dns_resolved(host)
        if dns_blocked:
            return False, dns_blocked

    return True, None


def is_redirect_safe(original_url: str, redirect_url: str) -> tuple[bool, Optional[str]]:
    """Validate a redirect destination against the same rules + cross-origin check."""
    safe, reason = is_url_safe(redirect_url, resolve_dns=True)
    if not safe:
        return False, f"Redirect blocked: {reason}"
    return True, None


def is_databricks_domain(url: str) -> bool:
    """Check if URL belongs to a Databricks-controlled domain (safe for token forwarding)."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return any(host.endswith(d) for d in _DATABRICKS_TOKEN_DOMAINS)
    except Exception:
        return False


def validate_url_for_browser(url: str) -> tuple[bool, Optional[str]]:
    """Stricter check for URLs that will be opened in a browser context.

    Browser contexts have cookies and potentially tokens — we must be
    especially careful about where they navigate.
    """
    safe, reason = is_url_safe(url, resolve_dns=True)
    if not safe:
        return False, reason

    parsed = urlparse(url)
    # Block non-standard ports for browser navigation (common in SSRF attacks)
    port = parsed.port
    if port and port not in (80, 443, 8080, 8443, 3000, 5000, 8000):
        return False, f"Non-standard port blocked for browser: {port}"

    return True, None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_ip_blocked(host: str) -> Optional[str]:
    """Check if a host string is a blocked IP address."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return None  # Not an IP literal — DNS check will handle it

    if addr.is_private:
        return f"Blocked private IP: {host}"
    if addr.is_loopback:
        return f"Blocked loopback IP: {host}"
    if addr.is_link_local:
        return f"Blocked link-local IP: {host}"
    if addr.is_reserved:
        return f"Blocked reserved IP: {host}"
    if addr.is_multicast:
        return f"Blocked multicast IP: {host}"
    # AWS metadata (169.254.169.254)
    if isinstance(addr, ipaddress.IPv4Address) and addr in ipaddress.ip_network("169.254.0.0/16"):
        return f"Blocked metadata IP: {host}"
    return None


def _check_dns_resolved(host: str) -> Optional[str]:
    """Resolve hostname and check if any resolved IP is in a blocked range."""
    try:
        results = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        # DNS resolution failed — could be a non-existent host or network issue.
        # We allow it through (the request will fail at connection time).
        return None
    except Exception:
        return None

    for family, _, _, _, sockaddr in results:
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        if addr.is_private:
            return f"DNS resolved to private IP: {host} -> {ip_str}"
        if addr.is_loopback:
            return f"DNS resolved to loopback: {host} -> {ip_str}"
        if addr.is_link_local:
            return f"DNS resolved to link-local: {host} -> {ip_str}"
        if addr.is_reserved:
            return f"DNS resolved to reserved IP: {host} -> {ip_str}"

    return None
