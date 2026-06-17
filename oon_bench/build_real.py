#!/usr/bin/env python3
"""Boil-the-lake MULTI-PAYER builder: pool real payer plan files into data/v1/.

Discovers and streams a diverse sample of real in-network MRF files from several
payers (UHC, Centene, Cigna), filters to therapy CPTs, pools, aggregates to
national in-network/Medicare ratios per code (the merge stage pools across payers
-> payer_scope=multi), merges over the v0 Medicare baseline, and geo-blends to
every CMS locality.

Runs locally (no cloud box); long, so run in the background. Robust: an individual
file OR an entire payer can fail and the build still completes with the rest. Needs
ijson for large files (pip install ijson).

Usage:
    python3 -m oon_bench.build_real                 # defaults: uhc=150 centene=40 cigna=8
    python3 -m oon_bench.build_real uhc=200 centene=60 cigna=12
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "v1_tic"))

from oon_bench import aggregate, blend, merge  # noqa: E402

BASELINE = os.path.join(HERE, "data", "therapy_oon_benchmark_v0_by_locality.csv")
OUT_DIR = os.path.join(HERE, "data", "v1")
POOL_DIR = "/tmp/br_pool"
LOG = os.path.join(OUT_DIR, "BUILD_LOG.txt")
FILTER = os.path.join(HERE, "v1_tic", "filter_mrf.py")

DEFAULT_COUNTS = {"uhc": 150, "centene": 40, "cigna": 8}


def log(msg: str) -> None:
    line = msg.rstrip()
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def _get(url: str, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "oon-bench/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _head_size(url: str, timeout: int = 20) -> int:
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "oon-bench/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.headers.get("Content-Length", "0"))
    except Exception:
        return -1


def _pick_smallest(urls: list, n: int, cap: int, head_budget: int = 140) -> list:
    """HEAD up to head_budget candidates, keep those <= cap bytes, return n smallest."""
    sized = []
    for u in urls[:head_budget]:
        s = _head_size(u)
        if 0 < s <= cap:
            sized.append((s, u))
    sized.sort()
    return [u for _, u in sized[:n]]


# --------------------------------------------------------------------------- #
# UHC
# --------------------------------------------------------------------------- #
UHC_INDEX_URL = "https://transparency-in-coverage.uhc.com/api/v1/uhc/blobs/"
UHC_CACHE = "/tmp/uhc-blobs.json"


def discover_uhc(n: int) -> list:
    if not os.path.exists(UHC_CACHE):
        log("  uhc: fetching index")
        urllib.request.urlretrieve(UHC_INDEX_URL, UHC_CACHE)
    d = json.load(open(UHC_CACHE))
    items = d if isinstance(d, list) else (d.get("blobs") or d.get("value") or d.get("data") or [])

    def sz(x):
        try:
            return int(x.get("size"))
        except (TypeError, ValueError):
            return -1

    inn = [x for x in items if isinstance(x, dict)
           and "in-network" in x.get("name", "").lower() and 1_000_000 < sz(x) < 12_000_000]
    inn.sort(key=lambda x: x.get("name", ""))
    if len(inn) > n:
        stride = len(inn) / n
        inn = [inn[int(i * stride)] for i in range(n)]
    return [x["downloadUrl"] for x in inn]


# --------------------------------------------------------------------------- #
# Centene (UHC-like: static HTML landing -> per-brand index JSON -> file locations)
# --------------------------------------------------------------------------- #
CENTENE_LANDING = "https://www.centene.com/price-transparency-files.html"
CENTENE_BASE = "https://www.centene.com/content/dam/centene/Centene%20Corporate/json/DOCUMENT"
CENTENE_BRANDS = ["ambetter", "healthnet", "fidelis", "qualchoice", "wellcarenc"]


def _centene_index_urls() -> list:
    """Prefer scraping the landing HTML; fall back to constructing known brand URLs
    for the current/previous reporting month (the landing page doesn't always expose
    the links in raw HTML)."""
    try:
        html = _get(CENTENE_LANDING, timeout=60).decode("utf-8", "replace")
        scraped = sorted(set(re.findall(
            r"https://www\.centene\.com/content/dam/[^\"'\s)]*_index\.json", html)))
        if scraped:
            return scraped
    except Exception:
        pass
    from datetime import date, timedelta
    today = date.today()
    months = [today.replace(day=1), (today.replace(day=1) - timedelta(days=1)).replace(day=1)]
    urls = []
    for m in months:
        stamp = m.strftime("%Y-%m-01")
        cand = [f"{CENTENE_BASE}/{stamp}_{b}_index.json" for b in CENTENE_BRANDS]
        urls = [u for u in cand if _head_size(u) > 0]
        if urls:
            break
    return urls


def discover_centene(n: int) -> list:
    idx_urls = _centene_index_urls()
    log(f"  centene: {len(idx_urls)} brand indexes")
    locs: list = []
    for iu in idx_urls:
        try:
            d = json.loads(_get(iu, timeout=60))
        except Exception:
            continue
        for s in d.get("reporting_structure", []) or []:
            for f in s.get("in_network_files", []) or []:
                loc = f.get("location")
                if loc and "in-network" in loc.lower():
                    locs.append(loc)
    locs = sorted(set(locs))
    log(f"  centene: {len(locs)} in-network locations; sizing")
    return _pick_smallest(locs, n, cap=60_000_000, head_budget=200)


# --------------------------------------------------------------------------- #
# Cigna (latest.json -> signed 72MB TOC -> cigna-native in-network-rates locations)
# --------------------------------------------------------------------------- #
CIGNA_LATEST = "https://www.cigna.com/static/mrf/latest.json"
CIGNA_HOST = "d25kgz5rikkq4n.cloudfront.net"


def discover_cigna(n: int) -> list:
    latest = json.loads(_get(CIGNA_LATEST, timeout=60))
    toc_url = None
    for m in latest.get("mrfs", []):
        for f in m.get("files", []):
            if f.get("url"):
                toc_url = f["url"]
                break
        if toc_url:
            break
    if not toc_url:
        raise RuntimeError("cigna: no TOC url in latest.json")
    log("  cigna: streaming 72MB TOC")
    import ijson
    locs: list = []
    req = urllib.request.Request(toc_url, headers={"User-Agent": "oon-bench/0.1"})
    with urllib.request.urlopen(req, timeout=300) as r:
        for f in ijson.items(r, "reporting_structure.item.in_network_files.item"):
            loc = f.get("location") if isinstance(f, dict) else None
            if (loc and CIGNA_HOST in loc and "in-network-rates" in loc
                    and "cigna-health-life-insurance-company" in loc):
                locs.append(loc)
                if len(locs) >= 120:
                    break
    log(f"  cigna: {len(locs)} cigna-native in-network locations; sizing")
    return _pick_smallest(locs, n, cap=150_000_000, head_budget=120)


PAYERS = {"uhc": discover_uhc, "centene": discover_centene, "cigna": discover_cigna}


def download_and_filter(url: str, payer: str, idx: int) -> str | None:
    raw = os.path.join(POOL_DIR, f"{payer}_{idx}.dat")
    out = os.path.join(POOL_DIR, f"{payer}_{idx}.jsonl")
    try:
        urllib.request.urlretrieve(url, raw)
        # name by magic bytes so the filter opens gz vs plain correctly
        with open(raw, "rb") as fh:
            magic = fh.read(2)
        path = raw + (".json.gz" if magic == b"\x1f\x8b" else ".json")
        os.rename(raw, path)
        subprocess.run([sys.executable, FILTER, "--payer", payer, "--kind", "in-network",
                        path, "-o", out], check=True, capture_output=True, timeout=600)
        os.remove(path)
        return out
    except Exception as e:  # noqa: BLE001 - skip a bad file, keep going
        log(f"  skip {payer}_{idx}: {type(e).__name__}")
        for p in (raw, raw + ".json", raw + ".json.gz"):
            if os.path.exists(p):
                os.remove(p)
        return None


def main() -> int:
    counts = dict(DEFAULT_COUNTS)
    for a in sys.argv[1:]:
        if "=" in a:
            k, v = a.split("=", 1)
            counts[k.strip()] = int(v)
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(POOL_DIR, exist_ok=True)
    open(LOG, "w").close()
    log(f"MULTI-PAYER BUILD START: {counts}")

    per_payer_ok = {}
    for payer, want in counts.items():
        if want <= 0 or payer not in PAYERS:
            continue
        try:
            urls = PAYERS[payer](want)
        except Exception as e:  # noqa: BLE001 - a whole payer can fail; continue with the rest
            log(f"  PAYER {payer} discovery failed: {type(e).__name__}: {e}")
            continue
        log(f"  {payer}: selected {len(urls)} files")
        ok = 0
        for i, u in enumerate(urls):
            if download_and_filter(u, payer, i):
                ok += 1
            if (i + 1) % 25 == 0:
                log(f"    {payer} {i + 1}/{len(urls)} ({ok} ok)")
        per_payer_ok[payer] = ok
        log(f"  {payer}: filtered {ok}/{len(urls)}")
    log(f"per-payer files pooled: {per_payer_ok}")

    pool = sorted(glob.glob(os.path.join(POOL_DIR, "*.jsonl")))
    records = aggregate.aggregate_files(pool)
    log(f"aggregate records (cleared MIN_N>=10): {len(records)}")

    result = merge.merge(records, baseline_csv=BASELINE, out_dir=OUT_DIR)
    log(f"merge basis counts (national): {result.get('meta', {}).get('basis_counts_national')}")

    jp = os.path.join(OUT_DIR, "therapy_oon_benchmark_v1.json")
    lp = os.path.join(OUT_DIR, "therapy_oon_benchmark_v1_by_locality.csv")
    ds = json.load(open(jp))
    ds["meta"]["plan_sample"] = per_payer_ok
    ds["meta"]["payers"] = sorted(per_payer_ok)
    blend.geo_adjust_dataset(ds)
    json.dump(ds, open(jp, "w"), indent=2)
    _patch_locality_csv(lp, ds)

    log("\nNATIONAL multi-payer in-network proxy:")
    for c in sorted(ds["codes"], key=lambda c: c["cpt_code"]):
        nat = c["national"]
        if nat.get("oon_obs_n"):
            log(f"  {c['cpt_code']}  n={nat['oon_obs_n']:>6}  scope={nat.get('payer_scope')}  "
                f"p25={nat['oon_low_usd']}  med={nat['oon_mid_usd']}  p75={nat['oon_high_usd']}")
    log(f"\nBUILD DONE: payers={sorted(per_payer_ok)} files={sum(per_payer_ok.values())}")
    return 0


def _patch_locality_csv(lp: str, ds: dict) -> None:
    import csv
    idx = {(c["cpt_code"], loc["state"], loc["locality_name"]): loc
           for c in ds["codes"] for loc in c["localities"]}
    rows = list(csv.DictReader(open(lp)))
    if not rows:
        return
    cols = list(rows[0].keys())
    for r in rows:
        loc = idx.get((r["cpt_code"], r["state"], r["locality_name"]))
        if not loc:
            continue
        for col in ("basis", "oon_low_usd", "oon_high_usd", "oon_mid_usd",
                    "oon_p90_usd", "oon_obs_n", "payer_scope"):
            if col in r and col in loc:
                r[col] = "" if loc[col] is None else loc[col]
    with open(lp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
