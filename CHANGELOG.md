# Changelog

All notable changes to this dataset and its build pipeline are documented here.
This project follows the spirit of [Keep a Changelog](https://keepachangelog.com/)
and uses dataset methodology versions (e.g. `v0-medicare-2026A`) alongside
semantic-ish release tags.

## [v0] — 2026-06-07

Methodology version: `v0-medicare-2026A` · Snapshot: 2026-06-07

First public release. Medicare-anchored, transparent, and deliberately narrow.

### Added

- **Medicare-anchored benchmark** for outpatient therapy CPT codes, computed
  from public CMS Physician Fee Schedule data. Per code:
  `allowed_nonfacility = (workRVU·workGPCI_floored + peRVU_nonfac·peGPCI + mpRVU·mpGPCI) × CF`,
  using the 2026 non-QPP conversion factor `33.4009` read from the CMS file and
  the work-GPCI 1.0-floor column.
- **19 therapy CPT codes** in scope (`therapy_codes.py`): intake (90791/90792),
  individual (90832/90834/90837), family (90846/90847), group (90853), crisis
  (90839/90840), brief assessment (96127), psychological/neuropsychological
  **testing** (96130/96131/96132/96133/96136/96137), psychoanalysis (90845), and
  the interactive-complexity add-on (90785). Service labels are our own original
  plain-language wording — not AMA CPT descriptors.
- **109 CMS payment localities** via GPCI geographic adjustment.
- **Three output files** (the deliverable, committed under `data/`):
  - `therapy_oon_benchmark_v0_national.csv` — one row per code, national.
  - `therapy_oon_benchmark_v0_by_locality.csv` — code × locality (19 × 109 = 2071 rows).
  - `therapy_oon_benchmark_v0.json` — calculator-friendly, nested by code.
- **`medicare_status` on every output** (national CSV, by-locality CSV, and JSON
  code objects), so the restricted status of `90846` is visible to consumers of
  any single file.
- **Placeholder out-of-network band**: `Medicare × [1.0, 2.0]`, explicitly
  documented as an assumption (`basis = medicare_multiple`), not a measurement.
- **Demo calculator** (`examples/calculator/index.html`): self-contained reference
  consumer of the dataset (not the deliverable), reads the JSON, handles
  deductible/coinsurance and no-benefit/no-data states, "show your work" breakdown.
- **Test suite** (`tests/`, 142 tests): golden Medicare values, output integrity,
  AMA-descriptor leak guard, and a fresh-clone skip when raw CMS files are absent.
- **Honesty metadata** stamped on every row: `source`, `snapshot_date`,
  `methodology_version`, `basis`; and a `meta.disclaimer` in the JSON. The JSON
  `meta.conversion_factor` echoes the CF actually parsed from the CMS file.
- **Reproducible build**: `fetch_cms_data.sh` (downloads + extracts the public
  CMS RVU + GPCI files) and `build_baseline.py` (standard-library only).
- **Repository infrastructure**: `LICENSE` (MIT, code), `LICENSE-DATA`
  (CC-BY-4.0 compilation + public-domain CMS / AMA CPT caveats),
  `CONTRIBUTING.md`, a CI workflow (`.github/workflows/ci.yml`, lint + tests on
  push/PR), and a quarterly rebuild workflow that opens a PR with refreshed data
  (`.github/workflows/rebuild.yml`).

### Known limitations

- The OON band is a **placeholder**, not real payer data. v1 replaces it with
  Transparency-in-Coverage-derived percentiles per payer/region.
- `90846` carries CMS status `R` (restricted/not separately payable under the
  PFS); included with its RVUs and flagged in `METHODOLOGY.md`.
- `96127` is a brief-assessment add-on with a small (~$5) Medicare amount by
  design.
- Localities are CMS payment localities, not ZIP codes. ZIP→locality mapping is
  a v1 nicety.

### Notes

- CPT(R) is a registered trademark of the American Medical Association. This
  release ships code numbers and CMS RVU facts plus our own labels only; it does
  not redistribute AMA CPT descriptor text. See `LICENSE-DATA`.

## [v1-backend] — unreleased

Methodology version: `v1-tic-2026A`

The v1 Transparency-in-Coverage backend, built and tested end to end on synthetic
MRF fixtures. The real-payer ingest (producing a committed `data/v1/`) is the
remaining operational step.

### Added

- **`oon_bench` Python package** — the open-source backend (the product; the
  calculator UI moved to `examples/`):
  - `schemas` — shared data contracts + the percentile math (single source of truth).
  - `aggregate` — rate rows → percentiles per code × region × payer, MIN_N ≥ 10 gate,
    dedupe, professional-only, outlier clip.
  - `merge` — TiC percentiles over the v0 Medicare baseline, basis precedence
    `tic_oon_actual > tic_innetwork_proxy > medicare_multiple`.
  - `query` — `RateStore` + module-level `get_rate(cpt, region)`; region resolution
    state → US national → Medicare fallback; confidence mapping.
  - `api` — FastAPI service: `GET /health`, `/v1/codes`, `/v1/rates/{cpt}?region=`.
  - `cli` / `__main__` — `python -m oon_bench {aggregate,merge,query}`.
  - `ingest` — real-payer TiC runner (checkpoint, gzip, dry-run); `run_local` +
    synthetic fixtures exercise the full pipeline without downloading payer data.
- **`pyproject.toml`** — pip-installable (`pip install -e ".[api]"`), `oon-bench`
  console script, pytest + ruff config.
- **~145 v1 tests** (schemas/aggregate/merge/query/api/end-to-end), bringing the repo
  suite to ~287 passing.

### Real data landed (national sample)

- **`data/v1/`** now contains a REAL `tic_innetwork_proxy` dataset: 40 real UHC
  in-network plan files (2026-06-01 snapshot) pooled and merged over the v0 Medicare
  baseline. All 19 codes cleared MIN_N (n=148-180). Example: 90837 (60-min therapy)
  proxy median $137 vs Medicare $167. The FastAPI service serves these by default.
  See `data/v1/PROVENANCE.md`.
- Confirmed the filter handles the real UHC in-network schema; confirmed payer
  out-of-network *allowed-amount* files are effectively empty (UHC largest 17 KB), so
  in-network negotiated rates are the proxy.

### Per-locality estimates (geo-blend)

- **`oon_bench/blend.py`**: every CMS locality now carries a real-data-informed
  estimate via `geo_method=medicare_gpci_blend` — the measured national
  in-network/Medicare ratio per code, scaled by each locality's Medicare GPCI.
  90837 median now runs AL $133 / CA $142 / NY $150 / US $137 instead of a flat
  national number. Chosen over provider-reference->NPI->state (NPPES) resolution
  because in-network rates are negotiated at the multi-state provider-group/TIN level,
  so per-state attribution from the MRF is inherently fuzzy and would need tens of
  thousands of NPI lookups. The rate signal is real; the geography is Medicare's.

### Multi-payer (data/v1/ rebuilt across 3 payers)

- **`build_real.py` is now a multi-payer registry** (UHC + Centene + Cigna), with
  per-payer discovery and per-payer error isolation. `data/v1/` is rebuilt from 166
  real plan files (UHC 150, Centene 13, Cigna 3); every code is now
  `payer_scope=multi` — the merge blends per-payer ratios by n-weighted mean, so the
  numbers are no longer UHC-only. 90837 median moved to $131 (was $130 UHC-only) with
  Cigna (rich national files, behavioral health folded in) and Centene (near-Medicare
  ACA rates) mixed in. n in the tens of thousands per common code.
- Payer access: Cigna via `cigna.com/static/mrf/latest.json` -> signed CloudFront TOC
  -> signed file URLs; Centene via constructed brand index URLs. Aetna (SPA), Anthem
  (~10GB index), Humana (synthetic data), Kaiser (integrated HMO) deferred with reasons.
- **`ijson`** is now an `[ingest]` extra (`pip install -e ".[ingest]"`) — required to
  stream the large Cigna files.

### Planned

- Broaden beyond a 40-plan UHC sample (more plans, more payers) to tighten the
  national ratio the localities inherit; Aetna index is SPA-gated (needs browser
  discovery). A full-scale run is where a fire-and-forget batch box earns its keep.
- True per-state measured rates (NPPES) only if the geo-blend proves insufficient.
- ZIP→locality mapping via CMS `26LOCCO`.
