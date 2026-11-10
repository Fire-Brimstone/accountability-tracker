"""Keyless bulk data sources: no API keys, no sign-ups.

  GovInfo BILLSTATUS bulk XML  - every bill's sponsor, cosponsors, status, laws, CBO estimates
  FEC bulk files                - campaign totals per committee; optional itemized individual gifts
  Data Commons ClaimReview feed - the public feed of fact-checks behind Google's Fact Check Explorer

Big files are downloaded once into .cache/bulk/ and reused until they are older than --cache-hours.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BULK = ROOT / ".cache" / "bulk"

GOVINFO_ZIP = "https://www.govinfo.gov/bulkdata/BILLSTATUS/{c}/{t}/BILLSTATUS-{c}-{t}.zip"
FEC_BULK = "https://www.fec.gov/files/bulk-downloads/{year}/{name}"
CLAIMREVIEW_FEED = "https://storage.googleapis.com/datacommons-feeds/claimreview/latest/data.json"

BILL_TYPES = ["hr", "s", "hjres", "sjres", "hconres", "sconres", "hres", "sres"]
BILL_LABELS = {"HR": "H.R.", "S": "S.", "HRES": "H.Res.", "SRES": "S.Res.", "HJRES": "H.J.Res.",
               "SJRES": "S.J.Res.", "HCONRES": "H.Con.Res.", "SCONRES": "S.Con.Res."}
BILL_SLUGS = {"hr": "house-bill", "s": "senate-bill", "hres": "house-resolution", "sres": "senate-resolution",
              "hjres": "house-joint-resolution", "sjres": "senate-joint-resolution",
              "hconres": "house-concurrent-resolution", "sconres": "senate-concurrent-resolution"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def download(url: str, name: str, max_age_s: float) -> Path:
    """Stream a (possibly multi-GB) file to .cache/bulk/<name>, reusing a fresh copy."""
    path = BULK / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and time.time() - path.stat().st_mtime < max_age_s:
        return path
    log(f"  downloading {url}")
    tmp = path.with_suffix(path.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "accountability-monitor/0.1"})
    with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as out:
        while chunk := r.read(1 << 20):
            out.write(chunk)
    tmp.replace(path)
    return path


# ---------------------------------------------------------------- bills (GovInfo)
def bill_label(t: str, n: str | int) -> str:
    return f"{BILL_LABELS.get(t.upper(), t.upper())} {n}"


def bill_url(congress: int, t: str, n) -> str:
    return f"https://www.congress.gov/bill/{congress}th-congress/{BILL_SLUGS.get(t.lower(), 'bill')}/{n}"


def load_bills(congresses: list[int], max_age_s: float) -> dict[str, dict]:
    """Returns {bioguide: {"sponsored": [...], "cosponsored": [...]}} for the given congresses."""
    out: dict[str, dict] = defaultdict(lambda: {"sponsored": [], "cosponsored": []})
    for c in congresses:
        for t in BILL_TYPES:
            try:
                path = download(GOVINFO_ZIP.format(c=c, t=t), f"BILLSTATUS-{c}-{t}.zip", max_age_s)
            except Exception as e:  # noqa: BLE001
                log(f"  ! GovInfo {c} {t}: {e}")
                continue
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if not name.endswith(".xml"):
                        continue
                    b = ET.fromstring(z.read(name)).find("bill")
                    if b is None:
                        continue
                    btype, num = b.findtext("type") or t.upper(), b.findtext("number")
                    action = b.findtext("latestAction/text") or ""
                    laws = [f"{(l.findtext('type') or 'Public Law').replace('Public Law', 'P.L.')} {l.findtext('number')}"
                            for l in b.findall("laws/item")]
                    bill = {
                        "id": f"{btype.lower()}{num}-{c}",
                        "label": bill_label(btype, num),
                        "congress": c,
                        "title": b.findtext("title"),
                        "introduced": b.findtext("introducedDate"),
                        "policy_area": b.findtext("policyArea/name"),
                        "latest_action": action,
                        "latest_action_date": b.findtext("latestAction/actionDate"),
                        "became_law": bool(laws) or bool(re.search(r"Became Public Law|Signed by President", action, re.I)),
                        "vetoed": bool(re.search(r"Vetoed by President", action, re.I)),
                        "laws": laws,
                        "cbo": [{"title": e.findtext("title"), "url": e.findtext("url"), "date": (e.findtext("pubDate") or "")[:10]}
                                for e in b.findall("cboCostEstimates/item")],
                        "url": bill_url(c, btype, num),
                    }
                    for s in b.findall("sponsors/item"):
                        if s.findtext("bioguideId"):
                            out[s.findtext("bioguideId")]["sponsored"].append(bill)
                    for s in b.findall("cosponsors/item"):
                        if s.findtext("bioguideId") and not s.findtext("sponsorshipWithdrawnDate"):
                            out[s.findtext("bioguideId")]["cosponsored"].append(bill)
        log(f"  bills loaded for {c}th Congress")
    result = {}
    for bg, L in out.items():
        for k in ("sponsored", "cosponsored"):
            L[k].sort(key=lambda b: b.get("introduced") or "", reverse=True)
        result[bg] = {**L, "counts": {
            "sponsored": len(L["sponsored"]), "sponsored_law": sum(b["became_law"] for b in L["sponsored"]),
            "cosponsored": len(L["cosponsored"]), "cosponsored_law": sum(b["became_law"] for b in L["cosponsored"])},
            "coverage": f"{min(congresses)}th–{max(congresses)}th Congress"}
    return result


# ---------------------------------------------------------------- money (FEC bulk)
def _num(x) -> float:
    try:
        return round(float(x or 0), 2)
    except ValueError:
        return 0.0


def _date(s: str) -> str:
    s = (s or "").strip()
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else s[:10]


SIZE_ORDER = ["Under $200 (unitemized)", "$200–$499", "$500–$999", "$1,000–$1,999", "$2,000 and over"]


def _size(amount: float) -> str:
    if amount < 500: return "$200–$499"
    if amount < 1000: return "$500–$999"
    if amount < 2000: return "$1,000–$1,999"
    return "$2,000 and over"


NON_EMPLOYERS = {"", "N/A", "NA", "NONE", "NULL", "RETIRED", "NOT EMPLOYED", "SELF-EMPLOYED", "SELF EMPLOYED",
                 "SELF", "UNEMPLOYED", "HOMEMAKER", "INFORMATION REQUESTED", "INFORMATION REQUESTED PER BEST EFFORTS",
                 "REQUESTED", "STUDENT", "NOT-EMPLOYED", "DISABLED", "ENTREPRENEUR"}


def load_money(members: list[dict], cycles: list[int], max_age_s: float, itemized: bool = False) -> dict[str, dict]:
    """FEC committee summaries (small files) → totals for every member's principal campaign committee.
    With itemized=True, also streams the multi-GB individual contributions file for the newest cycle to get
    donor states, employers and gift sizes."""
    cand_to_member = {}
    for m in members:
        for cid in m["ids"].get("fec", []):
            cand_to_member[cid] = m
    # The committee summary leaves CAND_ID blank for senators who aren't on this cycle's ballot, so link
    # principal committees to candidates through FEC's candidate-committee linkage files instead.
    cmte_to_member = {}
    for year in sorted({c for c in cycles} | {max(cycles) - 4, max(cycles) + 2}):
        try:
            path = download(FEC_BULK.format(year=year, name=f"ccl{str(year)[2:]}.zip"), f"ccl{str(year)[2:]}.zip", max_age_s)
        except Exception:  # noqa: BLE001 - future cycles may not be published yet
            continue
        with zipfile.ZipFile(path) as z, z.open(z.namelist()[0]) as raw:
            for line in io.TextIOWrapper(raw, encoding="latin-1"):
                f = line.rstrip().split("|")   # CAND_ID|CAND_ELECTION_YR|FEC_ELECTION_YR|CMTE_ID|CMTE_TP|CMTE_DSGN|LINKAGE_ID
                if len(f) >= 6 and f[5] == "P" and f[0] in cand_to_member:
                    cmte_to_member[f[3]] = (cand_to_member[f[0]], f[0])
    found: dict[str, dict] = {}
    for cycle in sorted(cycles, reverse=True):
        try:
            path = download(FEC_BULK.format(year=cycle, name=f"committee_summary_{cycle}.csv"),
                            f"committee_summary_{cycle}.csv", max_age_s)
        except Exception as e:  # noqa: BLE001
            log(f"  ! FEC committee summary {cycle}: {e}")
            continue
        with open(path, encoding="latin-1", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("CMTE_DSGN") != "P":
                    continue
                m, cand_id = cmte_to_member.get(r["CMTE_ID"], (cand_to_member.get(r.get("CAND_ID")), r.get("CAND_ID")))
                if not m:
                    continue
                receipts = _num(r["TTL_RECEIPTS"])
                if receipts <= 0:
                    continue
                if m["id"] in found:   # an older cycle: just record its total for comparison
                    found[m["id"]]["all_cycles"].append({"cycle": cycle, "total": receipts})
                    continue
                t = {
                    "receipts": receipts, "disbursements": _num(r["TTL_DISB"]), "cash_on_hand": _num(r["COH_COP"]),
                    "individual_itemized": _num(r["INDV_ITEM_CONTB"]), "individual_unitemized": _num(r["INDV_UNITEM_CONTB"]),
                    "pac": _num(r["OTH_CMTE_CONTB"]), "party": _num(r["PTY_CMTE_CONTB"]),
                    "self": _num(r.get("CAND_CNTB")), "loans": _num(r.get("CAND_LOAN")),
                    "transfers": _num(r["TRANF_FROM_OTHER_AUTH_CMTE"]),
                }
                found[m["id"]] = {
                    "source": "fec", "source_name": "Federal Election Commission",
                    "cycle": cycle, "cycle_label": f"{cycle - 1}–{cycle}",
                    "candidate_id": cand_id, "coverage_end": _date(r["CVG_END_DT"]),
                    "committee": {"id": r["CMTE_ID"], "name": r["CMTE_NM"]},
                    "fec_url": f"https://www.fec.gov/data/committee/{r['CMTE_ID']}/?cycle={cycle}",
                    "totals": t,
                    "composition": [
                        {"label": "Small donors (under $200)", "amount": t["individual_unitemized"], "kind": "small"},
                        {"label": "Larger individual donors", "amount": t["individual_itemized"], "kind": "individual"},
                        {"label": "PACs & other committees", "amount": t["pac"], "kind": "pac"},
                        {"label": "Party committees", "amount": t["party"], "kind": "party"},
                        {"label": "Candidate's own money", "amount": t["self"] + t["loans"], "kind": "self"},
                        {"label": "Transfers from own committees", "amount": t["transfers"], "kind": "transfer"},
                    ],
                    "by_size": [{"label": SIZE_ORDER[0], "total": t["individual_unitemized"]},
                                {"label": "$200 and over (itemized)", "total": t["individual_itemized"]}],
                    "top_states": [], "top_employers": [], "home_state_pct": None,
                    "all_cycles": [{"cycle": cycle, "total": receipts}],
                }
    log(f"  FEC totals matched for {len(found)} members")
    if itemized and found:
        _add_itemized(found, members, max(cycles), max_age_s)
    return found


def _add_itemized(found: dict, members: list[dict], cycle: int, max_age_s: float) -> None:
    yy = str(cycle)[2:]
    try:
        path = download(FEC_BULK.format(year=cycle, name=f"indiv{yy}.zip"), f"indiv{yy}.zip", max_age_s)
    except Exception as e:  # noqa: BLE001
        log(f"  ! FEC itemized file: {e}")
        return
    by_cmte = {v["committee"]["id"]: k for k, v in found.items() if v["cycle"] == cycle}
    home = {m["id"]: m["state"] for m in members}
    agg = {k: {"states": Counter(), "emp": Counter(), "emp_n": Counter(), "sizes": Counter()} for k in by_cmte.values()}
    log(f"  streaming itemized gifts for {len(by_cmte)} committees (this takes a while)…")
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.endswith(".txt"))
        with z.open(name) as raw:
            for line in io.TextIOWrapper(raw, encoding="latin-1"):
                cmte = line[:9]
                if cmte not in by_cmte:
                    continue
                f = line.rstrip("\n").split("|")
                # 0 CMTE_ID 5 TRANSACTION_TP 6 ENTITY_TP 9 STATE 11 EMPLOYER 14 AMT 18 MEMO_CD
                if len(f) < 19 or f[18] == "X" or f[6] != "IND" or f[5] not in ("15", "15E", "22Y"):
                    continue
                amt = _num(f[14])
                a = agg[by_cmte[cmte]]
                if f[9]:
                    a["states"][f[9]] += amt
                emp = f[11].strip().upper()
                if emp not in NON_EMPLOYERS and not emp.startswith(("RETI", "SELF")):
                    a["emp"][emp] += amt
                    a["emp_n"][emp] += 1
                if amt > 0:
                    a["sizes"][_size(amt)] += amt
    for mid, a in agg.items():
        rec = found[mid]
        total_states = sum(a["states"].values()) or None
        rec["top_states"] = [{"state": s, "total": round(v, 2)} for s, v in a["states"].most_common(10)]
        rec["home_state_pct"] = round(100 * a["states"].get(home[mid], 0) / total_states, 1) if total_states else None
        rec["top_employers"] = [{"employer": e, "total": round(v, 2), "count": a["emp_n"][e]} for e, v in a["emp"].most_common(20)]
        rec["by_size"] = [rec["by_size"][0]] + [{"label": s, "total": round(a["sizes"][s], 2)} for s in SIZE_ORDER[1:] if a["sizes"][s]]
        rec["size_note"] = "Itemized sizes are per gift, not per donor."


# ---------------------------------------------------------------- fact-checks (public ClaimReview feed)
# U.S. fact-checkers, identified by the article's domain (the feed's publisher field is sometimes wrong).
FACTCHECKERS = {
    "politifact.com": "PolitiFact", "factcheck.org": "FactCheck.org", "washingtonpost.com": "Washington Post Fact Checker",
    "apnews.com": "AP Fact Check", "reuters.com": "Reuters Fact Check", "usatoday.com": "USA Today",
    "leadstories.com": "Lead Stories", "checkyourfact.com": "Check Your Fact", "snopes.com": "Snopes",
    "factcheck.afp.com": "AFP Fact Check", "thedispatch.com": "The Dispatch", "science.feedback.org": "Science Feedback",
    "factchequeado.com": "Factchequeado", "verifythis.com": "VERIFY", "coloradosun.com": "The Colorado Sun",
}


def _publisher(url: str) -> str | None:
    host = re.sub(r"^https?://(www\.)?", "", url or "").split("/")[0].lower()
    return next((name for dom, name in FACTCHECKERS.items() if host == dom or host.endswith("." + dom)), None)
TITLE_RE = re.compile(r"\b(u\.?s\.?|rep\.?|sen\.?|senator|representative|congress(wo)?man|gov\.?|governor|speaker|"
                      r"house|minority|majority|leader|state|mayor|attorney general|secretary of state|former)\b", re.I)
NICKS = {"mike": "michael", "bob": "robert", "bill": "william", "jim": "james", "joe": "joseph", "tom": "thomas",
         "chuck": "charles", "dick": "richard", "rick": "richard", "ted": "edward", "dan": "daniel", "dave": "david",
         "chris": "christopher", "matt": "matthew", "liz": "elizabeth", "pat": "patrick", "tony": "anthony",
         "phil": "philip", "steve": "stephen", "ron": "ronald", "jeff": "jeffrey", "nancy": "nancy", "bernie": "bernard"}


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _name_tokens(s: str) -> list[str]:
    s = re.sub(r"\(.*?\)|,?\s*[DRI]-[A-Z.]+\.?", " ", s or "")      # "(D-N.Y.)", ", R-La."
    s = TITLE_RE.sub(" ", _ascii(s))
    return re.findall(r"[a-z]+(?:-[a-z]+)?", s)


def rating_bucket_default(text: str) -> str:
    from build import rating_bucket  # single source of truth for rating buckets
    return rating_bucket(text)


def load_factcheck_feed(max_age_s: float) -> dict[str, list[dict]]:
    """Returns {last-name: [fact-check, ...]} for US fact-checkers in the public feed."""
    path = download(CLAIMREVIEW_FEED, "claimreview.json", max_age_s)
    data = json.loads(path.read_text(encoding="utf-8"))
    by_last: dict[str, list[dict]] = defaultdict(list)
    for el in data.get("dataFeedElement", []):
        for it in el.get("item") or []:
            pub_name = _publisher(it.get("url"))
            if not pub_name:
                continue
            reviewed = it.get("itemReviewed") or {}
            claimant = reviewed.get("author") or {}
            claimant = claimant[0] if isinstance(claimant, list) and claimant else claimant
            claimant_name = claimant.get("name") if isinstance(claimant, dict) else str(claimant)
            toks = _name_tokens(claimant_name)
            if len(toks) < 2 or not it.get("url"):
                continue
            rating = (it.get("reviewRating") or {}).get("alternateName") or ""
            by_last[toks[-1]].append({
                "claim": (it.get("claimReviewed") or "").strip(),
                "claimant": claimant_name,
                "tokens": toks,
                "date": (reviewed.get("datePublished") or it.get("datePublished") or "")[:10],
                "rating": rating,
                "explanation": (it.get("reviewRating") or {}).get("ratingExplanation"),
                "publisher": pub_name,
                "url": it["url"],
                "origin": "claimreview-feed",
            })
    return by_last


def match_factchecks(feed: dict[str, list[dict]], m: dict) -> list[dict]:
    last = _name_tokens(m["last"])
    if not last:
        return []
    firsts = {t for n in (m.get("first", ""), m["name"].split()[0]) for t in _name_tokens(n)[:1]}
    firsts |= {NICKS[f] for f in list(firsts) if f in NICKS} | {k for k, v in NICKS.items() if v in firsts}
    out, seen = [], set()
    for x in sorted(feed.get(last[-1], []), key=lambda x: x["url"]):
        if x["url"] in seen or not firsts & set(x["tokens"][:-1]):
            continue
        seen.add(x["url"])
        item = {k: v for k, v in x.items() if k != "tokens"} | {"bucket": rating_bucket_default(x["rating"])}
        # the same statement checked by several outlets counts once; the other checks are listed under it
        twin = next((o for o in out if o["date"] == item["date"] and _similar(o["claim"], item["claim"])), None)
        if twin:
            twin.setdefault("also_checked_by", []).append(
                {k: item[k] for k in ("publisher", "rating", "bucket", "url")})
        else:
            out.append(item)
    return out


def _similar(a: str, b: str) -> bool:
    wa, wb = set(re.findall(r"[a-z0-9$]+", _ascii(a))), set(re.findall(r"[a-z0-9$]+", _ascii(b)))
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.6
