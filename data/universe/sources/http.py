"""Shared HTTP helpers for official universe sources."""

from __future__ import annotations

import threading
import time

import certifi
import requests

SYSTEM_CA_BUNDLE = "/etc/ssl/cert.pem"


class RateLimiter:
    """Enforce a max requests-per-second budget across a client."""

    def __init__(self, max_per_second: float = 9.0) -> None:
        self._min_interval = 1.0 / max_per_second
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        """Block until the next request is allowed by the rate budget."""
        with self._lock:
            now = time.monotonic()
            delay = self._min_interval - (now - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


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
