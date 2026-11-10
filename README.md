# Public Record Monitor

A transparent accountability monitor for elected officials: every member of the U.S. Congress (House and
Senate), plus Colorado's state legislators, statewide officials, and big-city mayors. For each official it shows:

No API keys, accounts or sign-ups are needed. Everything comes from free public bulk downloads.

| What | Where it comes from |
|---|---|
| Every roll-call vote, missed votes, how often they vote with their party | Voteview (UCLA), built from House Clerk & Senate records |
| Bills they wrote (sponsored) and championed (cosponsored), which became law, CBO cost estimates | GovInfo BILLSTATUS bulk data (Government Publishing Office) |
| Where their campaign money comes from: PACs vs. small vs. large donors (donor states and employers optional) | FEC bulk files |
| Statements rated false by independent fact-checkers ("lie count"), with every fact-check listed | Public ClaimReview feed (the data behind Google's Fact Check Explorer) + contributors |
| What they said about each bill | Contributors, verbatim with a source link (curated) |
| What each bill actually did (CBO/GAO/measured outcomes) | Contributors, with a source link (curated) |
| Colorado officials and their campaign money | openstates/people + Colorado Secretary of State TRACER |

Every number on the site links to its source. **Read [METHODOLOGY.md](METHODOLOGY.md)** —
it defines exactly what is counted and why, and the same rules apply to everyone.

## Quick start

```bash
python pipeline/build.py                  # Congress: ~3 minutes, downloads ~300 MB of bulk files the first time
python pipeline/build_state.py --state co # Colorado: ~1 minute
python serve.py                           # then open http://localhost:8765
```

Requires Python 3.10+ and nothing else (standard library only). Downloads are cached in `.cache/` for a day,
so reruns within a day are quick.

Optional: `python pipeline/build.py --fec-itemized` also computes donor states, employers and gift sizes by
streaming the FEC's individual-contributions file (about 2.5 GB). Best run weekly on a server rather than a laptop.

Other flags: `--skip-votes`, `--skip-bills`, `--skip-fec`, `--skip-factcheck`, `--congresses 117,118,119`,
`--fec-cycles 2026,2024`, `--cache-hours 24`.

## State and local officials (Colorado)

```bash
python pipeline/build_state.py --state co
```

This loads 110 Colorado officials: all 100 legislators, the statewide executives, and mayors of the largest
cities, with campaign money from the Secretary of State's TRACER data and fact-checks from the public feed.

**State legislative votes and bills are not included yet.** The Colorado General Assembly website blocks
automated access, and Open States (the national source) requires an account or API key. The pipeline still
supports Open States if you ever set `OPENSTATES_API_KEY`; otherwise those tabs say the data isn't available.

## Contributing statements, fact-checks, and bill effects

Hand-curated data lives in `curated/` as plain JSON so every change is reviewable in git history:

- `curated/members/<bioguide>.json` — verbatim statements about bills, and fact-checks the feed missed.
  Copy `curated/members/_TEMPLATE.json`.
- `curated/bills/<bill-id>.json` — documented effects of a bill (e.g. `hr1-119.json`).
  Copy `curated/bills/_TEMPLATE.json`. Two real examples are included.

Rules (the build drops entries that break them): every entry needs a `source_url`; quotes are verbatim;
effects are labelled `projection` or `measured`; think-tank sources are named so readers can weigh them.

## Layout

```
pipeline/build.py        federal pipeline (members → votes → bills → money → fact-checks → curated)
pipeline/bulk.py         keyless bulk sources: GovInfo bills, FEC files, ClaimReview fact-check feed
pipeline/build_state.py  state & local pipeline (openstates/people + Colorado TRACER)
pipeline/miniyaml.py     tiny YAML reader for openstates/people files
serve.py                 local web server for the site
curated/              hand-added, sourced data
site/                 static website (index, member profile, methodology)
site/data/            generated JSON (commit it to publish; regenerate with the pipeline)
METHODOLOGY.md        how every number is defined
```

## Publishing

The site is hosted on GitHub Pages. `.github/workflows/publish.yml` rebuilds all data from the public
bulk sources every night (and on every push to `main`), then publishes `site/`. No secrets or API keys
are involved. Generated data (`site/data/`) is not committed; each run starts from the previous run's
data, so if one source is down that night its section keeps the last good version.

To rebuild immediately: Actions tab → "Rebuild and publish" → Run workflow.

## Roadmap

- [x] Nightly GitHub Action to rebuild data and publish
- [ ] Public changelog of what changed in the data each night
- [ ] Outside spending for/against each member (FEC Schedule E) and lobbying contacts (Senate LDA API)
- [ ] Stock trades disclosed under the STOCK Act (House Clerk / Senate eFD periodic transaction reports)
- [ ] Statements pipeline: pull floor speeches from the Congressional Record (GovInfo API) and link them to bills automatically
- [x] Colorado legislators, statewide officials and big-city mayors
- [ ] Colorado legislative votes and bills — deliberately left out for now. Possible keyless route later: the
      monthly public Open States database dump (~11 GB PostgreSQL file, no account), run on a server once a month
- [ ] Campaign money for other states (each state has its own filing system), and Colorado county officials
- [ ] Local officials (city councils, mayors) — no national data source; needs per-city scrapers or contributors
- [ ] Contributor form so non-programmers can submit sourced entries for review
