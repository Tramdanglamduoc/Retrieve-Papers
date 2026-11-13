"""Dry run for _inbox: what is in it and which paper each file would be filed under.
Read-only - it moves nothing. Matches the same way fill_fulltext.py does.
Needs: pip install pypdf pandas

python check_inbox.py "D:\\papers\\my_project"
"""
import importlib.util
import re
import sys
import unicodedata
from pathlib import Path

import pandas as pd

if len(sys.argv) < 2:
    sys.exit('usage: python check_inbox.py "<project folder>"   (the one holding _state)')
if importlib.util.find_spec("pypdf") is None:
    sys.exit("pip install pypdf  - without it no pdf can be opened or matched by its text")
if importlib.util.find_spec("cryptography") is None:
    print('!! cryptography is missing: AES-encrypted pdfs show as UNREADABLE  '
          '(pip install "pypdf[crypto]")\n')

root = Path(sys.argv[1])
ledger = root / "_state" / "papers.csv"
if not ledger.exists():
    sys.exit(f"no ledger at {ledger} - is this the project folder (the one holding _state)?")
papers = pd.read_csv(ledger, dtype=str).fillna("")
squash = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())

plain = {d.lower(): k for d, k in zip(papers.doi, papers.key) if d}
by_doi = {squash(d): k for d, k in plain.items()}
by_pmid = {p: k for p, k in zip(papers.pmid, papers.key) if p}
by_pmc = {c.upper(): k for c, k in zip(papers.pmcid, papers.key) if c}
by_tail = {}
for d, k in plain.items():
    tail = squash(d.split("/", 1)[-1])
    if len(tail) >= 6:
        by_tail[tail] = None if tail in by_tail else k
title = dict(zip(papers.key, papers.title))
folder_of = {k: f.replace("\\", "/") for k, f in zip(papers.key, papers.paper_folder)}
REFERENCES = re.compile(r"\b(references|bibliography|literature cited)\b")


def clean(text):
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", re.sub(r"-\s*\n\s*", "-", text))


def read(path):
    """(pages, metadata text, first two pages' text)."""
    if path.suffix.lower() != ".pdf":
        return "-", "", ""
    try:
        from pypdf import PdfReader
        r = PdfReader(str(path))
        meta = " ".join(str(v) for v in (r.metadata or {}).values())
        body = "\n".join((p.extract_text() or "") for p in r.pages[:2])
        return len(r.pages), clean(meta), clean(body)
    except Exception:
        return None, "", ""


def doi_in_text(text):
    hits = [(m.start(), k) for d, k in plain.items()
            if d in text and (m := re.search(re.escape(d) + r"(?![0-9a-z\-_/(])", text))]
    return min(hits)[1] if hits else None


def title_head(text):
    head = squash(text)[:50]
    return head if len(head) >= 25 else ""


heads = {k: h for k, t in title.items() if (h := title_head(t))}
PMID_NAME = re.compile(r"(?i)^(?:pmid)?[\s_-]*(\d{5,9})$")


def match(path, meta, body):
    stem = re.sub(r"\s*\(\d+\)$", "", path.stem)
    name = squash(stem)
    if name in by_doi:
        return by_doi[name], "file name = doi"
    if by_tail.get(name):
        return by_tail[name], "file name = doi tail"
    for pmc in re.findall(r"PMC\d+", stem.upper()):
        if pmc in by_pmc:
            return by_pmc[pmc], "file name has PMCID"
    # only a name that IS the PMID: "nihms-2017454" is not PMID 2017454
    m = PMID_NAME.match(stem)
    if m and m.group(1) in by_pmid:
        return by_pmid[m.group(1)], "file name = PMID"
    key = doi_in_text(meta)
    if key:
        return key, "doi in pdf metadata"
    # the paper's own title must be printed before "references"; a pdf that
    # only cites a ledger paper has its title in the reference list
    front = squash(REFERENCES.split(body, maxsplit=1)[0])
    key = doi_in_text(body)
    if key and heads.get(key) and heads[key] in front:
        return key, "doi + title in the pdf"
    hits = {k for k, h in heads.items() if h in squash(meta) + front}
    if len(hits) == 1:
        return hits.pop(), "title in the pdf"
    if hits:
        return None, f"title fits {len(hits)} papers"
    return None, "doi of a cited paper?" if key else "no match"


inbox = root / "_inbox"
files = sorted(p for p in inbox.iterdir() if p.suffix.lower() in (".pdf", ".xml")) \
    if inbox.is_dir() else []
print(f"{len(files)} pdf/xml in {inbox}\n")
seen, unmatched = {}, 0
for f in files:
    pages, meta, body = read(f)
    key, how = match(f, meta, body)
    flag = ""
    if pages is None:
        flag = "UNREADABLE"
    elif pages == 1:
        flag = "1 PAGE - held back unless --allow-short-pdf"
    # what fill_fulltext.py would refuse, said here rather than discovered there
    refused = ""
    if key and not folder_of.get(key):
        refused = "PAPER HAS NO FOLDER - will stay in _inbox"
    elif key:
        name = "fulltext.xml" if f.suffix.lower() == ".xml" else "fulltext.pdf"
        if (root / folder_of[key] / name).is_file():
            refused = f"ALREADY HAS {name} - will stay in _inbox"
    flag = " | ".join(x for x in (flag, refused) if x)
    if key:
        seen.setdefault(key, []).append(f.name)
    else:
        unmatched += 1
    print(f"{f.name[:48]:48} pages={pages!s:>4}  {how:26} -> {(title.get(key) or '-')[:50]}  {flag}")
print(f"\nmatched {len(seen)} papers | unmatched {unmatched}")
for k, v in seen.items():
    if len(v) > 1:
        print(f"DUPLICATE for {title.get(k, k)[:60]}: {v}")
