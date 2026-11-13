r"""Arrange files after a hand download, and audit the folder structure.

Read-only unless you add --apply. Needs: pip install pandas pypdf

1) SORT: take the pdfs from any folder (Downloads, an EndNote .Data\PDF folder...)
   and put them into <root>\_inbox\ with a name the filler recognises
   (the DOI with "/" as "_", else <PMID>.pdf).

   python organize_inbox.py "D:\papers\my_project" --from "C:\Users\you\Downloads"
   python organize_inbox.py "D:\papers\my_project" --from "C:\...\MyLib.Data\PDF" --recursive --apply

   Without --apply it only prints the plan. --apply COPIES (your originals stay);
   add --move to move them instead. Then run:
   python fill_fulltext.py <yaml> --inbox-only

2) AUDIT: compare the ledger with what is really on disk.

   python organize_inbox.py "D:\papers\my_project" --audit

   Writes structure_audit.csv next to where you run it. Never changes the project.
"""
import argparse
import importlib.util
import re
import shutil
import sys
import unicodedata
from pathlib import Path

import pandas as pd

norm = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())
REFERENCES = re.compile(r"\b(references|bibliography|literature cited)\b")


def clean(text):
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", re.sub(r"-\s*\n\s*", "-", text))


def load_ledger(root):
    path = root / "_state" / "papers.csv"
    if not path.exists():
        sys.exit(f"no ledger at {path} - is this the project folder (the one holding _state)?")
    return pd.read_csv(path, dtype=str).fillna("")


def pdf_info(path):
    """(pages or None, metadata text, first two pages' text) - the same text
    fill_fulltext.py and check_inbox.py read."""
    try:
        from pypdf import PdfReader
        r = PdfReader(str(path))
        meta = " ".join(str(v) for v in (r.metadata or {}).values())
        body = "\n".join((p.extract_text() or "") for p in r.pages[:2])
        return len(r.pages), clean(meta), clean(body)
    except Exception:
        return None, "", ""


def build_index(papers):
    idx = {"doi": {}, "pmid": {}, "pmc": {}, "tail": {},
           "title": dict(zip(papers["key"], papers["title"]))}
    idx["heads"] = {k: h for k, t in idx["title"].items() if (h := title_head(t))}
    for _, r in papers.iterrows():
        if r["doi"]:
            idx["doi"][r["doi"].lower()] = r["key"]
            # publishers name the file after the part behind the slash
            tail = norm(r["doi"].split("/", 1)[-1])
            if len(tail) >= 6:
                idx["tail"][tail] = None if tail in idx["tail"] else r["key"]
        if r["pmid"]:
            idx["pmid"][r["pmid"]] = r["key"]
        if r["pmcid"]:
            idx["pmc"][r["pmcid"].upper()] = r["key"]
    return idx


def doi_in_text(text, idx):
    # a DOI only counts where it ends: eye.2013.1 must not match inside eye.2013.10
    hits = [(m.start(), k) for d, k in idx["doi"].items()
            if d in text and (m := re.search(re.escape(d) + r"(?![0-9a-z\-_/(])", text))]
    return min(hits)[1] if hits else None


PMID_NAME = re.compile(r"(?i)^(?:pmid)?[\s_-]*(\d{5,9})$")


def title_head(title):
    """First 50 letters/digits of a title; under 25 is too common to tell papers apart."""
    head = norm(title)[:50]
    return head if len(head) >= 25 else ""


def match(path, meta, body, idx):
    stem = re.sub(r"\s*\(\d+\)$", "", path.stem)
    for doi, key in idx["doi"].items():
        if norm(doi) == norm(stem):
            return key, "file name = doi"
    if idx["tail"].get(norm(stem)):
        return idx["tail"][norm(stem)], "file name = doi tail"
    for pmc in re.findall(r"PMC\d+", stem.upper()):
        if pmc in idx["pmc"]:
            return idx["pmc"][pmc], "file name has PMCID"
    # only a name that IS the PMID: "nihms-2017454" is not PMID 2017454
    m = PMID_NAME.match(stem)
    if m and m.group(1) in idx["pmid"]:
        return idx["pmid"][m.group(1)], "file name has PMID"
    key = doi_in_text(meta, idx)
    if key:
        return key, "doi in pdf metadata"
    # the paper's own title must be printed before "references": a pdf that
    # only cites a ledger paper has that title in its reference list
    front = norm(REFERENCES.split(body, maxsplit=1)[0])
    key = doi_in_text(body, idx)
    if key and idx["heads"].get(key) and idx["heads"][key] in front:
        return key, "doi + title in the pdf"
    # many pdfs print no DOI (IOVS, JAMA of 2013) or another edition's DOI
    hits = {k for k, h in idx["heads"].items() if h in norm(meta) + front}
    if len(hits) == 1:
        return hits.pop(), "title in the pdf"
    if hits:
        return None, f"title fits {len(hits)} papers"
    return None, "doi of a cited paper?" if key else "no match"


