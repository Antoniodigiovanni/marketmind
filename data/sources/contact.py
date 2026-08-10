"""Operator contact details for courtesy-access data sources.

SEC EDGAR's fair-access policy requires every request to identify the operator
with a real name and email address so they can make contact before throttling
or blocking; requests carrying a fake or absent contact are rate-limited and
IP-blocked. filings.xbrl.org is a comparable free, best-effort service and is
given the same courtesy.

Both come from the environment rather than a hardcoded constant, so a
deployment cannot silently ship someone else's address, and a missing value
fails loudly before any request is made instead of quietly earning a block.
"""

from __future__ import annotations

import os
import re

CONTACT_NAME_ENV = "SEC_CONTACT_NAME"
CONTACT_EMAIL_ENV = "SEC_CONTACT_EMAIL"

# Deliberately permissive: the point is to catch an unset or obviously fake
# value, not to adjudicate RFC 5322.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Reserved for documentation (RFC 2606). An address at one of these reaches
# nobody, which is precisely what fair access forbids.
_PLACEHOLDER_DOMAINS = frozenset({"example.com", "example.org", "example.net", "example.edu"})


class ContactNotConfigured(RuntimeError):
    """Raised when the operator contact is missing, malformed, or a placeholder."""


def contact_user_agent(purpose: str) -> str:
    """Build a User-Agent carrying the operator's real name and email.

    Args:
        purpose: Short description of what the client fetches, included in the
            header so a recipient can tell one job from another.

    Returns:
        A User-Agent string identifying the client and the operator.

    Raises:
        ContactNotConfigured: If either environment variable is unset, the
            address is malformed, or it uses a reserved documentation domain.
    """
    name = (os.getenv(CONTACT_NAME_ENV) or "").strip()
    email = (os.getenv(CONTACT_EMAIL_ENV) or "").strip()

    if not name:
        raise ContactNotConfigured(
            f"{CONTACT_NAME_ENV} is not set. SEC fair access requires a real "
            "contact name on every request."
        )
    if not email:
        raise ContactNotConfigured(
            f"{CONTACT_EMAIL_ENV} is not set. SEC fair access requires a real "
            "contact email on every request."
        )
    if not _EMAIL_RE.match(email):
        raise ContactNotConfigured(f"{CONTACT_EMAIL_ENV} is not a valid email address: {email!r}")
    if email.rsplit("@", 1)[1].lower() in _PLACEHOLDER_DOMAINS:
        raise ContactNotConfigured(
            f"{CONTACT_EMAIL_ENV} still uses a documentation domain ({email!r}). "
            "Set an address that actually reaches you."
        )

    return f"marketmind/0.1 ({purpose}; contact: {name} <{email}>)"
