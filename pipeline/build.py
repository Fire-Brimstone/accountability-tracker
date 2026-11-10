#!/usr/bin/env python3
"""Build the accountability-monitor dataset from public sources.

Standard library only. Output goes to site/data/ and is read by the static site.

Sources
  - unitedstates/congress-legislators  (current members, IDs)       no key
  - Voteview (UCLA)                    (every roll-call vote)        no key
  - GovInfo BILLSTATUS bulk XML        (sponsored / cosponsored)     no key
  - FEC bulk files                     (campaign money)              no key
  - Data Commons ClaimReview feed      (fact-checks)                 no key
  - curated/                           (statements, bill effects, manual fact-checks)

No API keys or sign-ups are needed.

Usage
  python pipeline/build.py                     # everything (first run downloads ~300 MB of bulk files)
  python pipeline/build.py --fec-itemized      # also donor states/employers (streams a ~2.5 GB FEC file)
  python pipeline/build.py --skip-bills --skip-fec --skip-factcheck   # votes only
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import bulk

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "data"
CACHE = ROOT / ".cache" / "http"
CURATED = ROOT / "curated"


LEGISLATORS_URL = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
VOTEVIEW = "https://voteview.com/static/data/out"
PHOTO_URL = "https://unitedstates.github.io/images/congress/225x275/{}.jpg"

PARTY_CODES = {100: "D", 200: "R", 328: "I"}
CAUCUS_TO_CODE = {"Democrat": 100, "Republican": 200}

BILL_TYPES = {  # voteview prefix -> (congress.gov api type, congress.gov url slug)
    "HCONRES": ("hconres", "house-concurrent-resolution"),
    "SCONRES": ("sconres", "senate-concurrent-resolution"),
    "HJRES": ("hjres", "house-joint-resolution"),
    "SJRES": ("sjres", "senate-joint-resolution"),
    "HRES": ("hres", "house-resolution"),
    "SRES": ("sres", "senate-resolution"),
    "HR": ("hr", "house-bill"),
    "S": ("s", "senate-bill"),
}
BILL_RE = re.compile(r"^(HCONRES|SCONRES|HJRES|SJRES|HRES|SRES|HR|S)(\d+)$")

# Junk values donors type into the FEC "employer" box.
NON_EMPLOYERS = {
    "N/A", "NA", "NONE", "NULL", "RETIRED", "NOT EMPLOYED", "SELF-EMPLOYED", "SELF EMPLOYED",
    "SELF", "UNEMPLOYED", "HOMEMAKER", "INFORMATION REQUESTED", "INFORMATION REQUESTED PER BEST EFFORTS",
    "REQUESTED", "STUDENT", "", "NOT-EMPLOYED", "DISABLED",
}

# ---------------------------------------------------------------- fact-check rating buckets
FALSE_RATINGS = [
    "pants on fire", "false", "four pinocchios", "4 pinocchios", "three pinocchios",
    "3 pinocchios", "incorrect", "fabricated", "fake", "wrong", "baseless", "no evidence",
]
MISLEADING_RATINGS = [
    "mostly false", "half true", "half-true", "misleading", "two pinocchios", "2 pinocchios",
    "missing context", "lacks context", "needs context", "distort", "exaggerat", "spin",
    "cherry", "partly false", "partly true", "mixed", "unsupported", "one pinocchio", "1 pinocchio",
]
TRUE_RATINGS = ["mostly true", "true", "correct", "accurate", "geppetto"]


def rating_bucket(text: str) -> str:
    t = (text or "").lower().strip()
    # check more specific phrases first ("mostly false" before "false", "half true" before "true")
    for phrase in MISLEADING_RATINGS:
        if phrase in t:
            return "misleading"
    for phrase in FALSE_RATINGS:
        if phrase in t:
            return "false"
    for phrase in TRUE_RATINGS:
        if phrase in t:
            return "true"
    return "other"


# ---------------------------------------------------------------- HTTP with on-disk cache
class RateLimited(Exception):
    pass


class Fetcher:
    def __init__(self, max_age_hours: float):
        self.max_age = max_age_hours * 3600
        self.network_calls = 0
        self._last_call: dict[str, float] = {}
        CACHE.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _cache_key(url: str) -> str:
        # strip API keys so the cache is shareable and keys never touch disk
        clean = re.sub(r"(api_key|key)=[^&]+", r"\1=X", url)
        return hashlib.sha1(clean.encode()).hexdigest()

    def get(self, url: str, *, as_json: bool = True, timeout: int = 60, min_interval: float = 0):
        """min_interval: minimum seconds between *network* calls to this host (cache hits are free)."""
        path = CACHE / self._cache_key(url)
        if path.exists() and time.time() - path.stat().st_mtime < self.max_age:
            data = path.read_bytes()
        else:
            host = urllib.parse.urlparse(url).netloc
            wait = self._last_call.get(host, 0) + min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_call[host] = time.time()
            data = self._download(url, timeout)
            path.write_bytes(data)
        return json.loads(data) if as_json else data.decode("utf-8")

    def _download(self, url: str, timeout: int) -> bytes:
        last_err = None
        for attempt in range(3):
            try:
                headers = {"User-Agent": "accountability-monitor/0.1"}
                # Inside GitHub Actions, use the runner's built-in token so shared runner IPs
                # don't hit GitHub's anonymous rate limit. Not needed (or set) when run locally.
                if "api.github.com" in url and os.environ.get("GITHUB_TOKEN"):
                    headers["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
                req = urllib.request.Request(url, headers=headers)
                self.network_calls += 1
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    raise RateLimited(url.split("?")[0])
                if e.code in (400, 403, 404):
                    raise
                last_err = e
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = e
            time.sleep(2 * (attempt + 1))
        raise last_err  # type: ignore[misc]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 1. members
def load_members(f: Fetcher) -> list[dict]:
    members = []
    for p in f.get(LEGISLATORS_URL):
        term = p["terms"][-1]
        name = p["name"].get("official_full") or f'{p["name"]["first"]} {p["name"]["last"]}'
        members.append({
            "id": p["id"]["bioguide"],
            "name": name,
            "first": p["name"].get("nickname") or p["name"]["first"],
            "last": p["name"]["last"],
            "party": term.get("party", ""),
            "caucus": term.get("caucus"),
            "state": term["state"],
            "district": term.get("district"),
            "chamber": "house" if term["type"] == "rep" else "senate",
            "since": p["terms"][0]["start"],
            "term_end": term["end"],
            "website": term.get("url"),
            "phone": term.get("phone"),
            "photo": PHOTO_URL.format(p["id"]["bioguide"]),
            "ids": {
                "bioguide": p["id"]["bioguide"],
                "icpsr": p["id"].get("icpsr"),
                "fec": p["id"].get("fec", []),
                "govtrack": p["id"].get("govtrack"),
                "opensecrets": p["id"].get("opensecrets"),
                "wikipedia": p["id"].get("wikipedia"),
            },
            "terms": [{"type": t["type"], "start": t["start"], "end": t["end"],
                       "state": t["state"], "district": t.get("district"), "party": t.get("party")}
                      for t in p["terms"]],
        })
    return members


# ---------------------------------------------------------------- 2. votes (Voteview)
def cast_to_pos(code: int) -> str:
    if 1 <= code <= 3:
        return "Y"
    if 4 <= code <= 6:
        return "N"
    if code == 7:
        return "P"
    if code in (8, 9):
        return "X"  # not voting
    return "-"      # not a member at the time


def parse_bill(raw: str, congress: int) -> dict | None:
    m = BILL_RE.match((raw or "").strip().upper())
    if not m:
        return None
    prefix, num = m.groups()
    api_type, slug = BILL_TYPES[prefix]
    return {
        "id": f"{api_type}{num}-{congress}",
        "label": bulk.bill_label(prefix, num),
        "url": f"https://www.congress.gov/bill/{congress}th-congress/{slug}/{num}",
    }


def build_votes(f: Fetcher, members: list[dict], congresses: list[int]) -> dict:
    """Returns {bioguide: {"votes": {...}, "stats": {...}}} and writes rollcall files."""
    by_bioguide: dict[str, dict] = defaultdict(lambda: {"votes": {}, "tally": Counter(), "party_agree": [0, 0]})
    caucus_override = {m["id"]: CAUCUS_TO_CODE.get(m["caucus"]) for m in members if m.get("caucus")}
    current_ids = {m["id"] for m in members}

    for congress in congresses:
        for ch, chamber in (("H", "House"), ("S", "Senate")):
            key = f"{ch}{congress}"
            log(f"  voteview {key}")
            try:
                rc_csv = f.get(f"{VOTEVIEW}/rollcalls/{key}_rollcalls.csv", as_json=False, timeout=180)
                vt_csv = f.get(f"{VOTEVIEW}/votes/{key}_votes.csv", as_json=False, timeout=300)
                mb_csv = f.get(f"{VOTEVIEW}/members/{key}_members.csv", as_json=False, timeout=180)
            except Exception as e:  # noqa: BLE001
                log(f"    ! voteview {key} unavailable: {e}")
                continue

            icpsr_info = {}
            for r in csv.DictReader(io.StringIO(mb_csv)):
                if r["chamber"] != chamber or not r["bioguide_id"]:
                    continue
                code = int(float(r["party_code"]))
                code = caucus_override.get(r["bioguide_id"]) or code
                icpsr_info[r["icpsr"]] = (r["bioguide_id"], code)

            rollcalls = {}
            for r in csv.DictReader(io.StringIO(rc_csv)):
                rollcalls[int(r["rollnumber"])] = r
            if not rollcalls:
                continue
            max_roll = max(rollcalls)

            # positions[bioguide] = list of chars indexed by roll-1
            positions: dict[str, list[str]] = {}
            party_counts: dict[int, dict[int, Counter]] = defaultdict(lambda: defaultdict(Counter))
            for r in csv.DictReader(io.StringIO(vt_csv)):
                info = icpsr_info.get(r["icpsr"])
                if not info:
                    continue
                bg, pcode = info
                roll = int(r["rollnumber"])
                pos = cast_to_pos(int(float(r["cast_code"])))
                arr = positions.setdefault(bg, ["-"] * max_roll)
                if arr[roll - 1] == "-" or pos != "-":
                    arr[roll - 1] = pos
                if pos in "YN":
                    party_counts[roll][pcode][pos] += 1

            party_majority = {}
            for roll, parties in party_counts.items():
                pm = {}
                for pcode, c in parties.items():
                    if c["Y"] != c["N"]:
                        pm[pcode] = "Y" if c["Y"] > c["N"] else "N"
                party_majority[roll] = pm

            # shared rollcall metadata file for this chamber/congress
            rc_out = []
            for roll in range(1, max_roll + 1):
                r = rollcalls.get(roll)
                if not r:
                    rc_out.append(None)
                    continue
                pm = party_majority.get(roll, {})
                rc_out.append({
                    "roll": roll,
                    "date": r["date"],
                    "bill": parse_bill(r["bill_number"], congress),
                    "question": r["vote_question"],
                    "result": r["vote_result"],
                    "desc": (r["vote_desc"] or r["dtl_desc"] or "").strip()[:400],
                    "yea": int(r["yea_count"] or 0),
                    "nay": int(r["nay_count"] or 0),
                    "party": {PARTY_CODES.get(k, str(k)): v for k, v in pm.items()},
                    "clerk": r.get("clerk_rollnumber"),
                })
            write_json(OUT / "rollcalls" / f"{key}.json", {"congress": congress, "chamber": chamber, "rollcalls": rc_out})

            for bg, arr in positions.items():
                if bg not in current_ids:
                    continue
                rec = by_bioguide[bg]
                rec["votes"][key] = "".join(arr)
                pcode = next((c for (b, c) in icpsr_info.values() if b == bg), None)
                for i, p in enumerate(arr):
                    rec["tally"][p] += 1
                    maj = party_majority.get(i + 1, {}).get(pcode)
                    if p in "YN" and maj:
                        rec["party_agree"][1] += 1
                        if p == maj:
                            rec["party_agree"][0] += 1

    out = {}
    for bg, rec in by_bioguide.items():
        t = rec["tally"]
        eligible = t["Y"] + t["N"] + t["P"] + t["X"]
        agree, total = rec["party_agree"]
        out[bg] = {
            "votes": rec["votes"],
            "stats": {
                "eligible": eligible,
                "yea": t["Y"], "nay": t["N"], "present": t["P"], "missed": t["X"],
                "missed_pct": round(100 * t["X"] / eligible, 1) if eligible else None,
                "party_pct": round(100 * agree / total, 1) if total else None,
            },
        }
    return out


# ---------------------------------------------------------------- 6. curated layer
def load_curated() -> tuple[dict, dict, list[str]]:
    problems: list[str] = []
    members, bills = {}, {}
    for p in sorted((CURATED / "members").glob("*.json")):
        if p.name.startswith("_"):
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        ok_st = []
        for i, s in enumerate(d.get("statements", [])):
            missing = [k for k in ("bill", "date", "quote", "source_url") if not s.get(k)]
            if missing:
                problems.append(f"{p.name} statements[{i}] missing {missing} — dropped")
            else:
                ok_st.append(s)
        ok_fc = []
        for i, fc in enumerate(d.get("fact_checks", [])):
            missing = [k for k in ("claim", "date", "rating", "publisher", "url") if not fc.get(k)]
            if missing:
                problems.append(f"{p.name} fact_checks[{i}] missing {missing} — dropped")
            else:
                fc.setdefault("bucket", rating_bucket(fc["rating"]))
                fc["origin"] = "curated"
                ok_fc.append(fc)
        members[p.stem] = {"statements": ok_st, "fact_checks": ok_fc}
    for p in sorted((CURATED / "bills").glob("*.json")):
        if p.name.startswith("_"):
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        ok = []
        for i, e in enumerate(d.get("effects", [])):
            missing = [k for k in ("summary", "kind", "source_name", "source_url") if not e.get(k)]
            if missing or e.get("kind") not in ("projection", "measured"):
                problems.append(f"{p.name} effects[{i}] invalid ({missing or 'kind'}) — dropped")
            else:
                ok.append(e)
        bills[p.stem] = {"title": d.get("title"), "effects": ok}
    return members, bills, problems


# ---------------------------------------------------------------- output
def seat_label(m: dict) -> str:
    if m["chamber"] == "senate":
        return f"U.S. Senator, {m['state']}"
    d = m.get("district")
    return f"U.S. Rep., {m['state']}-{'AL' if d in (0, None) else d}"


def update_manifest(key: str, label: str, level: str, count: int) -> None:
    """site/data/manifest.json lists every index file the site should load."""
    path = OUT / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"indexes": []}
    manifest["indexes"] = [x for x in manifest["indexes"] if x["key"] != key]
    manifest["indexes"].append({"key": key, "label": label, "level": level, "count": count,
                                "file": f"index-{key}.json",
                                "built": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    manifest["indexes"].sort(key=lambda x: ({"federal": 0, "state": 1, "local": 2}[x["level"]], x["key"]))
    write_json(path, manifest)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--congresses", default="118,119", help="congresses to pull votes and bills for")
    ap.add_argument("--fec-cycles", default="2026,2024", help="FEC cycles to search, newest first wins")
    ap.add_argument("--fec-itemized", action="store_true",
                    help="stream the FEC individual-contributions file (~2.5 GB) for donor states and employers")
    ap.add_argument("--skip-votes", action="store_true")
    ap.add_argument("--skip-bills", action="store_true")
    ap.add_argument("--skip-fec", action="store_true")
    ap.add_argument("--skip-factcheck", action="store_true")
    ap.add_argument("--cache-hours", type=float, default=24)
    args = ap.parse_args()

    f = Fetcher(args.cache_hours)
    max_age = args.cache_hours * 3600
    congresses = [int(c) for c in args.congresses.split(",")]
    errors: list[str] = []

    def step(name, fn, default):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - one source failing shouldn't sink the build
            log(f"  ! {name} failed: {e} — keeping previous data for it")
            errors.append(f"{name}: {e}")
            return default

    log("loading members…")
    members = load_members(f)
    log(f"  {len(members)} current members of Congress")

    votes = {} if args.skip_votes else (log("loading votes…") or step("votes", lambda: build_votes(f, members, congresses), {}))
    bills = {} if args.skip_bills else (log("loading bills (GovInfo)…") or step("bills", lambda: bulk.load_bills(congresses, max_age), {}))
    money = {} if args.skip_fec else (log("loading campaign money (FEC)…") or step(
        "fec", lambda: bulk.load_money(members, [int(c) for c in args.fec_cycles.split(",")], max_age, args.fec_itemized), {}))
    feed = None if args.skip_factcheck else (log("loading fact-checks (ClaimReview feed)…") or step(
        "factcheck", lambda: bulk.load_factcheck_feed(max_age), None))

    curated_members, curated_bills, problems = load_curated()
    for p in problems:
        log("  ! curated:", p)
    write_json(OUT / "bills.json", curated_bills)
    (ROOT / "site" / "METHODOLOGY.md").write_text((ROOT / "METHODOLOGY.md").read_text(encoding="utf-8"), encoding="utf-8")

    def previous(bg):
        p = OUT / "members" / f"{bg}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    index = []
    for m in members:
        bg = m["id"]
        prev = previous(bg)
        rec = dict(m)
        rec.update(level="federal", jurisdiction="US Congress", seat=seat_label(m))
        # sources skipped or failed this run keep their previous values
        rec["votes"] = votes.get(bg, {}).get("votes") or prev.get("votes", {})
        rec["vote_stats"] = votes.get(bg, {}).get("stats") or prev.get("vote_stats")
        rec["legislation"] = bills.get(bg) if bills else prev.get("legislation")
        rec["money"] = money.get(bg) if money else prev.get("money")
        auto_fc = bulk.match_factchecks(feed, m) if feed is not None else             [x for x in prev.get("fact_checks", []) if x.get("origin") != "curated"]

        cur = curated_members.get(bg, {})
        rec["statements"] = cur.get("statements", [])
        # curated entries win over automated duplicates of the same URL
        cur_urls = {x["url"] for x in cur.get("fact_checks", [])}
        rec["fact_checks"] = sorted(cur.get("fact_checks", []) + [x for x in auto_fc if x["url"] not in cur_urls],
                                    key=lambda x: x.get("date") or "", reverse=True)
        fc_counts = Counter(x["bucket"] for x in rec["fact_checks"])
        rec["fact_check_counts"] = {"checked": len(rec["fact_checks"]), **{k: fc_counts.get(k, 0) for k in ("false", "misleading", "true", "other")}}
        write_json(OUT / "members" / f"{bg}.json", rec)

        vs = rec.get("vote_stats") or {}
        mon = rec.get("money") or {}
        leg = (rec.get("legislation") or {}).get("counts") or {}
        index.append({
            "id": bg, "name": m["name"], "party": m["party"], "state": m["state"],
            "district": m["district"], "chamber": m["chamber"], "photo": m["photo"],
            "level": "federal", "jurisdiction": "US Congress", "seat": seat_label(m),
            "missed_pct": vs.get("missed_pct"), "party_pct": vs.get("party_pct"),
            "false": rec["fact_check_counts"]["false"], "checked": rec["fact_check_counts"]["checked"],
            "raised": (mon.get("totals") or {}).get("receipts"), "cycle": mon.get("cycle"),
            "sponsored": leg.get("sponsored"), "sponsored_law": leg.get("sponsored_law"),
            "statements": len(rec["statements"]),
        })

    index.sort(key=lambda r: (r["chamber"], r["state"], r["district"] or 0, r["name"]))
    write_json(OUT / "index-us.json", index)
    update_manifest("us", "U.S. Congress", "federal", len(index))
    write_json(OUT / "meta.json", {
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "congresses": congresses,
        "errors": errors,
        "curated_problems": problems,
    })
    log(f"done.{' errors: ' + '; '.join(errors) if errors else ''}")


if __name__ == "__main__":
    main()
