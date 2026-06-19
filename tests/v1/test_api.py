"""HTTP-surface tests for ``oon_bench.api`` (the read-only FastAPI backend).

These exercise the endpoint contract end-to-end with ``fastapi.testclient``:

  * GET /health           -> {"status": "ok", "snapshot_date", "codes": N}
  * GET /v1/codes         -> the code catalog
  * GET /v1/rates/{cpt}   -> the QUERY RESULT dict (200) for a known code,
                              with region resolution honored
  * GET /v1/rates/{cpt}   -> 404 JSON for an unknown code

The app is built with ``create_app(store)`` against a RateStore loaded from a
tiny temp dataset, so the suite never touches the committed data files or the
network. The whole module is guarded by ``pytest.importorskip("fastapi")`` so it
is silently skipped in environments where FastAPI / Starlette is not installed
(the data pipeline itself is stdlib-only; FastAPI is an API-only dependency).

Run only this module:
    pip install -r requirements-api.txt
    python -m pytest tests/v1/test_api.py -q
"""

from __future__ import annotations

import json

import pytest

# Guard the ENTIRE module: if FastAPI (or its TestClient stack) is absent, skip
# rather than error, so `pytest` still passes where only the stdlib pipeline is
# installed. TestClient additionally needs httpx; importorskip covers both via
# the fastapi.testclient import below.
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from oon_bench.api import create_app  # noqa: E402
from oon_bench.query import (  # noqa: E402
    BASIS_MEDICARE,
    BASIS_TIC_OON,
    RateStore,
)

SNAPSHOT = "2026-06-07"
METHODOLOGY = "v1-tic-2026A"


def _dataset() -> dict:
    """A two-code merged v1 dataset: one measured OON code, one Medicare fallback."""
    return {
        "meta": {
            "snapshot_date": SNAPSHOT,
            "methodology_version": METHODOLOGY,
            "sources": ["CMS PFS RVU26A", "TiC: uhc+aetna+cigna 2026Q2"],
            "disclaimer": "Estimate only, not a guarantee.",
        },
        "codes": [
            {
                "cpt_code": "90837",
                "service_label": "Individual therapy, 60 minutes",
                "medicare_status": "A",
                "national": {
                    "medicare_usd": 167.00,
                    "oon_low_usd": 150.00,
                    "oon_mid_usd": 185.00,
                    "oon_high_usd": 210.00,
                    "oon_p90_usd": 240.00,
                    "oon_obs_n": 4200,
                    "basis": BASIS_TIC_OON,
                    "payer_scope": "multi",
                },
                "localities": [
                    {
                        "state": "CA",
                        "locality_name": "LOS ANGELES",
                        "medicare_usd": 179.29,
                        "oon_low_usd": 165.00,
                        "oon_mid_usd": 198.00,
                        "oon_high_usd": 230.00,
                        "oon_p90_usd": 265.00,
                        "oon_obs_n": 512,
                        "basis": BASIS_TIC_OON,
                        "payer_scope": "multi",
                    },
                ],
            },
            {
                "cpt_code": "90791",
                "service_label": "Diagnostic intake / first evaluation",
                "medicare_status": "A",
                "national": {
                    "medicare_usd": 173.35,
                    "oon_low_usd": 173.35,
                    "oon_mid_usd": None,
                    "oon_high_usd": 346.70,
                    "oon_p90_usd": None,
                    "oon_obs_n": None,
                    "basis": BASIS_MEDICARE,
                    "payer_scope": None,
                },
                "localities": [
                    {
                        "state": "AL",
                        "locality_name": "ALABAMA",
                        "medicare_usd": 167.51,
                        "oon_low_usd": 167.51,
                        "oon_mid_usd": None,
                        "oon_high_usd": 335.02,
                        "oon_p90_usd": None,
                        "oon_obs_n": None,
                        "basis": BASIS_MEDICARE,
                        "payer_scope": None,
                    },
                ],
            },
        ],
    }


