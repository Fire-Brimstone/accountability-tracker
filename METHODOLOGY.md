# Methodology

This project only publishes what can be traced to a primary or independent source.
Every number on the site links to where it came from. The same rules apply to every
official, regardless of party.

## 1. Voting record (automated)

- **Source:** [Voteview](https://voteview.com) roll-call data (UCLA), which compiles the
  official House Clerk and Senate roll calls. Free, no key, covers both chambers.
- **Positions:** Voteview cast codes 1–3 = Yea, 4–6 = Nay, 7 = Present, 8–9 = Not voting.
- **Missed votes %:** roll calls where the member was eligible (cast code ≠ 0) but did not vote
  (codes 7–9 count as "Present/Not voting"; only 8–9 count as *missed*).
- **Votes with party %:** share of the member's Yea/Nay votes that match the majority position of
  their own party on that roll call. Independents are compared with the party they caucus with
  only if `caucus` is set in the legislators file; otherwise this stat is omitted.

## 2. Legislation written or championed (automated)

- **Source:** [GovInfo BILLSTATUS bulk data](https://www.govinfo.gov/bulkdata/BILLSTATUS) (Government Publishing
  Office, the same records Congress.gov displays). Covers the 118th and 119th Congresses.
- **Sponsored** = the member formally introduced it ("wrote" in the legislative sense).
- **Cosponsored** = the member formally signed on ("championed").
- Congressional Budget Office cost estimates are attached automatically when the bill record lists one.
- Withdrawn cosponsorships are not counted.

## 3. What they said about each bill (curated, sourced)

Statements live in `curated/members/<bioguide>.json`. Each statement **must** have:
a verbatim quote, the date, the bill it concerns, and a URL to the original (floor speech in the
Congressional Record, press release on an official site, video, or a news outlet quoting them).
The build rejects statements without a `source_url`.

## 4. Actual effects of bills (curated, sourced)

Effects live in `curated/bills/<bill-id>.json` (e.g. `hr1-119.json`) and are shared by every
member who voted on, sponsored, or cosponsored that bill. Acceptable sources, in order of
preference:

1. Non-partisan government analysis: CBO, GAO, CRS, JCT, agency Inspectors General, BLS/Census data.
2. Peer-reviewed research.
3. Analysis from established research organizations — **must be labelled** with the
   organization's name so readers can judge it (e.g. "Tax Policy Center", "AEI").

Projections (e.g. CBO scores before enactment) are labelled `"kind": "projection"`; measured
outcomes after enactment are labelled `"kind": "measured"`. The site shows these differently.

## 5. Where their money comes from (automated)

- **Source:** [FEC bulk data](https://www.fec.gov/data/browse-data/?tab=bulk-data) (Federal Election Commission):
  committee summaries for totals, and the candidate–committee linkage file to find each member's principal
  campaign committee (the summary leaves the candidate blank for senators not on the current ballot).
- Shows the most recent two-year cycle with any receipts; totals for the previous cycle are listed for comparison.
- Shown: total raised, individual (small vs. large) vs. PAC vs. self-funding, donations by size,
  top donor states, and top donor employers. States, employers and gift sizes come from the FEC's itemized
  individual-contributions file (about 2.5 GB), processed only when the pipeline is run with `--fec-itemized`.
  Itemized gift sizes are per gift, not per donor; memo entries are excluded to avoid double counting.
- Caveat shown on the site: FEC employer data is self-reported by donors; employer totals are
  contributions from *employees* of that organization, not from the organization itself
  (corporations cannot give directly to federal candidates).
- Not yet covered: Super PAC / outside spending for or against the member, lobbying data.
  These are on the roadmap (FEC Schedule E, Senate LDA API).

## 6. False claims count ("lie count")

The site labels this **"Rated false by fact-checkers"** rather than "lies". Calling something a
lie asserts the speaker *knew* it was untrue, which almost never can be proven and exposes the
project to defamation claims. A transparent, repeatable count of statements independent
fact-checkers rated false is both more defensible and more useful.

**What counts toward the number**

- The claim was made by the official themselves (not their campaign, staff, or a surrogate).
- It was rated by a fact-checker that is a verified signatory of the
  [IFCN Code of Principles](https://ifcncodeofprinciples.poynter.org/) (e.g. PolitiFact,
  FactCheck.org, Washington Post Fact Checker, AP Fact Check, Reuters Fact Check, Lead Stories).
- The rating is in the *false* bucket:

| Bucket      | Ratings mapped to it (examples) | Counted? |
|-------------|---------------------------------|----------|
| `false`     | False, Pants on Fire, Four Pinocchios, Three Pinocchios, Incorrect, Fabricated | **Yes** |
| `misleading`| Mostly False, Half True, Misleading, Two Pinocchios, Missing context, Distorts the facts | Shown, not counted |
| `true`      | True, Mostly True, Correct, Geppetto Checkmark | Shown, not counted |
| `other`     | anything that doesn't map cleanly | Shown, not counted |

**Each statement counts once.** When several fact-checkers rate the same statement (same date, mostly the
same words), it is counted once using the first rating, and the other ratings are listed under it.

**Every** fact-check the pipeline finds for an official is listed — including ones rated true —
so the count can't be cherry-picked. Each entry links to the fact-checker's article.

**Known bias in this metric (displayed on the site):** fact-checkers choose which claims to check,
and they check high-profile officials far more often. A low count can mean "rarely checked", not
"always truthful". The site therefore also shows *how many* of their statements were checked.

**Sources:** the public [ClaimReview feed](https://storage.googleapis.com/datacommons-feeds/claimreview/latest/data.json)
published daily by Data Commons (the same fact-check markup Google's Fact Check Explorer shows), plus manually
added entries in `curated/members/<bioguide>.json`. Automated matches are kept only when the fact-checker is on
the site's list of U.S. fact-checkers and the person the claim is attributed to matches the official's first and
last name (nicknames allowed). **Known limits:** the feed contains only fact-checks whose publishers added
ClaimReview markup, so it is incomplete (PolitiFact's own site lists more), and two people with the same
name (e.g. two "Mike Johnson"s) cannot always be told apart. Report wrong matches as corrections.

## 7. State and local officials (Colorado)

| What | Source | Notes |
|---|---|---|
| Who holds office | [openstates/people](https://github.com/openstates/people) (Plural/Open States), plus `curated/people/co.json` for anyone missing (e.g. the State Treasurer) | Legislators, statewide executives, and mayors of the largest cities |
| Floor votes, sponsored bills | Not included yet — the legislature's site blocks automated access and Open States requires an account | Supported if an Open States key is ever provided |
| Campaign money | [Colorado TRACER](https://tracer.sos.colorado.gov/) bulk contribution files (Secretary of State) | See below |

**Matching TRACER filings to officials.** TRACER files are keyed by the candidate's legal name, not an ID, so
the pipeline matches by last name plus first name, nickname (Tony → Thomas/Anthony, Bob → Robert, …) or a
middle name used as a first name (Timothy *Jarvis* Caldwell). Every money section lists the exact names and
committee IDs it matched, so a wrong match is visible and can be reported. Superseded (amended) records are
dropped; returned contributions are subtracted.

**Which cycle is shown.** The most recent two-year cycle with at least $5,000 raised. Otherwise a
term-limited official's few stray gifts would misrepresent who funds them. Totals for the official's other
cycles are listed below the chart. Money from every candidate committee the person controls is combined,
including committees for a different office (e.g. a legislator running for governor).

**Not covered yet:** municipal campaign finance (cities like Denver run their own filing systems), outside
spending by independent expenditure committees, and county officials.

## Corrections

Anything wrong? Open an issue with the source. Corrections are logged in the git history, which
is itself the public audit trail for every change to the data.
