# PubMed literature crawler + full-text filler

A small set of Python scripts that turn a keyword plan in a YAML file into a
PubMed search. They download every paper's metadata, collect the full text
(XML and/or PDF) from legal sources, and keep a ledger of what you have, what
is missing and why.

It was built for a knowledge-framework project on **age-related macular
degeneration (AMD)**, and the example config is that project. It works for any
disease or topic: you change the YAML, not the code.

> 🇻🇳 Tiếng Việt: [README.vi.md](README.vi.md)

---

## What it does

```
config.yaml ──► pubmed_crawler_v6_1.py ──► PubMed search (keywords + MeSH, per year)
                         │                    metadata for every hit
                         │                    full text from PMC / Unpaywall
                         ▼
                 output/_state/papers.csv  (the ledger: one row per paper)
                         │
                         ▼
               fill_fulltext.py  ──► fills the gaps: PMC Cloud, more Unpaywall copies,
                         │             publisher TDM APIs, repositories
                         ▼
          papers that still need a human ──► --open (your browser) ──► _inbox/ ──► filed
```

- **Structured search.** A *branch* is a research question. A *leaf* is a group
  of keywords inside it. Every query is `disease anchor AND (leaf terms OR leaf MeSH)`,
  run year by year, so a busy year is never cut short.
- **One copy per paper.** A paper found by five leaves is stored once and
  tagged five times (`paper_branches.csv`).
- **Honest statuses.** Every paper ends with a status and the next action
  (`done`, `needs_browser`, `needs_tmu`, `retry_auto` ...). Nothing is silently dropped.
- **Re-runnable.** Settled papers are skipped. Half-written files are cleaned up
  after a crash, and the previous ledger is kept as an undo copy.
- **Excel overview.** `_index/eye_disease_papers.xlsx` has sheets for saved /
  not saved / needs institutional access, coverage per branch and leaf,
  duplicates, and the run log.

## Files

| File | What it is for |
|---|---|
| `pubmed_crawler_v6_1.py` | Main crawler: search, metadata, first full-text pass |
| `fill_fulltext.py` | Goes back over the ledger and tries more routes for missing XML/PDF |
| `organize_inbox.py` | Renames PDFs you downloaded by hand and copies them into `_inbox/`; `--audit` checks the ledger against the disk |
| `check_inbox.py` | Dry run: which paper each file in `_inbox/` would be filed under |
| `check_elsevier_pdf.py` | Tests what Elsevier's API returns for one DOI from your current network |
| `config.example.yaml` | Worked example (AMD, one branch, two leaves, year 2013) |

`fill_fulltext.py` imports the crawler, so keep all the scripts in one folder.

---

## ⚠️ Read before you use it

- **Only legal sources.** The scripts use PubMed/PMC, Europe PMC, the PMC open
  dataset on AWS, Unpaywall, Crossref and the publishers' official text-and-data-mining
  (TDM) APIs. They do not use shadow libraries and do not get around paywalls.
- **Institutional access is yours, not the script's.** Elsevier and Wiley TDM,
  and the *publisher site* route, only work on a network your institution has a
  licence for (campus or VPN). Check your library's rules on automated downloading
  first. Many publishers forbid bulk downloads from their websites, and too many
  requests can get your **whole institution's IP blocked**. If you are not sure,
  run `fill_fulltext.py --no-publisher`.
- **Do not share the papers.** `output/` holds copyrighted PDFs. Keep it out of
  git (the `.gitignore` does this) and out of public drives.
- **Rate limits are built in.** NCBI 3 req/s without a key and 10 with one.
  Publisher sites get 1 req/s. Please don't raise them.

---

## Install

Python 3.9 or newer.