@pytest.fixture()
def client(tmp_path) -> TestClient:
    """A TestClient over an app loaded from a tiny temp dataset on disk.

    Writing the dataset to a file and loading via RateStore.from_file mirrors the
    real startup path (build_store -> RateStore.from_file) while staying hermetic.
    """
    data_path = tmp_path / "v1.json"
    data_path.write_text(json.dumps(_dataset()), encoding="utf-8")
    store = RateStore.from_file(str(data_path))
    app = create_app(store=store)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #
class TestHealth:
    def test_ok_shape(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["snapshot_date"] == SNAPSHOT
        assert body["codes"] == 2

    def test_health_keys(self, client):
        body = client.get("/health").json()
        assert set(body.keys()) == {"status", "snapshot_date", "codes"}


# --------------------------------------------------------------------------- #
# /v1/codes
# --------------------------------------------------------------------------- #
class TestCodes:
    def test_catalog(self, client):
        resp = client.get("/v1/codes")
        assert resp.status_code == 200
        codes = resp.json()
        assert isinstance(codes, list)
        assert {c["cpt_code"] for c in codes} == {"90837", "90791"}

    def test_catalog_fields(self, client):
        codes = client.get("/v1/codes").json()
        for c in codes:
            assert set(c.keys()) == {"cpt_code", "service_label", "medicare_status"}
        # Our own plain-language label, never an AMA descriptor.
        labels = {c["cpt_code"]: c["service_label"] for c in codes}
        assert labels["90837"] == "Individual therapy, 60 minutes"


# --------------------------------------------------------------------------- #
# /v1/rates/{cpt}
# --------------------------------------------------------------------------- #
class TestRates:
    def test_known_rate_state(self, client):
        resp = client.get("/v1/rates/90837", params={"region": "CA"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["cpt_code"] == "90837"
        assert body["service_label"] == "Individual therapy, 60 minutes"
        assert body["region"] == "CA"
        assert body["basis"] == BASIS_TIC_OON
        assert body["confidence"] == "high"
        assert body["estimate"] == {"low": 165.00, "mid": 198.00, "high": 230.00}
        assert body["n_obs"] == 512
        assert body["snapshot_date"] == SNAPSHOT
        assert body["disclaimer"]
        assert body["source"]

    def test_rate_result_keys(self, client):
        body = client.get("/v1/rates/90837").json()
        assert set(body.keys()) == {
            "cpt_code",
            "service_label",
            "region",
            "basis",
            "estimate",
            "confidence",
            "n_obs",
            "by_payer",
            "source",
            "snapshot_date",
            "disclaimer",
        }
        assert set(body["estimate"].keys()) == {"low", "mid", "high"}

    def test_default_region_is_national(self, client):
        # No region param -> national ("US") row.
        body = client.get("/v1/rates/90837").json()
        assert body["region"] == "US"
        assert body["estimate"]["mid"] == 185.00

    def test_unknown_state_falls_back_to_national(self, client):
        # FL is absent for 90837 -> degrade to the national row, not an error.
        resp = client.get("/v1/rates/90837", params={"region": "FL"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["region"] == "US"
        assert body["basis"] == BASIS_TIC_OON

    def test_medicare_fallback_code(self, client):
        body = client.get("/v1/rates/90791", params={"region": "AL"}).json()
        assert body["basis"] == BASIS_MEDICARE
        assert body["confidence"] == "low"
        assert body["estimate"]["low"] == 167.51
        assert body["estimate"]["high"] == 335.02
        assert body["estimate"]["mid"] is None
        assert body["n_obs"] is None

    def test_unknown_cpt_is_404(self, client):
        resp = client.get("/v1/rates/99999", params={"region": "CA"})
        assert resp.status_code == 404
        body = resp.json()
        assert "detail" in body
        assert "99999" in body["detail"]

    def test_unknown_cpt_404_without_region(self, client):
        resp = client.get("/v1/rates/00000")
        assert resp.status_code == 404
        assert "detail" in resp.json()
