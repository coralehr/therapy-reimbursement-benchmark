# v1 dataset provenance

- **Snapshot:** UHC Transparency-in-Coverage files dated 2026-06-01.
- **Method:** 230 real UHC in-network-rates plan files (1-12 MB band, strided across
  employers for geographic/plan spread) streamed through `v1_tic/filter_mrf.py`,
  pooled, aggregated (MIN_N>=10), merged over the v0 Medicare baseline. Reproduce with
  `python3 -m oon_bench.build_real`. Pooled professional negotiated rates per code now
  number in the tens of thousands (n ~100,000 for the common codes; ~2,300-3,800 for
  the rarer 96127 / 90845), so the national ratios are well-supported.
- **basis = `tic_innetwork_proxy`**. In-network negotiated rates are used as the
  out-of-network proxy because payers' actual OON allowed-amount files are effectively
  empty (UHC's largest is 17 KB). See README / METHODOLOGY.
- **Per-locality via geo-blend** (`geo_method = medicare_gpci_blend`): the measured
  signal is the NATIONAL in-network/Medicare ratio per code (e.g. 90837 ~0.82). Each
  CMS locality's number is that real ratio scaled by the locality's Medicare amount
  (which already carries GPCI). So the rate signal is real data; the geographic
  variation is Medicare's GPCI. National rows are `geo_method = measured`.
  Example: 90837 median runs AL $126 / CA $135 / NY $142 / US $130 (n=101,401).
- **Why not per-state from the MRF directly:** in-network rates are negotiated at the
  provider-group / TIN level, and groups are frequently multi-state, so attributing a
  single rate to one state is inherently fuzzy and would require resolving tens of
  thousands of NPIs through NPPES. The geo-blend is the honest, cheaper alternative.
- **Not comprehensive:** a 230-plan sample of one payer (UHC), not an all-payer build.
  UHC only publishes ~231 in-network files in the 1-12 MB band; going further means
  the multi-GB files or adding payers (Cigna is fetchable; Aetna is SPA-gated). Adding
  payers is the next lever for a true multi-payer ratio.