```bash
git clone <this repo>
cd <this repo>
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Set up your keys (environment variables, not the YAML)

| Variable | Needed? | Where to get it |
|---|---|---|
| `NCBI_API_KEY` | Recommended (3 → 10 req/s) | NCBI account → Account settings → API Key Management |
| `ELSEVIER_API_KEY` | Optional | dev.elsevier.com (works only from an entitled network) |
| `ELSEVIER_INSTTOKEN` | Optional | Only if your library gives you one |
| `WILEY_TDM_TOKEN` | Optional | Wiley TDM click-through token (via Crossref / ORCID) |

```bash
# Windows (then open a NEW terminal)
setx NCBI_API_KEY "your-key"
# macOS / Linux (add to ~/.bashrc or ~/.zshrc)
export NCBI_API_KEY="your-key"
```

The scripts read these automatically. They never print a key, and keys never
go into the log or the Excel file.

---

## Quick start

```bash
cp config.example.yaml config.yaml      # then put your email in contact.email
python pubmed_crawler_v6_1.py config.yaml --check      # 1. prints the queries, no network
python pubmed_crawler_v6_1.py config.yaml --test-api   # 2. do the APIs answer?
python pubmed_crawler_v6_1.py config.yaml              # 3. the real run
python fill_fulltext.py config.yaml --limit 20         # 4. fill gaps, small trial first
python fill_fulltext.py config.yaml                    # 5. fill all gaps
```

**Tip:** paste a query printed by `--check` into
[pubmed.ncbi.nlm.nih.gov](https://pubmed.ncbi.nlm.nih.gov). The count there should
match the count in the run report.

The example config has a known answer: the 2013 window contains Ferris et al.
2013, *Clinical classification of age-related macular degeneration*
(PMID 23332590). `--test-api` checks that the search finds it.

---

## The by-hand loop (papers the script cannot get)

Some papers are free but the site wants a human ("are you a robot?"). Others
need your institution. For those:

```bash
python fill_fulltext.py config.yaml --open 10      # opens the next 10 in YOUR browser
# save each PDF into output/_inbox/  (any file name)
python check_inbox.py output                       # optional: see what will match
python fill_fulltext.py config.yaml --inbox-only   # files them into the right folders
```

PDFs you already have somewhere else (Downloads, an EndNote library):

```bash
python organize_inbox.py output --from "C:\Users\you\Downloads"           # plan only
python organize_inbox.py output --from "C:\Users\you\Downloads" --apply   # copy into _inbox
python fill_fulltext.py config.yaml --inbox-only
```

Files are matched by DOI or PMID in the file name, then by the DOI in the PDF's
metadata, then by the title printed on the first pages. A **1-page PDF is held
back**, because Elsevier sends only page one when your network is not entitled.
If a 1-page PDF really is the whole paper, add `--allow-short-pdf`.

`python organize_inbox.py output --audit` compares the ledger with the disk and
writes `structure_audit.csv`. It does not change anything.

---

## Output layout

```
output/
├── papers/<year>/<PMCID_PMID or noPMC_PMID>/
│       metadata.json   fulltext.xml   fulltext.pdf
├── _state/
│       papers.csv            the ledger, one row per paper
│       paper_branches.csv    one row per paper × branch × leaf × method
│       queries.csv           every query that ran
│       runs.csv              one row per run
│       fulltext_fill_log.csv every route tried, with the HTTP answer
│       run_report_latest.txt
│       previous/             the ledger before the last run (undo)
│       history/              dated reports
├── _index/
│       eye_disease_papers.xlsx
│       branches/<branch>/<leaf>.csv
│       duplicates.csv
├── _inbox/                   drop hand-downloaded files here
└── raw/                      raw PubMed XML per batch
```

### What the `action` column means

| action | meaning | what you do |
|---|---|---|
| `done` | full text on disk | nothing |
| `needs_browser` | free, but the site refuses scripts | `--open`, save to `_inbox/` |
| `needs_tmu` | not open; needs institutional access | on campus/VPN: `--open`, or ask the library |
| `needs_request` | in PMC but closed | request through the library |
| `retry_auto` | a server did not answer | run again |
| `retry_later` | PMC embargo | wait; it is re-checked after 90 days |
| `blocked_network` | host unreachable | check firewall / VPN |
| `needs_code_fix` | metadata could not be parsed | open an issue |

(`needs_tmu` comes from the original project at Taipei Medical University. Read
it as "needs your institution's access".)

---

## Adapt it to your own topic

Only the YAML changes:

1. `disease.anchor`: every way papers name your disease (with and without hyphens, abbreviations).
2. `disease.mesh`: its MeSH descriptors (look them up at meshb.nlm.nih.gov).
3. `branches`: one per research question. Inside each, `leaves` with `terms`
   (title/abstract keywords) and optional `mesh`.
4. `date_range`: keep `split_by_year: true`. PubMed stops paging at 10,000 hits per query.
5. Run `--check` and paste the queries into PubMed before a real run.

Then read the run report. `leaf_coverage` → `only_here` shows what each leaf
adds. In `branch_coverage`, a large `mesh_only` means either your keywords
have a gap or a MeSH term is too broad.

Note: the `--test-api` known-answer check is hard-coded to the AMD paper
(`BECKMAN_PMID` near the top of the crawler). For another topic, change it to
a paper you know must be found.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `reach NCBI` fails | firewall, proxy or VPN, not the code |
| `CAPPED` warning | too many hits in one query; split the years or narrow the terms |
| `PUBMED IGNORED` warning | PubMed silently dropped a quoted phrase; reword it |
| Elsevier PDFs are 1 page | your network is not entitled; check with `check_elsevier_pdf.py <config> <doi>` |
| "is still open in another program" | close the Excel file; the run waits, or rerun and it continues |
| PDFs show as UNREADABLE | `pip install "pypdf[crypto]"` |

## Limitations

- PubMed only (the `source.database` field is a label).
- Keyword hits are a rough signal: PubMed stems words, the tagger does not.
- Automated full text is never 100%. Expect a share of papers to need the library.

## License & citation

MIT (see `LICENSE`). If it helps your work, a mention or a star is appreciated.
Issues and pull requests are welcome.