def inbox_name(row):
    """A name the filler recognises and Windows accepts, or None when the
    paper has no id the filler can match by name. Old Wiley DOIs carry
    : < > ; - the filler ignores every non-alphanumeric character anyway."""
    stem = row["doi"] or row["pmid"] or row["pmcid"]
    return re.sub(r'[\\/:*?"<>|]', "_", stem) + ".pdf" if stem else None


# a file named after the paper beats one recognised only by its text: a
# supplement can carry the article's DOI and be longer than the article
TRUST = {"file name = doi": 3, "file name = doi tail": 3, "file name has PMCID": 3,
         "file name has PMID": 3, "doi in pdf metadata": 2, "doi + title in the pdf": 1}


def sort_downloads(root, source, recursive, apply, move, allow_short=False):
    papers = load_ledger(root)
    idx = build_index(papers)
    rows = papers.set_index("key").to_dict("index")
    inbox = root / "_inbox"
    files = sorted(source.rglob("*.pdf") if recursive else source.glob("*.pdf"))
    print(f"{len(files)} pdf in {source}" + (" (recursive)" if recursive else ""))
    best, skipped = {}, []
    rank = lambda pages, how: (TRUST.get(how, 0), pages)
    for f in files:
        pages, meta, body = pdf_info(f)
        key, how = match(f, meta, body, idx)
        if not key:
            skipped.append((f, f"no match in the ledger ({how})"))
        elif not inbox_name(rows[key]):
            skipped.append((f, "its paper has no DOI/PMID/PMCID - file it by hand"))
        elif pages is None:
            skipped.append((f, "unreadable pdf (encrypted? pip install \"pypdf[crypto]\")"))
        elif pages <= 1 and not allow_short:
            skipped.append((f, f"1 page only - open it; if complete, rerun with "
                               f"--allow-short-pdf; matched {rows[key]['title'][:40]}"))
        elif key not in best or rank(pages, how) > rank(best[key][1], best[key][2]):
            if key in best:
                skipped.append((best[key][0], "duplicate - a better match for the same paper wins"))
            best[key] = (f, pages, how)
        else:
            skipped.append((f, "duplicate - a better match for the same paper wins"))
    print(f"\nPLAN ({len(best)} paper(s)):")
    for key, (f, pages, how) in sorted(best.items()):
        r = rows[key]
        name = inbox_name(r)
        have = " (paper already has fulltext.pdf - will be left in _inbox by the filler)" if r["pdf_path"] else ""
        print(f"  {f.name[:44]:44} {pages:>3}p  {how:26} -> _inbox\\{name}{have}")
        if apply:
            inbox.mkdir(parents=True, exist_ok=True)
            target = inbox / name
            if target.exists():
                print(f"      skipped: {name} already in _inbox")
                continue
            (shutil.move if move else shutil.copy2)(str(f), str(target))
    if skipped:
        print(f"\nNOT USED ({len(skipped)}):")
        for f, why in skipped:
            print(f"  {f.name[:44]:44} {why}")
    if not apply:
        print("\nDry run - nothing copied. Add --apply to " + ("move" if move else "copy") + " the files above.")
    else:
        print("\nDone. Next: python fill_fulltext.py <yaml> --inbox-only"
              + (" --allow-short-pdf" if allow_short else ""))


