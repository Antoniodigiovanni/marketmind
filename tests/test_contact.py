"""Tests for the operator contact used by courtesy-access data sources."""

from __future__ import annotations

import pytest

from data.sources.contact import (
    CONTACT_EMAIL_ENV,
    CONTACT_NAME_ENV,
    ContactNotConfigured,
    contact_user_agent,
)


def test_user_agent_carries_the_name_email_and_purpose(monkeypatch) -> None:
    monkeypatch.setenv(CONTACT_NAME_ENV, "Jane Roe")
    monkeypatch.setenv(CONTACT_EMAIL_ENV, "jane@roe-research.dev")

    agent = contact_user_agent("SEC EDGAR ingest")

    assert "Jane Roe" in agent
    assert "jane@roe-research.dev" in agent
    assert "SEC EDGAR ingest" in agent
    assert "marketmind" in agent


def test_missing_name_raises(monkeypatch) -> None:
    monkeypatch.delenv(CONTACT_NAME_ENV, raising=False)
    monkeypatch.setenv(CONTACT_EMAIL_ENV, "jane@roe-research.dev")

    with pytest.raises(ContactNotConfigured, match=CONTACT_NAME_ENV):
        contact_user_agent("SEC EDGAR ingest")


def test_missing_email_raises(monkeypatch) -> None:
    monkeypatch.setenv(CONTACT_NAME_ENV, "Jane Roe")
    monkeypatch.delenv(CONTACT_EMAIL_ENV, raising=False)

    with pytest.raises(ContactNotConfigured, match=CONTACT_EMAIL_ENV):
        contact_user_agent("SEC EDGAR ingest")


def test_blank_values_are_treated_as_missing(monkeypatch) -> None:
    monkeypatch.setenv(CONTACT_NAME_ENV, "   ")
    monkeypatch.setenv(CONTACT_EMAIL_ENV, "jane@roe-research.dev")

    with pytest.raises(ContactNotConfigured, match=CONTACT_NAME_ENV):
        contact_user_agent("SEC EDGAR ingest")


@pytest.mark.parametrize("bad", ["not-an-email", "no@tld", "@nolocal.dev", "two@@at.dev"])
def test_malformed_email_raises(monkeypatch, bad: str) -> None:
    monkeypatch.setenv(CONTACT_NAME_ENV, "Jane Roe")
    monkeypatch.setenv(CONTACT_EMAIL_ENV, bad)

    with pytest.raises(ContactNotConfigured, match="valid email"):
        contact_user_agent("SEC EDGAR ingest")


@pytest.mark.parametrize("domain", ["example.com", "EXAMPLE.COM", "example.org", "example.net"])
def test_documentation_domains_are_rejected(monkeypatch, domain: str) -> None:
    """The placeholder shipped in .env.example must not reach SEC."""
    monkeypatch.setenv(CONTACT_NAME_ENV, "Jane Roe")
    monkeypatch.setenv(CONTACT_EMAIL_ENV, f"jane@{domain}")

    with pytest.raises(ContactNotConfigured, match="documentation domain"):
        contact_user_agent("SEC EDGAR ingest")


def test_edgar_client_refuses_to_build_without_a_contact(monkeypatch) -> None:
    """The guard fires at client construction, before any request is made."""
    from data.sources.sec import EdgardClient

    monkeypatch.delenv(CONTACT_NAME_ENV, raising=False)
    monkeypatch.delenv(CONTACT_EMAIL_ENV, raising=False)

    with pytest.raises(ContactNotConfigured):
        EdgardClient()


def test_xbrlorg_client_refuses_to_build_without_a_contact(monkeypatch) -> None:
    from data.sources.xbrlorg import XbrlorgClient

    monkeypatch.delenv(CONTACT_NAME_ENV, raising=False)
    monkeypatch.delenv(CONTACT_EMAIL_ENV, raising=False)

    with pytest.raises(ContactNotConfigured):
        XbrlorgClient()


def test_both_clients_send_the_configured_contact(monkeypatch) -> None:
    """Round-trip: the header the client actually sets carries the real values."""
    from data.sources.sec import EdgardClient
    from data.sources.xbrlorg import XbrlorgClient

    monkeypatch.setenv(CONTACT_NAME_ENV, "Jane Roe")
    monkeypatch.setenv(CONTACT_EMAIL_ENV, "jane@roe-research.dev")

    for client in (EdgardClient(), XbrlorgClient()):
        header = client._session.headers["User-Agent"]
        assert "Jane Roe" in header
        assert "jane@roe-research.dev" in header
        assert "example.com" not in header
