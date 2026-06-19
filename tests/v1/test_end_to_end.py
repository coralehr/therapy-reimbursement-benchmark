"""End-to-end test for the v1 pipeline over the bundled synthetic fixtures.

Runs the REAL stages in-process (no subprocess, no network, no real payer data):

    filter (v1_tic/filter_mrf.py)  ->  aggregate (oon_bench.aggregate)
        ->  merge (oon_bench.merge)  ->  query (oon_bench.query.RateStore)

and asserts the contract-level guarantees that matter for a consumer:

  * the expected BASIS DISTRIBUTION appears — at least one ``tic_oon_actual``
    (real OON allowed-amount percentiles cleared MIN_N) AND at least one
    ``medicare_multiple`` (states with no qualifying TiC fall back to the v0 band);
  * a ``tic_innetwork_proxy`` row appears where only negotiated (in-network) data
    cleared MIN_N for a state;
  * the fixtures' deliberately-bad rows NEVER reach the output:
      - non-therapy codes (99213, lab PLA 0202U),
      - institutional / facility rows,
      - zero / negative amounts;
  * every merged row carries a basis from the allowed enum, and a TiC row carries
    a positive observation count and ordered percentiles;
  * the query layer reads the merged dataset and reports basis + confidence
    consistently with the row it resolved.

The fixtures live in ``oon_bench/fixtures/`` and are structurally-realistic TiC
MRFs (in-network-rates shape + allowed-amounts shape) — synthetic, not real payer
data. ``oon_bench/run_local.py`` runs the same pipeline as a human smoke test;
this file is the automated assertion of the same path.

Stdlib + pytest only. Nothing here downloads anything.
"""

from __future__ import annotations

import io
import json
import os

import pytest

from oon_bench import aggregate as agg_stage
from oon_bench import merge as merge_stage
from oon_bench import schemas
from oon_bench.query import RateStore

# tests/v1/test_end_to_end.py -> tests/v1 -> tests -> repo root
_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))
FIXTURE_DIR = os.path.join(REPO_ROOT, "oon_bench", "fixtures")
INNETWORK_FIXTURE = os.path.join(FIXTURE_DIR, "mrf_innetwork_sample.json")
ALLOWED_FIXTURE = os.path.join(FIXTURE_DIR, "mrf_allowed_sample.json")
V0_BY_LOCALITY = os.path.join(
    REPO_ROOT, "data", "therapy_oon_benchmark_v0_by_locality.csv"
)

# Codes the fixtures deliberately include that MUST be filtered out everywhere.
NON_THERAPY_CODES = {"99213", "0202U"}
BASES = {
    schemas.BASIS_OON_ACTUAL,
    schemas.BASIS_INNETWORK_PROXY,
    schemas.BASIS_MEDICARE_MULTIPLE,
}


def _import_filter():
    """Import v1_tic/filter_mrf.py (a plain module, not a package)."""
    import sys

    v1_tic = os.path.join(REPO_ROOT, "v1_tic")
    if v1_tic not in sys.path:
        sys.path.insert(0, v1_tic)
    import filter_mrf  # noqa: WPS433 (intentional local import after path setup)

    return filter_mrf


def _filter(fixture_path: str, kind: str) -> list[dict]:
    """Stream a fixture through the REAL filter and return the parsed JSONL rows."""
    filter_mrf = _import_filter()
    buf = io.StringIO()
    filter_mrf.run_filter(
        fixture_path,
        payer="fixturehealth",
        kind=kind,
        region_hint=None,
        out=buf,
    )
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Fixtures (pytest) — run the pipeline once, share across assertions.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def filtered_rows() -> list[dict]:
    """All therapy rows the filter kept from BOTH fixtures (in-network + allowed)."""
    return _filter(INNETWORK_FIXTURE, "in-network") + _filter(ALLOWED_FIXTURE, "allowed")


@pytest.fixture(scope="module")
def aggregate_records(filtered_rows) -> list[dict]:
    return agg_stage.aggregate_rows(filtered_rows)


@pytest.fixture(scope="module")
def merged(aggregate_records, tmp_path_factory) -> dict:
    out_dir = tmp_path_factory.mktemp("v1_e2e")
    return merge_stage.merge(
        aggregate_records,
        baseline_csv=V0_BY_LOCALITY,
        out_dir=str(out_dir),
    )


