"""Tests for FRED and ECB macro fetchers (mocked HTTP/API)."""

from __future__ import annotations

import pandas as pd
import pytest

from data.sources.ecb import fetch_series as fetch_ecb
from data.sources.fred import fetch_series as fetch_fred


def test_fred_fetch_series_mocked() -> None:
    class FakeFred:
        def get_series(
            self, series_id: str, start: str | None = None, end: str | None = None
        ) -> pd.Series:
            return pd.Series(
                [2.5, 2.6],
                index=pd.to_datetime(["2026-08-01", "2026-08-04"]),
                name=series_id,
            )

    df = fetch_fred("DGS10", "2026-08-01", "2026-08-05", fred_client=FakeFred())
    assert df.columns == ["date", "series_id", "value"]
    assert df.height == 2
    assert df["series_id"].to_list() == ["DGS10", "DGS10"]


def test_fred_missing_api_key_raises() -> None:
    with pytest.raises(ValueError, match="FRED_API_KEY"):
        fetch_fred("DGS10", "2026-08-01", "2026-08-05", fred_client=None)


def test_ecb_fetch_series_mocked(monkeypatch) -> None:
    import requests

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "dataSets": [
                    {"series": {"0:0:0:0:0": {"observations": {"0": ["2.0"], "1": ["2.1"]}}}}
                ],
                "structure": {
                    "dimensions": {
                        "series": [{"values": [{"name": "Deposit facility"}]}],
                        "observation": [{"values": [{"id": "2026-08-01"}, {"id": "2026-08-04"}]}],
                    }
                },
            }

    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse())
    df = fetch_ecb("FM.B.U2.EUR.FR.BB.U2.EUR.MR.R", "2026-08-01", "2026-08-05")
    assert df.columns == ["date", "series_id", "value"]
    assert df.height == 2
    assert df["value"].to_list() == [2.0, 2.1]
