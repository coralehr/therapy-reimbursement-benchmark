"""oon_bench.query — the read-only point-query layer over the merged v1 dataset.

This is the library's primary backend interface. A :class:`RateStore` loads a
merged v1 dataset ONCE (from the v1 JSON, or from the by-locality CSV) and then
answers :meth:`RateStore.get_rate` in memory with no further I/O — which is also
exactly what the FastAPI layer needs (load at startup, no network at request
time).

QUERY RESULT contract (returned by ``get_rate``)::

    {
      "cpt_code": str,
      "service_label": str,
      "region": str,                 # the region actually resolved to
      "basis": "tic_oon_actual" | "tic_innetwork_proxy" | "medicare_multiple",
      "estimate": {"low": float, "mid": float | None, "high": float},
      "confidence": "high" | "medium" | "low",
      "n_obs": int | None,
      "source": str,
      "snapshot_date": str,
      "disclaimer": str,
    }

Region resolution (per the v1 contract):
    exact state (2-letter)  ->  "US" national  ->  Medicare fallback

Unknown CPT  ->  ``None`` (the HTTP layer turns this into a 404; CLI prints an
error). An *unknown region* is NOT an error: it degrades gracefully to the
national row, and then to the Medicare-band fallback, never raising.

Provenance / confidence mapping (the honesty contract, carried from v0):
    tic_oon_actual,      n_obs large (>= HIGH_CONF_N)  -> "high"
    tic_oon_actual,      n_obs small                   -> "medium"
    tic_innetwork_proxy  (any n)                       -> "medium"
    medicare_multiple    (the fallback band)           -> "low"

The ``basis`` precedence and the "never present a proxy as measured OON" rule
live in the merge stage; the query layer simply reports the basis already
stamped on the merged row and maps it to a confidence the caller can trust.

Stdlib only.
"""

from __future__ import annotations

import csv
import json
import os
import statistics
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Tunables — kept module-level so the CLI/API and tests can reference them.
# ---------------------------------------------------------------------------

#: National pseudo-region key. Two-letter state codes resolve first; this is the
#: documented fallback region when a specific state is unknown or carries no row.
NATIONAL_REGION = "US"

#: A ``tic_oon_actual`` row needs at least this many observations to earn the
#: "high" confidence label. Below it (but still >= MIN_N, which the merge stage
#: enforces) the figure is real OON data but thin, so we report "medium".
HIGH_CONF_N = 30

#: The three bases, in descending strength. Used only for sanity / documentation
#: here; precedence selection happens upstream in the merge stage.
BASIS_TIC_OON = "tic_oon_actual"
BASIS_TIC_PROXY = "tic_innetwork_proxy"
BASIS_MEDICARE = "medicare_multiple"

#: Default served disclaimer. v1 figures are derived from payers' published
#: Transparency-in-Coverage data (or, on fallback, a Medicare multiple); they are
#: never a guarantee of payment.
DEFAULT_DISCLAIMER = (
    "Estimate only, not a guarantee of payment. Out-of-network figures are "
    "derived from payers' published Transparency-in-Coverage data where "
    "available (a quarterly snapshot), and otherwise fall back to a Medicare-"
    "anchored multiplier band. Confidence and observation count indicate the "
    "strength of each figure. Verify benefits with the payer before relying on "
    "any amount."
)


