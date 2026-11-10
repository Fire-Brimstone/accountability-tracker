#!/usr/bin/env python3
"""Build state and local officials for one state (currently tuned for Colorado).

Sources
  - openstates/people (GitHub)     legislators, statewide executives, big-city mayors   no key
  - curated/people/<st>.json       officials missing from openstates (sourced)          no key
  - Open States API v3             floor votes, sponsored/cosponsored bills             OPENSTATES_API_KEY
  - Colorado TRACER bulk data      campaign contributions                               no key
  - Data Commons ClaimReview feed  fact-checks                                          no key
  - curated/                       statements, bill effects, manual fact-checks

Get a free Open States key at https://open.pluralpolicy.com/accounts/signup/ (Account → API key).

Usage
  python pipeline/build_state.py --state co
  python pipeline/build_state.py --state co --since 2025-01-01 --skip-votes
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timezone

import miniyaml
import bulk
from build import CACHE, CURATED, OUT, Fetcher, RateLimited, load_curated, log, update_manifest, write_json

OS_KEY = os.environ.get("OPENSTATES_API_KEY")
OS_API = "https://v3.openstates.org"
GH_API = "https://api.github.com/repos/openstates/people/contents/data/{st}/{dir}"
TRACER_ZIP = "https://tracer.sos.colorado.gov/PublicSite/Docs/BulkDataDownloads/{year}_ContributionData.csv.zip"
TRACER_SEARCH = "https://tracer.sos.colorado.gov/PublicSite/SearchPages/CandidateSearch.aspx"

STATE_NAMES = {"co": "Colorado"}
PARTY_NORMAL = {"Democratic": "Democrat", "Republican": "Republican", "Independent": "Independent",
                "Unaffiliated": "Unaffiliated", "Libertarian": "Libertarian", "Green": "Green"}
EXEC_TITLES = {"governor": "Governor", "lt_governor": "Lieutenant Governor", "lieutenant governor": "Lieutenant Governor",
               "attorney general": "Attorney General", "secretary of state": "Secretary of State",
               "treasurer": "State Treasurer", "state treasurer": "State Treasurer"}
SUFFIXES = {"JR", "SR", "II", "III", "IV"}
NON_EMPLOYERS = {"", "N/A", "NA", "NONE", "RETIRED", "NOT EMPLOYED", "SELF", "SELF-EMPLOYED", "SELF EMPLOYED",
                 "UNEMPLOYED", "HOMEMAKER", "NOT CURRENTLY EMPLOYED", "STUDENT", "UNKNOWN", "REQUESTED"}
NON_OCCUPATIONS = {"", "Retired", "Not Currently Employed", "Other", "Unknown", "Homemaker", "Student"}


def party_letter(p: str | None) -> str:
    return (p or "?")[0].upper()


# ---------------------------------------------------------------- people
def load_people(f: Fetcher, st: str) -> list[dict]:
    today = date.today().isoformat()
    people = []
    for directory, level in (("legislature", "state"), ("executive", "state"), ("municipalities", "local")):
        try:
            listing = f.get(GH_API.format(st=st, dir=directory), min_interval=1)
        except Exception as e:  # noqa: BLE001
            log(f"  ! could not list {directory}: {e}")
            continue
        for entry in listing:
            if not entry["name"].endswith(".yml"):
                continue
            p = miniyaml.loads(f.get(entry["download_url"], as_json=False))
            rec = person_record(p, st, level, today)
            if rec:
                people.append(rec)
    # officials missing from openstates (e.g. state treasurer), added by hand with sources
    extra = CURATED / "people" / f"{st}.json"
    if extra.exists():
        for p in json.loads(extra.read_text(encoding="utf-8")).get("people", []):
            rec = person_record(p, st, p.get("_level", "state"), today)
            if rec:
                rec["curated_person"] = True
                people.append(rec)
    return people


def person_record(p: dict, st: str, level: str, today: str) -> dict | None:
    roles = p.get("roles") or []
    current = [r for r in roles if str(r.get("start_date") or "0000") <= today and str(r.get("end_date") or "9999") >= today]
    if not current:
        return None
    role = current[0]
    rtype = str(role.get("type"))
    uuid = p["id"].split("/")[-1]
    state_name = STATE_NAMES.get(st, st.upper())
    if rtype in ("upper", "lower"):
        chamber = "state-senate" if rtype == "upper" else "state-house"
        seat = f"{'State Senator' if rtype == 'upper' else 'State Rep.'}, {st.upper()} District {role.get('district')}"
        jurisdiction = state_name
    elif rtype == "mayor":
        chamber = "mayor"
        place = re.search(r"place:([a-z_~.-]+)", role.get("jurisdiction", ""))
        city = place.group(1).replace("_", " ").title() if place else "?"
        seat = f"Mayor of {city}, {st.upper()}"
        jurisdiction = city
    else:
        chamber = "executive"
        seat = f"{EXEC_TITLES.get(rtype, rtype.replace('_', ' ').title())} of {state_name}"
        jurisdiction = state_name
    same_type = [str(r["start_date"]) for r in roles if r.get("type") == rtype and r.get("start_date")]
    party = ((p.get("party") or [{}])[0] or {}).get("name")
    links = [{"label": "Official page", "url": l["url"]} for l in (p.get("links") or [])[:1] if l.get("url")]
    links += [{"label": "Ballotpedia" if "ballotpedia" in s["url"] else "Wikipedia" if "wikipedia" in s["url"] else None,
               "url": s["url"]} for s in (p.get("sources") or []) if any(k in s.get("url", "") for k in ("ballotpedia", "wikipedia"))]
    phone = next((o.get("voice") for o in (p.get("offices") or []) if o.get("voice")), None)
    given = p.get("given_name") or p["name"].split()[0]
    family = p.get("family_name") or p["name"].split()[-1]
    return {
        "id": f"{st.upper()}-{uuid[:8]}",
        "ocd_id": p["id"],
        "name": p["name"],
        "first": given,
        "last": family,
        "other_names": [o["name"] for o in (p.get("other_names") or []) if o.get("name")],
        "party": PARTY_NORMAL.get(party, party) if party else ("Nonpartisan" if chamber == "mayor" else ""),
        "state": st.upper(),
        "district": role.get("district"),
        "chamber": chamber,
        "level": level,
        "jurisdiction": jurisdiction,
        "seat": seat,
        "title": (p.get("extras") or {}).get("title"),
        "since": min(same_type) if same_type else None,
        "term_end": role.get("end_date"),
        "website": links[0]["url"] if links else None,
        "links": [l for l in links if l["label"]],
        "phone": phone,
        "email": p.get("email"),
        "photo": p.get("image"),
        "ids": {"openstates": p["id"]},
    }


# ---------------------------------------------------------------- votes & bills (Open States API)
def os_get(f: Fetcher, path: str, **params):
    q = urllib.parse.urlencode([(k, x) for k, v in params.items() for x in (v if isinstance(v, list) else [v])])
    return f.get(f"{OS_API}{path}?{q}&apikey={urllib.parse.quote(OS_KEY)}", min_interval=6.5, timeout=120)


def os_bill_id(st: str, identifier: str, session: str) -> str:
    ident = re.sub(r"\s+", "", identifier.lower())
    # Colorado identifiers already contain the year (HB25-1001); others get the session appended
    return f"{st}-{ident}" if re.search(r"\d{2}-\d+", ident) else f"{st}-{ident}-{session.lower()}"


def build_votes_and_bills(f: Fetcher, st: str, people: list[dict], since: str):
    by_ocd = {p["ocd_id"]: p for p in people}
    by_id = {p["id"]: p for p in people}
    # fallback name lookup for votes whose voter isn't linked to a person id
    name_lookup: dict[tuple, str] = {}
    for p in people:
        if p["chamber"] not in ("state-senate", "state-house"):
            continue
        ch = "upper" if p["chamber"] == "state-senate" else "lower"
        for n in [p["name"], p["last"], *p["other_names"]]:
            name_lookup.setdefault((ch, n.lower().strip()), p["id"])

    juris = os_get(f, f"/jurisdictions/{st}", include="legislative_sessions")
    sessions = [s for s in juris.get("legislative_sessions", []) if str(s.get("start_date") or "") >= since]
    sessions.sort(key=lambda s: s.get("start_date") or "")
    log(f"  sessions: {[s['identifier'] for s in sessions]}")

    legislation = defaultdict(lambda: {"sponsored": [], "cosponsored": []})
    rollcalls: dict[str, list] = defaultdict(list)          # key -> list of vote dicts (unsorted)
    positions: dict[str, dict] = defaultdict(dict)          # key -> {member_id: {vote_uid: pos}}

    for s in sessions:
        sid = s["identifier"]
        page, max_page = 1, 1
        while page <= max_page:
            data = os_get(f, "/bills", jurisdiction=st, session=sid, include=["sponsorships", "votes"],
                          per_page=20, page=page, sort="first_action_asc")
            max_page = data.get("pagination", {}).get("max_page", 1)
            log(f"    {sid} bills page {page}/{max_page}")
            for b in data.get("results", []):
                bid = os_bill_id(st, b["identifier"], sid)
                text = b.get("latest_action_description") or ""
                bill = {
                    "id": bid, "label": b["identifier"], "session": sid, "title": b.get("title"),
                    "introduced": (b.get("first_action_date") or "")[:10],
                    "policy_area": ", ".join((b.get("subject") or [])[:2]) or None,
                    "latest_action": text, "latest_action_date": (b.get("latest_action_date") or "")[:10],
                    "became_law": bool(re.search(r"governor signed|became law|signed by governor|became law without", text, re.I)),
                    "vetoed": bool(re.search(r"vetoed", text, re.I)),
                    "url": b.get("openstates_url"),
                }
                for sp in b.get("sponsorships") or []:
                    person = by_ocd.get((sp.get("person") or {}).get("id"))
                    if person:
                        legislation[person["id"]]["sponsored" if sp.get("primary") else "cosponsored"].append(bill)
                for v in b.get("votes") or []:
                    org = (v.get("organization") or {}).get("classification")
                    if org not in ("upper", "lower"):
                        continue  # floor votes only; committee votes are skipped
                    key = f"{st.upper()}-{sid}-{'S' if org == 'upper' else 'H'}"
                    counts = {c["option"]: c["value"] for c in v.get("counts") or []}
                    party_tally: dict[str, Counter] = defaultdict(Counter)
                    tally = Counter()
                    uid = v["id"]
                    for pv in v.get("votes") or []:
                        pos = {"yes": "Y", "no": "N"}.get(pv.get("option"),
                              "X" if pv.get("option") in ("absent", "excused", "not voting") else "P")
                        voter = pv.get("voter") or {}
                        member = by_ocd.get(voter.get("id"))
                        mid = member["id"] if member else name_lookup.get((org, (pv.get("voter_name") or "").lower().strip()))
                        if mid:
                            positions[key].setdefault(mid, {})[uid] = pos
                        tally[pos] += 1
                        vp = voter.get("party") or (by_id.get(mid) or {}).get("party")
                        if vp and pos in "YN":
                            party_tally[party_letter(PARTY_NORMAL.get(vp, vp))][pos] += 1
                    rollcalls[key].append({
                        "uid": uid,
                        "date": (v.get("start_date") or "")[:10],
                        "bill": {"id": bid, "label": b["identifier"], "url": b.get("openstates_url")},
                        "question": v.get("motion_text"),
                        "result": v.get("result"),
                        "desc": (b.get("title") or "")[:400],
                        "yea": counts.get("yes", tally["Y"]), "nay": counts.get("no", tally["N"]),
                        "party": {p: ("Y" if c["Y"] > c["N"] else "N") for p, c in party_tally.items() if c["Y"] != c["N"]},
                    })
            page += 1

    votes_out: dict[str, dict] = defaultdict(dict)
    stats: dict[str, Counter] = defaultdict(Counter)
    party_of = {p["id"]: party_letter(p["party"]) for p in people}
    for key, rcs in rollcalls.items():
        rcs.sort(key=lambda r: (r["date"], r["uid"]))
        for i, r in enumerate(rcs, 1):
            r["roll"] = i
        write_json(OUT / "rollcalls" / f"{key}.json", {"chamber": key, "rollcalls": rcs})
        for mid, pos_by_uid in positions[key].items():
            s = "".join(pos_by_uid.get(r["uid"], "-") for r in rcs)
            votes_out[mid][key] = s
            for r, ch in zip(rcs, s):
                stats[mid][ch] += 1
                maj = r["party"].get(party_of.get(mid))
                if ch in "YN" and maj:
                    stats[mid]["agree_total"] += 1
                    stats[mid]["agree"] += ch == maj

    def vstats(mid):
        t = stats[mid]
        eligible = t["Y"] + t["N"] + t["P"] + t["X"]
        if not eligible:
            return None
        return {"eligible": eligible, "yea": t["Y"], "nay": t["N"], "present": t["P"], "missed": t["X"],
                "missed_pct": round(100 * t["X"] / eligible, 1),
                "party_pct": round(100 * t["agree"] / t["agree_total"], 1) if t["agree_total"] else None}

    leg_out = {}
    for mid, L in legislation.items():
        # a bill can appear in several sessions' listings; de-duplicate by id
        for k in ("sponsored", "cosponsored"):
            L[k] = list({b["id"]: b for b in L[k]}.values())
            L[k].sort(key=lambda b: b.get("introduced") or "", reverse=True)
        leg_out[mid] = {**L, "counts": {
            "sponsored": len(L["sponsored"]), "sponsored_law": sum(b["became_law"] for b in L["sponsored"]),
            "cosponsored": len(L["cosponsored"]), "cosponsored_law": sum(b["became_law"] for b in L["cosponsored"])}}
    return votes_out, {mid: vstats(mid) for mid in votes_out}, leg_out


# ---------------------------------------------------------------- money (Colorado TRACER)
NICKNAMES = {
    "TONY": {"ANTHONY", "THOMAS", "ANTONIO"}, "BOB": {"ROBERT"}, "BOBBY": {"ROBERT"}, "BILL": {"WILLIAM"},
    "WILL": {"WILLIAM"}, "MATT": {"MATTHEW"}, "MIKE": {"MICHAEL"}, "DAVE": {"DAVID"}, "RICK": {"RICHARD", "FREDERICK", "MERRICK"},
    "DICK": {"RICHARD"}, "JIM": {"JAMES"}, "JIMMY": {"JAMES"}, "JOE": {"JOSEPH"}, "CHRIS": {"CHRISTOPHER", "CHRISTINE", "CHRISTINA"},
    "LIZ": {"ELIZABETH"}, "BETH": {"ELIZABETH"}, "KATHY": {"KATHERINE", "KATHLEEN"}, "KATIE": {"KATHERINE", "KATHRYN"},
    "DAN": {"DANIEL"}, "TOM": {"THOMAS"}, "STEVE": {"STEVEN", "STEPHEN"}, "JEFF": {"JEFFREY"}, "ED": {"EDWARD"},
    "NICK": {"NICHOLAS"}, "RON": {"RONALD"}, "BARB": {"BARBARA"}, "JON": {"JONATHAN"}, "SAM": {"SAMUEL", "SAMANTHA"},
    "TIM": {"TIMOTHY"}, "TED": {"EDWARD", "THEODORE"}, "GREG": {"GREGORY"}, "JUDY": {"JUDITH"}, "PEGGY": {"MARGARET"},
    "PHIL": {"PHILIP", "PHILLIP"}, "NIKKI": {"NICOLE", "NAQUETTA"}, "CHELA": {"GRACIELA"}, "JULIE": {"JULIA"},
    "ANDY": {"ANDREW"}, "ALEX": {"ALEXANDER", "ALEXANDRA"}, "BEN": {"BENJAMIN"}, "PAT": {"PATRICK", "PATRICIA"},
}


def norm_tokens(name: str) -> list[str]:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return [t for t in re.sub(r"[^A-Z\s-]", " ", ascii_name.upper()).replace("-", " ").split() if t not in SUFFIXES]


def name_match(cand: str, p: dict) -> bool:
    """TRACER candidate name vs. an official. Last name must match; the official's first name (or a
    nickname of it) must match ANY other token, since filers often use a full legal name
    ("TIMOTHY JARVIS CALDWELL" for Jarvis Caldwell)."""
    toks = norm_tokens(cand)
    fam = norm_tokens(p["last"])
    if len(toks) < 2 or not fam:
        return False
    if toks[-len(fam):] == fam:
        rest = toks[:-len(fam)]
    elif toks[-1] == fam[-1]:
        rest = toks[:-1]
    else:
        return False
    firsts = set()
    for n in [p["first"], p["name"], *p.get("other_names", [])]:
        nt = norm_tokens(n)
        if nt and nt[-1] == fam[-1]:
            nt = nt[:-1]
        if nt:
            firsts.add(nt[0])
    for fn in list(firsts):
        firsts |= NICKNAMES.get(fn, set())
        firsts |= {nick for nick, full in NICKNAMES.items() if fn in full}  # formal name -> nickname
    return any(t == fn or (len(t) >= 4 and len(fn) >= 4 and t[:4] == fn[:4]) for t in rest for fn in firsts)


def size_bucket(a: float) -> str:
    if a <= 50: return "$50 and under"
    if a <= 200: return "$51–$200"
    if a <= 400: return "$201–$400"
    if a <= 1000: return "$401–$1,000"
    return "Over $1,000"


SIZE_ORDER = ["$50 and under", "$51–$200", "$201–$400", "$401–$1,000", "Over $1,000"]
TYPE_BUCKETS = {
    "Individual": "individual", "Small Donor Committee": "sdc", "Political Committee": "pac",
    "Candidate Committee": "pac", "527 Political Organization": "pac", "Political Party Committee": "party",
    "Candidate": "self", "Business": "business", "Corporation": "business", "Partnership": "business",
    "LLC": "business", "Labor Union": "union",
}
TYPE_LABELS = {
    "small": ("Unitemized small gifts (under $20)", "small"), "individual": ("Individual donors", "individual"),
    "sdc": ("Small donor committees", "sdc"), "pac": ("Political committees (PACs)", "pac"),
    "party": ("Party committees", "party"), "self": ("Candidate's own money", "self"),
    "business": ("Businesses", "business"), "union": ("Labor unions", "union"), "other": ("Other / unknown", "other"),
}


def tracer_rows(f: Fetcher, year: int, max_age: float):
    path = CACHE.parent / "tracer" / f"{year}_ContributionData.csv.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or time.time() - path.stat().st_mtime > max_age:
        log(f"  downloading TRACER {year}…")
        req = urllib.request.Request(TRACER_ZIP.format(year=year), headers={"User-Agent": "accountability-monitor/0.1"})
        with urllib.request.urlopen(req, timeout=600) as r:
            path.write_bytes(r.read())
    z = zipfile.ZipFile(path)
    with z.open(z.namelist()[0]) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="latin-1"))


def build_money_tracer(f: Fetcher, people: list[dict], years: list[int], max_age: float) -> dict:
    agg: dict[tuple, dict] = {}
    for y in years:
        try:
            rows = tracer_rows(f, y, max_age)
            for r in rows:
                if r["CommitteeType"] != "Candidate Committee" or r["Amended"] == "Y" or not r["CandidateName"]:
                    continue
                try:
                    amt = float(r["ContributionAmount"] or 0)
                except ValueError:
                    continue
                d = (r["ContributionDate"] or "")[:10]
                yr = int(d[:4]) if d[:4].isdigit() else y
                cycle = yr if yr % 2 == 0 else yr + 1
                key = (r["CandidateName"].strip().upper(), cycle)
                a = agg.get(key)
                if a is None:
                    a = agg[key] = {"total": 0.0, "n": 0, "types": Counter(), "sizes": Counter(), "states": Counter(),
                                    "employers": Counter(), "employer_n": Counter(), "occupations": Counter(),
                                    "committees": {}, "last": ""}
                ctype = r["ContributionType"]
                if ctype == "Returned Contributions":
                    a["total"] -= abs(amt)
                    continue
                a["total"] += amt
                a["n"] += 1
                a["last"] = max(a["last"], d)
                a["committees"][r["CO_ID"]] = r["CommitteeName"]
                if "Non-Itemized" in ctype:
                    bucket = "small"
                else:
                    bucket = TYPE_BUCKETS.get(r["ContributorType"], "other")
                a["types"][bucket] += amt
                if bucket == "individual":
                    a["sizes"][size_bucket(amt)] += amt
                    if r["State"]:
                        a["states"][r["State"].upper()] += amt
                    emp = (r["Employer"] or "").strip().upper()
                    if emp not in NON_EMPLOYERS and not emp.startswith(("RETI", "RETT", "NOT EMPL", "SELF")):
                        a["employers"][emp] += amt
                        a["employer_n"][emp] += 1
                    if r["Occupation"] not in NON_OCCUPATIONS:
                        a["occupations"][r["Occupation"]] += amt
                elif bucket == "small":
                    a["sizes"]["$50 and under"] += amt
        except Exception as e:  # noqa: BLE001
            log(f"  ! TRACER {y}: {e}")

    cand_names = {k[0] for k in agg}
    out = {}
    for p in people:
        if p["level"] != "state":
            continue  # municipal candidates file with their city, not TRACER
        matches = [c for c in cand_names if name_match(c, p)]
        if not matches:
            continue
        by_cycle = Counter()
        for k, a in agg.items():
            if k[0] in matches:
                by_cycle[k[1]] += a["total"]
        cycles = sorted((c for c, t in by_cycle.items() if t > 0), reverse=True)
        if not cycles:
            continue
        # most recent cycle with real fundraising; a term-limited official may only have a few
        # stray gifts in the latest cycle, which would misrepresent who funds them
        cycle = next((c for c in cycles if by_cycle[c] >= 5000), cycles[0])
        parts = [agg[(c, cycle)] for c in matches if (c, cycle) in agg]
        merged = {"total": 0.0, "n": 0, "types": Counter(), "sizes": Counter(), "states": Counter(), "employers": Counter(),
                  "employer_n": Counter(), "occupations": Counter(), "committees": {}, "last": ""}
        for a in parts:
            for k in ("total", "n"):
                merged[k] += a[k]
            for k in ("types", "sizes", "states", "employers", "employer_n", "occupations"):
                merged[k].update(a[k])
            merged["committees"].update(a["committees"])
            merged["last"] = max(merged["last"], a["last"])
        states_total = sum(merged["states"].values()) or None
        out[p["id"]] = {
            "source": "tracer",
            "source_name": "Colorado Secretary of State (TRACER)",
            "source_url": TRACER_SEARCH,
            "matched_names": sorted(matches),
            "cycle": cycle,
            "cycle_label": f"{cycle - 1}–{cycle}",
            "all_cycles": [{"cycle": c, "total": round(by_cycle[c], 2)} for c in cycles],
            "coverage_end": merged["last"],
            "committee": {"id": next(iter(merged["committees"])), "name": next(iter(merged["committees"].values()))},
            "committees": [{"id": k, "name": v} for k, v in merged["committees"].items()],
            "totals": {"receipts": round(merged["total"], 2), "count": merged["n"]},
            "composition": [{"label": TYPE_LABELS[k][0], "amount": round(v, 2), "kind": TYPE_LABELS[k][1]}
                            for k, v in sorted(merged["types"].items(), key=lambda kv: -kv[1]) if v > 0],
            "by_size": [{"label": s, "total": round(merged["sizes"][s], 2)} for s in SIZE_ORDER if merged["sizes"][s]],
            "top_states": [{"state": s, "total": round(v, 2)} for s, v in merged["states"].most_common(10)],
            "home_state_pct": round(100 * merged["states"].get(p["state"], 0) / states_total, 1) if states_total else None,
            "top_employers": [{"employer": e, "total": round(v, 2), "count": merged["employer_n"][e]}
                              for e, v in merged["employers"].most_common(20)],
            "top_occupations": [{"occupation": o, "total": round(v, 2)} for o, v in merged["occupations"].most_common(10)],
            "employer_note": "Employers and occupation categories are as reported by individual donors on Colorado filings.",
        }
    return out


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="co")
    ap.add_argument("--since", default="2025-01-01", help="include legislative sessions starting on/after this date")
    ap.add_argument("--money-years", default="2023,2024,2025,2026")
    ap.add_argument("--skip-votes", action="store_true")
    ap.add_argument("--skip-money", action="store_true")
    ap.add_argument("--skip-factcheck", action="store_true")
    ap.add_argument("--cache-hours", type=float, default=24)
    args = ap.parse_args()
    st = args.state.lower()
    if st != "co":
        log("note: money import is Colorado-specific (TRACER); other states get people, votes and bills only")

    f = Fetcher(args.cache_hours)
    log("loading people…")
    people = load_people(f, st)
    log(f"  {len(people)} current officials: {Counter(p['chamber'] for p in people)}")

    votes, vstats, leg = {}, {}, {}
    if not args.skip_votes:
        if not OS_KEY:
            log("  ! OPENSTATES_API_KEY not set — skipping votes and bills")
        else:
            log("loading votes and bills (Open States)…")
            try:
                votes, vstats, leg = build_votes_and_bills(f, st, people, args.since)
            except RateLimited as e:
                log(f"  ! Open States rate limit reached ({e}); rerun later — cached pages are reused")

    money = {}
    if not args.skip_money and st == "co":
        log("loading campaign finance (TRACER)…")
        money = build_money_tracer(f, people, [int(y) for y in args.money_years.split(",")], args.cache_hours * 3600)
        log(f"  matched money for {len(money)} officials")

    curated_members, _, problems = load_curated()
    for p in problems:
        log("  ! curated:", p)

    def previous(mid):
        p = OUT / "members" / f"{mid}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    feed = None
    if not args.skip_factcheck:
        log("loading fact-checks (ClaimReview feed)…")
        try:
            feed = bulk.load_factcheck_feed(args.cache_hours * 3600)
        except Exception as e:  # noqa: BLE001
            log(f"  ! fact-check feed: {e}")
    indexes = defaultdict(list)
    for p in people:
        mid = p["id"]
        prev = previous(mid)
        rec = dict(p)
        rec["votes"] = votes.get(mid) or prev.get("votes", {})
        rec["vote_stats"] = vstats.get(mid) or prev.get("vote_stats")
        rec["legislation"] = leg.get(mid) or prev.get("legislation")
        rec["money"] = money.get(mid) or prev.get("money")
        auto_fc = bulk.match_factchecks(feed, p) if feed is not None else             [x for x in prev.get("fact_checks", []) if x.get("origin") != "curated"]
        cur = curated_members.get(mid, {})
        rec["statements"] = cur.get("statements", [])
        cur_urls = {x["url"] for x in cur.get("fact_checks", [])}
        rec["fact_checks"] = sorted(cur.get("fact_checks", []) + [x for x in auto_fc if x["url"] not in cur_urls],
                                    key=lambda x: x.get("date") or "", reverse=True)
        c = Counter(x["bucket"] for x in rec["fact_checks"])
        rec["fact_check_counts"] = {"checked": len(rec["fact_checks"]), **{k: c.get(k, 0) for k in ("false", "misleading", "true", "other")}}
        write_json(OUT / "members" / f"{mid}.json", rec)

        vs, mon, lc = rec.get("vote_stats") or {}, rec.get("money") or {}, (rec.get("legislation") or {}).get("counts") or {}
        indexes[p["level"]].append({
            "id": mid, "name": p["name"], "party": p["party"], "state": p["state"], "district": p["district"],
            "chamber": p["chamber"], "photo": p["photo"], "level": p["level"], "jurisdiction": p["jurisdiction"],
            "seat": p["seat"], "missed_pct": vs.get("missed_pct"), "party_pct": vs.get("party_pct"),
            "false": rec["fact_check_counts"]["false"], "checked": rec["fact_check_counts"]["checked"],
            "raised": (mon.get("totals") or {}).get("receipts"), "cycle": mon.get("cycle"),
            "sponsored": lc.get("sponsored"), "sponsored_law": lc.get("sponsored_law"),
            "statements": len(rec["statements"]),
        })

    order = {"executive": 0, "state-senate": 1, "state-house": 2, "mayor": 3}
    for level, rows in indexes.items():
        rows.sort(key=lambda r: (order.get(r["chamber"], 9), int(r["district"]) if str(r["district"] or "").isdigit() else 0, r["name"]))
        key = st if level == "state" else f"{st}-local"
        label = f"{STATE_NAMES.get(st, st.upper())} {'state government' if level == 'state' else 'local officials'}"
        write_json(OUT / f"index-{key}.json", rows)
        update_manifest(key, label, level, len(rows))
    log(f"done. {f.network_calls} network calls.")


if __name__ == "__main__":
    main()
