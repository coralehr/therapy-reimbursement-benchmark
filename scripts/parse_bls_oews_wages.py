#!/usr/bin/env python3
"""Parse BLS OEWS state wage file -> per-state therapist wage bundle.

BLS blocks automated fetch AND serves no OEWS through its public API (even with a
key, the documented series return empty). So the OEWS state file must be downloaded
MANUALLY from https://www.bls.gov/oes/tables.htm (the "State" file, oesm{YY}st.zip).
This script parses the unzipped state_M20YY_dl.xlsx into the slim bundle.

CRITICAL: use SOC 21-1018, NOT 21-1014. The 2018 SOC merged 21-1014 (Mental Health
Counselors) into 21-1018 (Substance Abuse, Behavioral Disorder & Mental Health
Counselors). The old code returns ZERO rows in current OEWS data.

Usage: python3 scripts/parse_bls_oews_wages.py /path/to/state_M2025_dl.xlsx out.json
"""
import sys, json, openpyxl

OCC = {"21-1018": "Mental Health & Substance Abuse Counselors",
       "19-3033": "Clinical & Counseling Psychologists",
       "21-1013": "Marriage & Family Therapists",
       "21-1023": "Mental Health & Substance Abuse Social Workers",
       "21-1015": "Rehabilitation Counselors"}
ABBR = {"Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA","Colorado":"CO",
        "Connecticut":"CT","Delaware":"DE","District of Columbia":"DC","Florida":"FL","Georgia":"GA",
        "Hawaii":"HI","Idaho":"ID","Illinois":"IL","Indiana":"IN","Iowa":"IA","Kansas":"KS","Kentucky":"KY",
        "Louisiana":"LA","Maine":"ME","Maryland":"MD","Massachusetts":"MA","Michigan":"MI","Minnesota":"MN",
        "Mississippi":"MS","Missouri":"MO","Montana":"MT","Nebraska":"NE","Nevada":"NV","New Hampshire":"NH",
        "New Jersey":"NJ","New Mexico":"NM","New York":"NY","North Carolina":"NC","North Dakota":"ND",
        "Ohio":"OH","Oklahoma":"OK","Oregon":"OR","Pennsylvania":"PA","Rhode Island":"RI","South Carolina":"SC",
        "South Dakota":"SD","Tennessee":"TN","Texas":"TX","Utah":"UT","Vermont":"VT","Virginia":"VA",
        "Washington":"WA","West Virginia":"WV","Wisconsin":"WI","Wyoming":"WY"}


def num(v):
    if v in (None, "*", "**", "#", "~"): return None
    try: return int(round(float(str(v).replace(",", ""))))
    except Exception: return None


def main(xlsx, out, release="May 2025"):
    ws = openpyxl.load_workbook(xlsx, read_only=True).active
    it = ws.iter_rows(values_only=True)
    h = {str(c).strip().upper(): i for i, c in enumerate(next(it))}
    states = {}
    for r in it:
        occ, area = r[h["OCC_CODE"]], r[h["AREA_TITLE"]]
        if occ not in OCC or area not in ABBR:
            continue
        states.setdefault(ABBR[area], {})[occ] = {
            "emp": num(r[h["TOT_EMP"]]), "mean": num(r[h["A_MEAN"]]),
            "p10": num(r[h["A_PCT10"]]), "median": num(r[h["A_MEDIAN"]]), "p90": num(r[h["A_PCT90"]]),
        }
    if len(states) < 50:
        raise SystemExit(f"INTEGRITY: only {len(states)} states parsed (expected 51). Check the file.")
    bundle = {"meta": {"source": f"BLS Occupational Employment & Wage Statistics (OEWS), {release}",
                       "release": release,
                       "caveat": ("Annual wages for EMPLOYED therapists (W-2) by state — a benchmark/floor, "
                                  "NOT solo private-practice take-home or per-session cash rates.")},
              "occupations": OCC, "states": states}
    json.dump(bundle, open(out, "w"))
    print(f"OK: {len(states)} states, {sum(len(v) for v in states.values())} cells -> {out}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: parse_bls_oews_wages.py <state_M20YY_dl.xlsx> <out.json>")
    main(sys.argv[1], sys.argv[2])