def _to_float(x: Any) -> Optional[float]:
    """Best-effort float; empty string / None / garbage -> None."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(x: Any) -> Optional[int]:
    if x is None:
        return None
    if isinstance(x, bool):  # guard: bool is an int subclass
        return None
    if isinstance(x, int):
        return x
    s = str(x).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


class RateStore:
    """In-memory index over a merged v1 dataset, queried by (cpt, region).

    Build with :meth:`from_file` (auto-detects JSON vs. by-locality CSV) or pass
    a pre-parsed JSON-shaped ``dataset`` dict to the constructor (useful for
    tests). The store flattens the dataset into:

      * ``_codes``      : {cpt_code -> {service_label, medicare_status}}
      * ``_by_region``  : {cpt_code -> {REGION -> region-record}} where REGION is
                          a 2-letter state or ``"US"``. TiC is state-level, so the
                          state record is shared across all of that state's
                          localities; we collapse to one record per state.
    """

    def __init__(self, dataset: dict, *, source_path: Optional[str] = None) -> None:
        self.source_path = source_path
        meta = dataset.get("meta") or {}
        self.meta: dict = meta
        self.snapshot_date: str = str(
            meta.get("snapshot_date") or dataset.get("snapshot_date") or ""
        )
        self.methodology_version: str = str(meta.get("methodology_version") or "")
        # The headline source string we attach to served results. Prefer an
        # explicit meta source list; fall back to the methodology version.
        sources = meta.get("sources")
        if isinstance(sources, list) and sources:
            self._source = "; ".join(str(s) for s in sources)
        elif isinstance(sources, str) and sources.strip():
            self._source = sources.strip()
        else:
            self._source = self.methodology_version or "oon-therapy-benchmark"
        self._disclaimer: str = str(meta.get("disclaimer") or DEFAULT_DISCLAIMER)

        self._codes: dict[str, dict] = {}
        self._by_region: dict[str, dict[str, dict]] = {}
        self._national: dict[str, dict] = {}

        for code_rec in dataset.get("codes", []) or []:
            self._ingest_code(code_rec)

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    @classmethod
    def from_file(cls, path: str) -> "RateStore":
        """Load a merged v1 dataset from a ``.json`` or by-locality ``.csv``.

        Detection is by extension first, then by content sniff so a ``.json``
        file that is actually CSV (or vice versa) still loads.
        """
        ext = os.path.splitext(path)[1].lower()
        if ext == ".json":
            with open(path, encoding="utf-8") as f:
                return cls(json.load(f), source_path=path)
        if ext == ".csv":
            return cls(cls._dataset_from_locality_csv(path), source_path=path)
        # Unknown extension: sniff the first non-space byte.
        with open(path, encoding="utf-8") as f:
            head = f.read(64).lstrip()
        if head.startswith("{"):
            with open(path, encoding="utf-8") as f:
                return cls(json.load(f), source_path=path)
        return cls(cls._dataset_from_locality_csv(path), source_path=path)

    @staticmethod
    def _dataset_from_locality_csv(path: str) -> dict:
        """Reconstruct the JSON-shaped dataset dict from a by-locality CSV.

        Accepts both the v0 column set and the v1 additive columns. v0 rows
        (basis=medicare_multiple, no percentile columns) load fine: the missing
        oon_* fields simply come back as None and get_rate reports them as a
        Medicare-band fallback. Region (state) percentiles are de-duplicated:
        every locality in a state carries the same TiC values, so the first
        non-empty per (cpt, state) wins and a national synthetic region is
        derived where a 'US'/national row is present.
        """
        codes: dict[str, dict] = {}
        snapshot = ""
        methodology = ""

        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cpt = (row.get("cpt_code") or "").strip()
                if not cpt:
                    continue
                snapshot = snapshot or (row.get("snapshot_date") or "").strip()
                methodology = methodology or (row.get("methodology_version") or "").strip()
                code_entry = codes.setdefault(
                    cpt,
                    {
                        "cpt_code": cpt,
                        "service_label": (row.get("service_label") or "").strip(),
                        "medicare_status": (row.get("medicare_status") or "").strip(),
                        "localities": [],
                    },
                )
                # Keep the first non-empty label/status we see.
                if not code_entry["service_label"]:
                    code_entry["service_label"] = (row.get("service_label") or "").strip()
                if not code_entry["medicare_status"]:
                    code_entry["medicare_status"] = (row.get("medicare_status") or "").strip()

                # v1 columns may be absent (v0 CSV) — handle both. The legacy v0
                # columns are oon_estimate_low_usd / oon_estimate_high_usd.
                low = _to_float(
                    row.get("oon_low_usd")
                    if row.get("oon_low_usd") not in (None, "")
                    else row.get("oon_estimate_low_usd")
                )
                high = _to_float(
                    row.get("oon_high_usd")
                    if row.get("oon_high_usd") not in (None, "")
                    else row.get("oon_estimate_high_usd")
                )
                loc = {
                    "state": (row.get("state") or "").strip(),
                    "locality_name": (row.get("locality_name") or "").strip(),
                    "medicare_usd": _to_float(row.get("medicare_nonfacility_usd")),
                    "oon_low_usd": low,
                    "oon_high_usd": high,
                    "oon_mid_usd": _to_float(row.get("oon_mid_usd")),
                    "oon_p90_usd": _to_float(row.get("oon_p90_usd")),
                    "oon_obs_n": _to_int(row.get("oon_obs_n")),
                    "basis": (row.get("basis") or BASIS_MEDICARE).strip() or BASIS_MEDICARE,
                    "payer_scope": (row.get("payer_scope") or "").strip() or None,
                }
                code_entry["localities"].append(loc)

        # Synthesize a national ("US") fallback per code so an uncovered-state query
        # degrades to a Medicare-basis national estimate rather than to some other
        # state's data. The by-locality CSV has no true national (GPCI=1.0) row, so
        # we use the median of the code's locality Medicare amounts as a Medicare
        # multiple band. Honestly labeled basis=medicare_multiple.
        for code_entry in codes.values():
            med_vals = [
                loc["medicare_usd"]
                for loc in code_entry["localities"]
                if loc.get("medicare_usd") is not None
            ]
            if med_vals:
                med = round(statistics.median(med_vals), 2)
                code_entry["national"] = {
                    "state": NATIONAL_REGION,
                    "locality_name": NATIONAL_REGION,
                    "medicare_usd": med,
                    "oon_low_usd": med,
                    "oon_high_usd": round(med * 2.0, 2),
                    "oon_mid_usd": None,
                    "oon_p90_usd": None,
                    "oon_obs_n": None,
                    "basis": BASIS_MEDICARE,
                    "payer_scope": None,
                }

        return {
            "meta": {
                "snapshot_date": snapshot,
                "methodology_version": methodology,
                "disclaimer": DEFAULT_DISCLAIMER,
            },
            "codes": list(codes.values()),
        }

    # ------------------------------------------------------------------ #
    # Ingestion / indexing
    # ------------------------------------------------------------------ #
    def _ingest_code(self, code_rec: dict) -> None:
        cpt = str(code_rec.get("cpt_code") or "").strip()
        if not cpt:
            return
        self._codes[cpt] = {
            "cpt_code": cpt,
            "service_label": str(code_rec.get("service_label") or ""),
            "medicare_status": str(code_rec.get("medicare_status") or ""),
            "by_payer": code_rec.get("by_payer") or {},
        }

        region_map: dict[str, dict] = {}

        # National block: stored under "national" in v0/v1 JSON; we key it as "US".
        national = code_rec.get("national")
        if isinstance(national, dict):
            rec = self._normalize_region_record(national, region=NATIONAL_REGION)
            region_map[NATIONAL_REGION] = rec
            self._national[cpt] = rec

        # Localities collapse to one record per state. TiC is state-level, so all
        # localities in a state share the same percentiles/basis; we therefore
        # pick the strongest (best basis, then most observations) representative
        # per state, which is robust even if rows drift.
        for loc in code_rec.get("localities", []) or []:
            if not isinstance(loc, dict):
                continue
            state = str(loc.get("state") or "").strip().upper()
            if not state:
                continue
            rec = self._normalize_region_record(loc, region=state)
            existing = region_map.get(state)
            if existing is None or self._record_is_stronger(rec, existing):
                region_map[state] = rec

        self._by_region[cpt] = region_map

    @staticmethod
    def _basis_rank(basis: str) -> int:
        return {
            BASIS_TIC_OON: 3,
            BASIS_TIC_PROXY: 2,
            BASIS_MEDICARE: 1,
        }.get(basis, 0)

    @classmethod
    def _record_is_stronger(cls, candidate: dict, incumbent: dict) -> bool:
        """Prefer a higher basis rank, then a higher observation count."""
        cb = cls._basis_rank(candidate.get("basis", BASIS_MEDICARE))
        ib = cls._basis_rank(incumbent.get("basis", BASIS_MEDICARE))
        if cb != ib:
            return cb > ib
        cn = candidate.get("oon_obs_n") or 0
        inn = incumbent.get("oon_obs_n") or 0
        return cn > inn

    @staticmethod
    def _normalize_region_record(node: dict, *, region: str) -> dict:
        """Coerce a v0/v1 locality-or-national node into a uniform region record."""
        basis = str(node.get("basis") or BASIS_MEDICARE).strip() or BASIS_MEDICARE
        return {
            "region": region,
            "basis": basis,
            "medicare_usd": _to_float(
                node.get("medicare_usd")
                if node.get("medicare_usd") is not None
                else node.get("medicare_nonfacility_usd")
            ),
            "oon_low_usd": _to_float(node.get("oon_low_usd")),
            "oon_high_usd": _to_float(node.get("oon_high_usd")),
            "oon_mid_usd": _to_float(node.get("oon_mid_usd")),
            "oon_p90_usd": _to_float(node.get("oon_p90_usd")),
            "oon_obs_n": _to_int(node.get("oon_obs_n")),
            "payer_scope": (node.get("payer_scope") or None),
        }

    # ------------------------------------------------------------------ #
    # Public query surface
    # ------------------------------------------------------------------ #
    def list_codes(self) -> list[dict]:
        """Return the code catalog: [{cpt_code, service_label, medicare_status}].

        Order follows first-seen ingestion order (which mirrors the dataset /
        therapy_codes.py order). The catalog is intentionally lean (no by_payer
        payload — that rides on the per-rate response).
        """
        return [
            {"cpt_code": r["cpt_code"], "service_label": r["service_label"],
             "medicare_status": r["medicare_status"]}
            for r in self._codes.values()
        ]

    def has_code(self, cpt: str) -> bool:
        return str(cpt).strip() in self._codes

    def get_rate(self, cpt: str, region: str = NATIONAL_REGION) -> Optional[dict]:
        """Resolve a single (cpt, region) to the QUERY RESULT dict, or ``None``.

        Returns ``None`` ONLY when the CPT is unknown (caller raises / 404).
        An unknown *region* is not an error: it degrades to the national row,
        and then to whatever basis the data carries (down to medicare_multiple).
        """
        cpt = str(cpt).strip()
        if cpt not in self._codes:
            return None

        requested_region = (region or NATIONAL_REGION).strip()
        norm_region = requested_region.upper() if requested_region else NATIONAL_REGION

        region_map = self._by_region.get(cpt, {})

        # Region resolution: exact state -> US national -> any national record.
        record: Optional[dict] = None
        if norm_region and norm_region != NATIONAL_REGION:
            record = region_map.get(norm_region)
        if record is None:
            record = region_map.get(NATIONAL_REGION) or self._national.get(cpt)
        # NOTE: we deliberately do NOT fall back to an arbitrary other state's
        # record here. Presenting one state's TiC numbers under a different state
        # would be wrong. With a national block always present (JSON has it; the CSV
        # loader synthesizes a Medicare-basis one), this resolves; if it somehow
        # does not, we degrade to the honest Medicare-shell below.

        code_meta = self._codes[cpt]

        if record is None:
            # No usable numbers at all (degenerate dataset). Return a low-
            # confidence, Medicare-basis shell so the contract shape holds.
            return self._build_result(
                code_meta,
                resolved_region=NATIONAL_REGION,
                basis=BASIS_MEDICARE,
                low=None,
                mid=None,
                high=None,
                n_obs=None,
            )

        return self._build_result(
            code_meta,
            resolved_region=record.get("region", norm_region),
            basis=record.get("basis", BASIS_MEDICARE),
            low=record.get("oon_low_usd"),
            mid=record.get("oon_mid_usd"),
            high=record.get("oon_high_usd"),
            n_obs=record.get("oon_obs_n"),
        )

    # ------------------------------------------------------------------ #
    # Result construction
    # ------------------------------------------------------------------ #
    def _confidence_for(self, basis: str, n_obs: Optional[int]) -> str:
        """Map (basis, n_obs) to high | medium | low per the contract."""
        if basis == BASIS_TIC_OON:
            if n_obs is not None and n_obs >= HIGH_CONF_N:
                return "high"
            return "medium"
        if basis == BASIS_TIC_PROXY:
            return "medium"
        return "low"  # medicare_multiple (or anything unexpected)

    def _build_result(
        self,
        code_meta: dict,
        *,
        resolved_region: str,
        basis: str,
        low: Optional[float],
        mid: Optional[float],
        high: Optional[float],
        n_obs: Optional[int],
    ) -> dict:
        return {
            "cpt_code": code_meta["cpt_code"],
            "service_label": code_meta["service_label"],
            "region": resolved_region,
            "basis": basis,
            "estimate": {
                "low": _to_float(low),
                "mid": _to_float(mid),
                "high": _to_float(high),
            },
            "confidence": self._confidence_for(basis, n_obs),
            "n_obs": n_obs,
            "by_payer": code_meta.get("by_payer") or {},
            "source": self._source,
            "snapshot_date": self.snapshot_date,
            "disclaimer": self._disclaimer,
        }


_DEFAULT_STORE: Optional["RateStore"] = None


def _default_dataset_path() -> str:
    """OON_V1_DATA env -> data/v1/...json -> v0 baseline (so it always resolves)."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = os.environ.get("OON_V1_DATA")
    if env:
        return env
    v1 = os.path.join(repo, "data", "v1", "therapy_oon_benchmark_v1.json")
    if os.path.exists(v1):
        return v1
    return os.path.join(repo, "data", "therapy_oon_benchmark_v0.json")


def get_rate(
    cpt: str, region: str = NATIONAL_REGION, *, data: Optional[str] = None
) -> Optional[dict]:
    """Module-level convenience resolver (the function ``oon_bench`` advertises).

    Loads the merged dataset once and caches it (``OON_V1_DATA`` env, else
    ``data/v1/...``, else the v0 baseline). For served/repeated use, build a
    :class:`RateStore` directly. Returns the QUERY RESULT dict, or ``None`` for an
    unknown CPT. Pass ``data=`` to resolve against a specific dataset file.
    """
    global _DEFAULT_STORE
    if data is not None:
        return RateStore.from_file(data).get_rate(cpt, region)
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = RateStore.from_file(_default_dataset_path())
    return _DEFAULT_STORE.get_rate(cpt, region)


__all__ = [
    "RateStore",
    "get_rate",
    "NATIONAL_REGION",
    "HIGH_CONF_N",
    "BASIS_TIC_OON",
    "BASIS_TIC_PROXY",
    "BASIS_MEDICARE",
    "DEFAULT_DISCLAIMER",
]