def audit(root):
    papers = load_ledger(root)
    problems, counts = [], {"both": 0, "xml_only": 0, "pdf_only": 0, "none": 0}
    ledger_folders = set()
    for _, r in papers.iterrows():
        folder = root / r["paper_folder"].replace("\\", "/") if r["paper_folder"] else None
        if folder is None:
            problems.append((r["key"], "", "ledger row has no paper_folder"))
            continue
        ledger_folders.add(folder.resolve())
        xml, pdf = folder / "fulltext.xml", folder / "fulltext.pdf"
        has = (xml.is_file(), pdf.is_file())
        counts["both" if all(has) else "xml_only" if has[0] else "pdf_only" if has[1] else "none"] += 1
        if not folder.is_dir():
            problems.append((r["key"], str(folder), "folder missing on disk"))
        if r["fulltext_path"] and not has[0]:
            problems.append((r["key"], str(xml), "ledger lists fulltext.xml but it is not on disk"))
        if not r["fulltext_path"] and has[0]:
            problems.append((r["key"], str(xml), "fulltext.xml on disk but not in the ledger"))
        if r["pdf_path"] and not has[1]:
            problems.append((r["key"], str(pdf), "ledger lists fulltext.pdf but it is not on disk"))
        if not r["pdf_path"] and has[1]:
            problems.append((r["key"], str(pdf), "fulltext.pdf on disk but not in the ledger"))
        if has[1]:
            pages = pdf_info(pdf)[0]
            if pages == 1:
                problems.append((r["key"], str(pdf), "fulltext.pdf has only 1 page (first page only?)"))
            elif pages is None:
                problems.append((r["key"], str(pdf), "fulltext.pdf unreadable"))
        if not (folder / "metadata.json").is_file() and folder.is_dir():
            problems.append((r["key"], str(folder), "metadata.json missing"))
    papers_dir = root / "papers"
    orphans = []
    if papers_dir.is_dir():
        for year in sorted(p for p in papers_dir.iterdir() if p.is_dir()):
            for d in sorted(p for p in year.iterdir() if p.is_dir()):
                if d.resolve() not in ledger_folders:
                    orphans.append(d)
                    problems.append(("", str(d), "folder on disk is not in the ledger"))
    inbox = root / "_inbox"
    waiting = sorted(p for p in inbox.iterdir() if p.is_file()) if inbox.is_dir() else []
    print(f"papers in ledger      {len(papers)}")
    print(f"  xml + pdf           {counts['both']}")
    print(f"  xml only            {counts['xml_only']}")
    print(f"  pdf only            {counts['pdf_only']}")
    print(f"  nothing             {counts['none']}   (abstract only)")
    print(f"folders not in ledger {len(orphans)}")
    print(f"files waiting in _inbox {len(waiting)}")
    print(f"problems found        {len(problems)}")
    for key, where, why in problems[:40]:
        print(f"  - {why}: {where or key}")
    if len(problems) > 40:
        print(f"  ... and {len(problems) - 40} more (see structure_audit.csv)")
    pd.DataFrame(problems, columns=["paper_key", "path", "problem"]).to_csv(
        "structure_audit.csv", index=False, encoding="utf-8-sig")
    print("\nstructure_audit.csv written here. The project was not changed.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", help="the project folder (the one that holds _state, papers, _inbox)")
    ap.add_argument("--from", dest="source", help="folder with the downloaded pdfs")
    ap.add_argument("--recursive", action="store_true", help="also look inside sub-folders")
    ap.add_argument("--apply", action="store_true", help="really copy the files into _inbox")
    ap.add_argument("--move", action="store_true", help="with --apply: move instead of copy")
    ap.add_argument("--audit", action="store_true", help="compare the ledger with the disk")
    ap.add_argument("--allow-short-pdf", action="store_true",
                    help="also take 1-page pdfs (then file them with the same flag)")
    args = ap.parse_args()
    if importlib.util.find_spec("pypdf") is None:
        sys.exit("pip install pypdf  - without it no pdf can be opened, so every "
                 "file would look unreadable")
    if importlib.util.find_spec("cryptography") is None:
        print('!! cryptography is missing: AES-encrypted pdfs will look unreadable  '
              '(pip install "pypdf[crypto]")\n')
    root = Path(args.root)
    if args.audit:
        audit(root)
    elif args.source:
        sort_downloads(root, Path(args.source), args.recursive, args.apply, args.move,
                       args.allow_short_pdf)
    else:
        ap.error("give --from <folder> to sort downloads, or --audit")


if __name__ == "__main__":
    main()
