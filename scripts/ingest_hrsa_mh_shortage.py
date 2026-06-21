#!/usr/bin/env python3
"""Ingest HRSA mental-health HPSA designations -> per-state shortage bundle.

Downloads HRSA's public mental-health HPSA designation file, dedupes by HPSA ID,
aggregates the metrics that RECONCILE faithfully (count, avg score, geographic
count), and emits the slim JSON the landing tool consumes.

Integrity gate (learned the hard way): the raw file has multiple component-rows
per HPSA, so naive aggregation roughly DOUBLES HRSA's official figures and the
population fields overlap into impossible sums. This script therefore (a) dedupes
by HPSA ID, (b) ships only count/score/geographic per state, and (c) validates the
national deduped count against HRSA's published headline (within tolerance). It
does NOT derive per-state population or providers-needed (cannot reconcile to
HRSA's designation methodology) -- those are cited from HRSA's published report.

Usage: python3 scripts/ingest_hrsa_mh_shortage.py [out.json]
"""
import csv, io, json, sys, urllib.request

SRC = "https://data.hrsa.gov/DataDownload/DD_Files/BCD_HPSA_FCT_DET_MH.csv"
# HRSA published headline (update from the quarterly report when refreshing the bundle).
OFFICIAL = {"designations": 6807, "practitionersNeeded": 6800, "pctNeedMet": 27.29, "asOf": "2025-12-31"}
US = {"AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA",
      "ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR",
      "PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"}


def num(x):
    try: return float((x or "").replace(",", "").strip())
    except Exception: return 0.0


def main(out_path):
    req = urllib.request.Request(SRC, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=120).read().decode("latin-1")
    seen = {}
    for row in csv.DictReader(io.StringIO(raw)):
        if (row.get("HPSA Status") or "").strip() != "Designated":
            continue
        hid = (row.get("HPSA ID") or "").strip()
        if hid and hid not in seen:
            seen[hid] = row

    states = {}
    for r in seen.values():
        st = (r.get("Primary State Abbreviation") or "").strip()
        if st not in US:
            continue
        a = states.setdefault(st, {"count": 0, "score_sum": 0.0, "score_n": 0, "geo": 0})
        a["count"] += 1
        sc = num(r.get("HPSA Score"))
        if sc > 0:
            a["score_sum"] += sc; a["score_n"] += 1
        if "geo" in (r.get("HPSA Type Code", "") + r.get("HPSA Component Type Description", "")).lower():
            a["geo"] += 1

    out_states = {st: {"hpsas": a["count"],
                       "avgScore": round(a["score_sum"] / a["score_n"], 1) if a["score_n"] else None,
                       "geographic": a["geo"]} for st, a in states.items()}

    total = sum(v["hpsas"] for v in out_states.values())
    # Integrity gate: deduped national count must be within 15% of HRSA's headline.
    drift = abs(total - OFFICIAL["designations"]) / OFFICIAL["designations"]
    if drift > 0.15:
        raise SystemExit(f"INTEGRITY GATE FAILED: deduped count {total} vs official "
                         f"{OFFICIAL['designations']} (drift {drift:.0%} > 15%). "
                         "Re-check dedup keys / status filter before shipping.")
    print(f"OK: {len(out_states)} states, {total} deduped HPSAs "
          f"(official {OFFICIAL['designations']}, drift {drift:.0%})")

    bundle = {"meta": {"source": "HRSA Health Professional Shortage Areas - Mental Health designation file",
                       "snapshot": __import__("datetime").date.today().isoformat(),
                       "officialNational": dict(OFFICIAL, note="HRSA official published figures (cited, not derived)."),
                       "caveat": ("Per-state counts are a faithful deduped read of the live HRSA file. HPSA "
                                  "designations reflect overall access/safety-net need, NOT private-pay demand.")},
              "states": out_states}
    json.dump(bundle, open(out_path, "w"))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "mh-shortage.json")