# --------------------------------------------------------------------------- #
# Filter stage — bad rows rejected at ingestion.
# --------------------------------------------------------------------------- #
class TestFilterStage:
    def test_only_therapy_codes_survive_filter(self, filtered_rows):
        codes = {r["billing_code"] for r in filtered_rows}
        assert codes  # something survived
        assert codes <= schemas.THERAPY_CODE_SET
        assert not (codes & NON_THERAPY_CODES)

    def test_filter_kept_both_amount_kinds(self, filtered_rows):
        kinds = {r["amount_kind"] for r in filtered_rows}
        assert "allowed" in kinds
        assert "negotiated" in kinds


# --------------------------------------------------------------------------- #
# Aggregate stage — MIN_N + percentile + drop rules.
# --------------------------------------------------------------------------- #
class TestAggregateStage:
    def test_at_least_one_allowed_group_cleared_min_n(self, aggregate_records):
        allowed = [r for r in aggregate_records if r["amount_kind"] == "allowed"]
        assert allowed, "no OON allowed group cleared MIN_N — fixtures too thin"
        for r in allowed:
            assert r["n_obs"] >= schemas.MIN_N

    def test_at_least_one_negotiated_group_cleared_min_n(self, aggregate_records):
        negotiated = [r for r in aggregate_records if r["amount_kind"] == "negotiated"]
        assert negotiated, "no in-network group cleared MIN_N — fixtures too thin"

    def test_aggregate_only_emits_therapy_codes(self, aggregate_records):
        codes = {r["cpt_code"] for r in aggregate_records}
        assert codes <= schemas.THERAPY_CODE_SET
        assert not (codes & NON_THERAPY_CODES)

    def test_percentiles_are_ordered_and_positive(self, aggregate_records):
        for r in aggregate_records:
            assert 0 < r["min"] <= r["p25"] <= r["p50"] <= r["p75"] <= r["p90"] <= r["max"]

    def test_institutional_and_nonpositive_never_inflate_a_group(self, aggregate_records):
        # The allowed 96132/CA fixture's professional obs are ~155..240; an
        # institutional 9000-style row or a 0/-42 row would blow min/max if it
        # leaked. Assert the surviving distribution stays in the professional band.
        for r in aggregate_records:
            assert r["max"] < 1000.0  # no facility/garbage mega-amount leaked
            assert r["min"] > 0.0


# --------------------------------------------------------------------------- #
# Merge stage — basis distribution + provenance.
# --------------------------------------------------------------------------- #
class TestMergeBasisDistribution:
    def test_has_at_least_one_tic_oon_actual(self, merged):
        bases = {r["basis"] for r in merged["by_locality_rows"]}
        assert schemas.BASIS_OON_ACTUAL in bases

    def test_has_at_least_one_medicare_multiple(self, merged):
        bases = {r["basis"] for r in merged["by_locality_rows"]}
        assert schemas.BASIS_MEDICARE_MULTIPLE in bases

    def test_has_at_least_one_innetwork_proxy(self, merged):
        bases = {r["basis"] for r in merged["by_locality_rows"]}
        assert schemas.BASIS_INNETWORK_PROXY in bases

    def test_every_row_has_a_known_basis(self, merged):
        for r in merged["by_locality_rows"]:
            assert r["basis"] in BASES

    def test_basis_counts_sum_to_row_total(self, merged):
        counts = merged["meta"]["basis_counts_by_locality"]
        assert sum(counts.values()) == len(merged["by_locality_rows"])

    def test_no_non_therapy_code_in_merged_output(self, merged):
        codes = {r["cpt_code"] for r in merged["by_locality_rows"]}
        assert not (codes & NON_THERAPY_CODES)
        codes_nat = {r["cpt_code"] for r in merged["national_rows"]}
        assert not (codes_nat & NON_THERAPY_CODES)


