"""Unit tests for ``oon_bench.query.RateStore`` (the v1 query layer).

These tests build a SMALL, in-memory merged v1 dataset (the JSON shape produced
by the merge stage) and assert that ``get_rate`` honors the QUERY RESULT
contract:

  * a ``tic_oon_actual`` state with large N  -> basis tic_oon_actual, high conf,
    estimate.low/mid/high from p25/p50/p75, n_obs carried.
  * a ``medicare_multiple`` state (the v0 fallback band) -> basis
    medicare_multiple, low confidence, no n_obs.
  * region resolution falls back to the national ("US") row when the requested
    state is absent.
  * an unknown CPT returns ``None`` (callers raise / 404).

Plus a couple of guardrails: confidence degrades to "medium" for a thin
tic_oon_actual row and for an in-network proxy, list_codes returns the catalog,
and the by-locality CSV loader reconstructs an equivalent store.

Stdlib + pytest only. The repo-root sys.path insertion in tests/conftest.py
makes ``import oon_bench`` resolve to the package at the repo root.
"""

from __future__ import annotations

import csv
import json
import os

import pytest

from oon_bench.query import (
    BASIS_MEDICARE,
    BASIS_TIC_OON,
    BASIS_TIC_PROXY,
    HIGH_CONF_N,
    NATIONAL_REGION,
    RateStore,
)


SNAPSHOT = "2026-06-07"
METHODOLOGY = "v1-tic-2026A"


