# v1 dataset provenance

- **Snapshot:** payer Transparency-in-Coverage files dated 2026-06-01.
- **Multi-payer:** pooled across **3 payers** (`payer_scope = multi`): UHC (200 plan
  files), Centene (19 Ambetter/HealthNet/etc. files), Cigna (3 large national files) —
  222 files total. Each streamed through `v1_tic/filter_mrf.py` (ijson), pooled,
  aggregated per (code, payer) with MIN_N>=10, then the merge blends payers by an
  n-weighted mean into one national ratio per code. Reproduce with
  `python3 -m oon_bench.build_real uhc=200 centene=40 cigna=15`. (Cigna caps at ~3
  files because its non-national plans are still >150MB; UHC dominates by file volume.)
- **Observation counts:** n ~90,000 for the common individual/family/group codes;
  ~38,000-46,000 for the testing block; ~4,000 for the rarer 96127 / 90845.
- **Converged:** the medians are stable across builds — 40 UHC plans, 230 UHC plans,
  and 222 multi-payer plans all put 90837 within ~$130-137 and 90791 within ~$145-151.
  Adding files/payers no longer moves the estimate, which is the signal that this slice
  is well-supported (the lake is boiled). Cigna (rich national files, behavioral health
  folded in) and Centene (near-Medicare ACA rates) shift the blend only slightly.
- **basis = `tic_innetwork_proxy`**. In-network negotiated rates are used as the
  out-of-network proxy because payers' actual OON allowed-amount files are effectively
  empty (UHC's largest is 17 KB). See README / METHODOLOGY.
- **Per-locality via geo-blend** (`geo_method = medicare_gpci_blend`): the measured
  signal is the NATIONAL in-network/Medicare ratio per code (e.g. 90837 ~0.82). Each
  CMS locality's number is that real ratio scaled by the locality's Medicare amount
  (which already carries GPCI). So the rate signal is real data; the geographic
  variation is Medicare's GPCI. National rows are `geo_method = measured`.
  Example: 90837 median runs AL $126 / CA $135 / NY $142 / US $131 (multi-payer, n=66,092).
- **Why not per-state from the MRF directly:** in-network rates are negotiated at the
  provider-group / TIN level, and groups are frequently multi-state, so attributing a
  single rate to one state is inherently fuzzy and would require resolving tens of
  thousands of NPIs through NPPES. The geo-blend is the honest, cheaper alternative.
- **Not comprehensive:** 3 payers, size-banded samples (we skip the multi-GB national
  files and cap Centene/Cigna by size). Still missing Aetna (SPA-gated, needs browser
  discovery), Anthem/Elevance (~10 GB index), and the BCBS plans. UHC remains the
  largest contributor by volume. Broadening payer mix + raising per-payer caps is the
  next lever; the build is a local background job (`oon_bench/build_real.py`).