class TestMergeProvenance:
    def test_tic_rows_carry_obs_and_ordered_percentiles(self, merged):
        tic = [
            r
            for r in merged["by_locality_rows"]
            if r["basis"] in (schemas.BASIS_OON_ACTUAL, schemas.BASIS_INNETWORK_PROXY)
        ]
        assert tic
        for r in tic:
            assert r["oon_obs_n"] is not None and r["oon_obs_n"] >= schemas.MIN_N
            assert r["payer_scope"] in ("single", "multi")
            # low(p25) <= mid(p50) <= high(p75) <= p90
            assert (
                r["oon_low_usd"]
                <= r["oon_mid_usd"]
                <= r["oon_high_usd"]
                <= r["oon_p90_usd"]
            )

    def test_medicare_rows_are_band_with_no_obs(self, merged):
        med = [
            r
            for r in merged["by_locality_rows"]
            if r["basis"] == schemas.BASIS_MEDICARE_MULTIPLE
        ]
        assert med
        for r in med:
            # Fallback band: low == medicare, high == round(2x), no percentiles/obs.
            assert r["oon_obs_n"] is None
            assert r["payer_scope"] is None
            assert r["oon_mid_usd"] is None
            assert r["oon_p90_usd"] is None
            assert r["oon_low_usd"] == pytest.approx(r["medicare_nonfacility_usd"])
            assert r["oon_high_usd"] == pytest.approx(
                round(r["medicare_nonfacility_usd"] * schemas.MEDICARE_MULT_HIGH, 2)
            )

    def test_tic_state_applies_to_every_locality_in_that_state(self, merged):
        # 96132 cleared MIN_N for 'allowed' in CA -> every CA locality for 96132
        # must be tic_oon_actual (TiC is state-level).
        ca_96132 = [
            r
            for r in merged["by_locality_rows"]
            if r["cpt_code"] == "96132" and r["state"] == "CA"
        ]
        assert ca_96132
        assert all(r["basis"] == schemas.BASIS_OON_ACTUAL for r in ca_96132)


# --------------------------------------------------------------------------- #
# Query stage — read the merged dataset, basis + confidence consistent.
# --------------------------------------------------------------------------- #
class TestQueryOverMerged:
    @pytest.fixture(scope="class")
    def store(self, merged) -> RateStore:
        return RateStore.from_file(merged["paths"]["json"])

    def test_oon_actual_query_is_tic_and_measured(self, store):
        res = store.get_rate("96132", "CA")
        assert res is not None
        assert res["basis"] == schemas.BASIS_OON_ACTUAL
        assert res["confidence"] in ("high", "medium")
        assert res["n_obs"] is not None and res["n_obs"] >= schemas.MIN_N
        assert res["estimate"]["low"] <= res["estimate"]["high"]

    def test_proxy_query_is_medium_confidence(self, store):
        res = store.get_rate("90837", "CA")
        assert res is not None
        # CA/90837 only has negotiated data -> proxy.
        assert res["basis"] == schemas.BASIS_INNETWORK_PROXY
        assert res["confidence"] == "medium"

    def test_fallback_state_is_medicare_low_confidence(self, store):
        # A code/state with no qualifying TiC -> medicare_multiple, low confidence.
        # 90832 has no fixture data anywhere; any state falls back.
        res = store.get_rate("90832", "TX")
        assert res is not None
        assert res["basis"] == schemas.BASIS_MEDICARE_MULTIPLE
        assert res["confidence"] == "low"
        assert res["n_obs"] is None

    def test_unknown_cpt_returns_none(self, store):
        assert store.get_rate("99213", "CA") is None  # non-therapy => not in catalog

    def test_unknown_region_degrades_not_errors(self, store):
        # An unknown 2-letter region must not raise; it degrades to national/fallback.
        res = store.get_rate("96132", "ZZ")
        assert res is not None
        assert res["basis"] in BASES

    def test_query_result_has_full_contract_shape(self, store):
        res = store.get_rate("90791", "US")
        assert set(res.keys()) == {
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
        assert set(res["estimate"].keys()) == {"low", "mid", "high"}
        # Our plain-language label, never an AMA descriptor.
        assert res["service_label"] == schemas.CODE_LABELS["90791"]


# --------------------------------------------------------------------------- #
# Written artifacts exist and are well-formed.
# --------------------------------------------------------------------------- #
class TestWrittenArtifacts:
    def test_three_files_written(self, merged):
        for key in ("by_locality_csv", "national_csv", "json"):
            assert os.path.isfile(merged["paths"][key])

    def test_json_loads_and_has_meta_and_codes(self, merged):
        with open(merged["paths"]["json"], encoding="utf-8") as f:
            doc = json.load(f)
        assert "meta" in doc and "codes" in doc
        assert doc["meta"]["methodology_version"] == schemas.METHODOLOGY_VERSION
        # No non-therapy code leaked into the calculator JSON.
        json_codes = {c["cpt_code"] for c in doc["codes"]}
        assert not (json_codes & NON_THERAPY_CODES)