def _dataset() -> dict:
    """A minimal merged v1 dataset exercising every basis + region path.

    Codes:
      90837 — has a national (US) row, a CA tic_oon_actual row (large N -> high),
              a TX tic_innetwork_proxy row (-> medium), and a thin NY
              tic_oon_actual row (small N -> medium). No FL row (-> US fallback).
      90791 — only a medicare_multiple national + AL fallback row.
    """
    return {
        "meta": {
            "snapshot_date": SNAPSHOT,
            "methodology_version": METHODOLOGY,
            "sources": ["CMS PFS RVU26A", "TiC: uhc+aetna+cigna 2026Q2"],
            "disclaimer": "Estimate only, not a guarantee. v1 TiC-derived where available.",
            "min_observations": 10,
            "percentiles": [25, 50, 75, 90],
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
                    {
                        "state": "TX",
                        "locality_name": "TEXAS",
                        "medicare_usd": 160.00,
                        "oon_low_usd": 150.00,
                        "oon_mid_usd": 172.00,
                        "oon_high_usd": 195.00,
                        "oon_p90_usd": 220.00,
                        "oon_obs_n": 88,
                        "basis": BASIS_TIC_PROXY,
                        "payer_scope": "single",
                    },
                    {
                        "state": "NY",
                        "locality_name": "NYC",
                        "medicare_usd": 175.00,
                        "oon_low_usd": 170.00,
                        "oon_mid_usd": 190.00,
                        "oon_high_usd": 215.00,
                        "oon_p90_usd": 250.00,
                        "oon_obs_n": HIGH_CONF_N - 5,  # thin: still real OON, but medium
                        "basis": BASIS_TIC_OON,
                        "payer_scope": "single",
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
def store() -> RateStore:
    return RateStore(_dataset())


# --------------------------------------------------------------------------- #
# tic_oon_actual — large N -> high confidence
# --------------------------------------------------------------------------- #
class TestTicOonActual:
    def test_basis_and_confidence(self, store):
        res = store.get_rate("90837", "CA")
        assert res is not None
        assert res["cpt_code"] == "90837"
        assert res["region"] == "CA"
        assert res["basis"] == BASIS_TIC_OON
        assert res["confidence"] == "high"

    def test_estimate_from_percentiles(self, store):
        res = store.get_rate("90837", "CA")
        assert res["estimate"] == {"low": 165.00, "mid": 198.00, "high": 230.00}

    def test_n_obs_carried(self, store):
        res = store.get_rate("90837", "CA")
        assert res["n_obs"] == 512

    def test_provenance_fields(self, store):
        res = store.get_rate("90837", "CA")
        assert res["snapshot_date"] == SNAPSHOT
        assert "Transparency-in-Coverage" in res["disclaimer"] or "TiC" in res["disclaimer"]
        assert res["source"]

    def test_service_label_is_plain_language(self, store):
        # Our own label, never an AMA descriptor.
        res = store.get_rate("90837", "CA")
        assert res["service_label"] == "Individual therapy, 60 minutes"

    def test_thin_oon_actual_is_medium(self, store):
        # Real OON data but below HIGH_CONF_N -> medium, not high.
        res = store.get_rate("90837", "NY")
        assert res["basis"] == BASIS_TIC_OON
        assert res["confidence"] == "medium"
        assert res["n_obs"] == HIGH_CONF_N - 5


# --------------------------------------------------------------------------- #
# tic_innetwork_proxy -> medium confidence
# --------------------------------------------------------------------------- #
class TestProxy:
    def test_proxy_is_medium(self, store):
        res = store.get_rate("90837", "TX")
        assert res["basis"] == BASIS_TIC_PROXY
        assert res["confidence"] == "medium"
        assert res["estimate"]["low"] == 150.00
        assert res["n_obs"] == 88


# --------------------------------------------------------------------------- #
# medicare_multiple — the v0 fallback band -> low confidence
# --------------------------------------------------------------------------- #
class TestMedicareFallback:
    def test_basis_and_confidence(self, store):
        res = store.get_rate("90791", "AL")
        assert res is not None
        assert res["region"] == "AL"
        assert res["basis"] == BASIS_MEDICARE
        assert res["confidence"] == "low"

    def test_estimate_is_medicare_band(self, store):
        res = store.get_rate("90791", "AL")
        assert res["estimate"]["low"] == 167.51
        assert res["estimate"]["high"] == 335.02
        assert res["estimate"]["mid"] is None

    def test_no_observations(self, store):
        res = store.get_rate("90791", "AL")
        assert res["n_obs"] is None


# --------------------------------------------------------------------------- #
# Region resolution: exact state -> US national -> medicare fallback
# --------------------------------------------------------------------------- #
class TestRegionResolution:
    def test_unknown_state_falls_back_to_national(self, store):
        # FL is absent for 90837; resolve to the national ("US") row.
        res = store.get_rate("90837", "FL")
        assert res is not None
        assert res["region"] == NATIONAL_REGION
        assert res["basis"] == BASIS_TIC_OON
        assert res["estimate"]["low"] == 150.00  # national p25
        assert res["n_obs"] == 4200

    def test_explicit_us_region(self, store):
        res = store.get_rate("90837", NATIONAL_REGION)
        assert res["region"] == NATIONAL_REGION
        assert res["estimate"]["mid"] == 185.00

    def test_default_region_is_national(self, store):
        res = store.get_rate("90837")
        assert res["region"] == NATIONAL_REGION

    def test_lowercase_state_resolves(self, store):
        res = store.get_rate("90837", "ca")
        assert res["region"] == "CA"
        assert res["basis"] == BASIS_TIC_OON

    def test_unknown_state_for_medicare_only_code_falls_back(self, store):
        # 90791 has no WY row -> US national medicare band.
        res = store.get_rate("90791", "WY")
        assert res["region"] == NATIONAL_REGION
        assert res["basis"] == BASIS_MEDICARE
        assert res["estimate"]["low"] == 173.35


# --------------------------------------------------------------------------- #
# Unknown CPT -> None
# --------------------------------------------------------------------------- #
class TestUnknownCpt:
    def test_returns_none(self, store):
        assert store.get_rate("99999", "CA") is None

    def test_returns_none_national(self, store):
        assert store.get_rate("00000") is None

    def test_has_code(self, store):
        assert store.has_code("90837")
        assert not store.has_code("99999")


# --------------------------------------------------------------------------- #
# list_codes — the catalog
# --------------------------------------------------------------------------- #
class TestListCodes:
    def test_catalog_shape(self, store):
        codes = store.list_codes()
        assert {c["cpt_code"] for c in codes} == {"90837", "90791"}
        for c in codes:
            assert set(c.keys()) == {"cpt_code", "service_label", "medicare_status"}

    def test_catalog_preserves_order(self, store):
        codes = store.list_codes()
        assert [c["cpt_code"] for c in codes] == ["90837", "90791"]


# --------------------------------------------------------------------------- #
# Loading from disk: JSON round-trip + by-locality CSV
# --------------------------------------------------------------------------- #
class TestFromFile:
    def test_from_json_file(self, tmp_path):
        p = tmp_path / "v1.json"
        p.write_text(json.dumps(_dataset()), encoding="utf-8")
        store = RateStore.from_file(str(p))
        res = store.get_rate("90837", "CA")
        assert res["basis"] == BASIS_TIC_OON
        assert res["confidence"] == "high"

    def test_from_locality_csv(self, tmp_path):
        # Build a by-locality CSV in the v1 column layout and confirm the loader
        # reconstructs an equivalent store (state percentiles + basis carried).
        p = tmp_path / "v1_by_locality.csv"
        fieldnames = [
            "cpt_code", "service_label", "medicare_status", "state", "locality_name",
            "medicare_nonfacility_usd", "oon_low_usd", "oon_high_usd",
            "oon_mid_usd", "oon_p90_usd", "oon_obs_n", "basis", "payer_scope",
            "snapshot_date", "methodology_version",
        ]
        rows = [
            {
                "cpt_code": "90837", "service_label": "Individual therapy, 60 minutes",
                "medicare_status": "A", "state": "CA", "locality_name": "LOS ANGELES",
                "medicare_nonfacility_usd": "179.29", "oon_low_usd": "165.00",
                "oon_high_usd": "230.00", "oon_mid_usd": "198.00", "oon_p90_usd": "265.00",
                "oon_obs_n": "512", "basis": BASIS_TIC_OON, "payer_scope": "multi",
                "snapshot_date": SNAPSHOT, "methodology_version": METHODOLOGY,
            },
            {
                "cpt_code": "90791", "service_label": "Diagnostic intake / first evaluation",
                "medicare_status": "A", "state": "AL", "locality_name": "ALABAMA",
                "medicare_nonfacility_usd": "167.51", "oon_low_usd": "167.51",
                "oon_high_usd": "335.02", "oon_mid_usd": "", "oon_p90_usd": "",
                "oon_obs_n": "", "basis": BASIS_MEDICARE, "payer_scope": "",
                "snapshot_date": SNAPSHOT, "methodology_version": METHODOLOGY,
            },
        ]
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)

        store = RateStore.from_file(str(p))
        ca = store.get_rate("90837", "CA")
        assert ca["basis"] == BASIS_TIC_OON
        assert ca["confidence"] == "high"
        assert ca["estimate"] == {"low": 165.00, "mid": 198.00, "high": 230.00}
        assert ca["n_obs"] == 512

        al = store.get_rate("90791", "AL")
        assert al["basis"] == BASIS_MEDICARE
        assert al["confidence"] == "low"
        assert al["estimate"]["mid"] is None
        assert al["n_obs"] is None

        assert store.get_rate("99999", "CA") is None

    def test_from_v0_locality_csv_loads_as_medicare(self, tmp_path):
        # The real committed v0 by-locality CSV (legacy columns, no v1 fields)
        # must load as an all-medicare_multiple store without error.
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        v0_csv = os.path.join(repo_root, "data", "therapy_oon_benchmark_v0_by_locality.csv")
        if not os.path.exists(v0_csv):
            pytest.skip("v0 by-locality CSV not present")
        store = RateStore.from_file(v0_csv)
        res = store.get_rate("90791", "AL")
        assert res is not None
        assert res["basis"] == BASIS_MEDICARE
        assert res["confidence"] == "low"
        # Unknown CPT still None against the real dataset.
        assert store.get_rate("12345", "AL") is None


# --------------------------------------------------------------------------- #
# by_payer breakout surfaced on the rate result
# --------------------------------------------------------------------------- #
class TestByPayer:
    def _store(self, by_payer):
        ds = {
            "meta": {"snapshot_date": "2026-06-07", "disclaimer": "estimate, not a guarantee"},
            "codes": [{
                "cpt_code": "90837", "service_label": "Individual therapy, 60 minutes",
                "medicare_status": "A",
                "national": {"medicare_usd": 167.0, "basis": "tic_innetwork_proxy",
                             "oon_low_usd": 116.0, "oon_mid_usd": 130.0, "oon_high_usd": 164.0,
                             "oon_obs_n": 80000, "payer_scope": "multi"},
                "localities": [], **({"by_payer": by_payer} if by_payer is not None else {})},
            ],
        }
        return RateStore(ds)

    def test_by_payer_surfaced(self):
        bp = {"uhc": {"n_obs": 64000, "median": 130.0},
              "cigna": {"n_obs": 1000, "median": 164.0}}
        r = self._store(bp).get_rate("90837", "US")
        assert set(r["by_payer"]) == {"uhc", "cigna"}
        assert r["by_payer"]["cigna"]["median"] == 164.0

    def test_by_payer_absent_is_empty_dict(self):
        r = self._store(None).get_rate("90837", "US")
        assert r["by_payer"] == {}

    def test_catalog_excludes_by_payer(self):
        bp = {"uhc": {"n_obs": 1, "median": 1.0}}
        codes = self._store(bp).list_codes()
        assert all("by_payer" not in c for c in codes)
