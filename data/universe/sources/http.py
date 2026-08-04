"""Shared HTTP helpers for official universe sources."""

from __future__ import annotations

import certifi
import requests

SYSTEM_CA_BUNDLE = "/etc/ssl/cert.pem"


def get_with_ca_fallback(
    url: str,
    *,
    session: requests.Session | None = None,
    params: dict[str, str] | None = None,
    timeout: int = 60,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """GET a URL, retrying once with the system CA bundle on TLS errors.

    certifi's bundle fails to verify some issuer chains on macOS (stoxx.com is
    one example); the system bundle at /etc/ssl/cert.pem verifies them.

    Args:
        url: Request URL.
        session: Optional session to reuse.
        params: Query parameters.
        timeout: Request timeout in seconds.
        headers: Request headers.

    Returns:
        The response from the first successful attempt.

    Raises:
        requests.RequestException: If both attempts fail.
    """
    http = session or requests.Session()
    try:
        return http.get(
            url, params=params, timeout=timeout, headers=headers, verify=certifi.where()
        )
    except requests.exceptions.SSLError:
        return http.get(
            url, params=params, timeout=timeout, headers=headers, verify=SYSTEM_CA_BUNDLE
        )
