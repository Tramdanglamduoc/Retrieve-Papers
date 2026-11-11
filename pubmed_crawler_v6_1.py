"""AMD knowledge-framework crawler v6.1.

v6 is the two v5 forks put back together: the per-leaf tagging and bug fixes
of "v5 API fix", plus the query table, MeSH-in-query and the extra full-text
routes of "v5 cau truc".

    _state/runs.csv             one row per run
    _state/papers.csv           one row per paper
    _state/paper_branches.csv   one row per paper x branch x leaf x method
    _state/queries.csv          one row per distinct query; edges point at it
    papers/<year>/<id>/         fulltext.xml, fulltext.pdf, supplementary.zip,
                                metadata.json - stored once per paper
    _index/branches/<b>/<leaf>.csv      one leaf, one row per paper
    _index/branches/<b>/_all_leaves.csv the repeats, written down
    _index/duplicates.csv               every paper carrying >1 tag

A paper may repeat across branches and across leaves; same branch + same leaf
is one row. Set paths.link_into_branches: true for a per-leaf tree of
hardlinks to the stored files.

MeSH descriptors go INTO the query beside the keywords (a leaf's own mesh for
a leaf, the branch mesh for a branch without leaves) and are still used for
tagging afterwards. search.include_mesh: false turns that off.

Full text is tried in order: Europe PMC xml -> NCBI PMC xml -> Unpaywall pdf
-> publisher TDM pdf.

    pip install biopython pandas openpyxl requests pyyaml tqdm
    python pubmed_crawler_v6_1.py config.yaml --test-api   # does the API answer?
    python pubmed_crawler_v6_1.py config.yaml --check      # queries, no network
    python pubmed_crawler_v6_1.py config.yaml              # run
"""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import zipfile
from collections import defaultdict
from datetime import datetime, timezone, date
from pathlib import Path
from xml.etree import ElementTree

import pandas as pd
import requests
import yaml
from Bio import Entrez
from tqdm import tqdm

PARSER_VERSION = 7

# how a tag was earned. "query" and "query_mesh" are exclusive: the query found
# the paper either because a keyword is in the text, or because MeSH caught it.
# "mesh" is the separate post-hoc autotagger.
QUERY_METHODS = ("query", "query_mesh")

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/{what}"
EPMC_PDF = "https://europepmc.org/articles/{pmcid}?pdf=render"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/einfo.fcgi"
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"
ELSEVIER = "https://api.elsevier.com/content/article/doi/{doi}"
WILEY = "https://api.wiley.com/onlinelibrary/tdm/v1/articles/{doi}"
TDM_PREFIXES = {"10.1016": "elsevier", "10.1111": "wiley", "10.1002": "wiley"}
HEADERS = {"User-Agent": "AMD-knowledge-crawler/6"}

# the smoke test's known answer: Ferris et al. 2013, the Beckman paper
BECKMAN_PMID = "23332590"

DOI_PREFIXES = {
    "10.1016": "Elsevier", "10.1007": "Springer", "10.1002": "Wiley",
    "10.1111": "Wiley", "10.1155": "Wiley (Hindawi)", "10.1038": "Nature Portfolio",
    "10.1186": "BMC", "10.1136": "BMJ", "10.1097": "Wolters Kluwer",
    "10.4103": "Wolters Kluwer (Medknow)", "10.1167": "ARVO",
    "10.1080": "Taylor & Francis", "10.3390": "MDPI", "10.3389": "Frontiers",
    "10.1371": "PLOS", "10.2147": "Dove Press", "10.1159": "Karger",
    "10.1089": "Mary Ann Liebert", "10.1021": "ACS", "10.1364": "Optica",
    "10.1093": "Oxford University Press", "10.1017": "Cambridge University Press",
    "10.1177": "SAGE", "10.3928": "Healio", "10.1056": "NEJM",
    "10.1073": "PNAS", "10.1126": "AAAS", "10.1101": "Cold Spring Harbor (preprint)",
    "10.1172": "JCI", "10.7150": "Ivyspring", "10.18240": "IJO Press",
}

ACTIONS = {
    "ok":              ("done", "full text saved"),
    "not_in_pmc":      ("needs_tmu", "no PMC copy and no open copy - TMU or VPN"),
    "pmc_not_oa":      ("needs_request", "in PMC, readable by hand, not retrievable"),
    "embargo":         ("retry_later", "PMC embargo not yet lifted"),
    "network_blocked": ("blocked_network", "host unreachable - firewall or proxy"),
    "http_error":      ("retry_auto", "transient HTTP error"),
    "corrupt_file":    ("retry_auto", "downloaded file failed validation"),
    "metadata_only":   ("done_metadata", "metadata saved, full text not requested"),
    "parse_error":     ("needs_code_fix", "metadata structure unexpected"),
}
RETRY_STATUSES = {"network_blocked", "http_error", "corrupt_file", "embargo"}
# settled by an older version that knew fewer routes; worth one more look
NEW_ROUTE_STATUSES = {"not_in_pmc", "pmc_not_oa"}

PAPER_COLUMNS = [
    "key", "doi", "pmid", "pmcid", "title", "pub_year", "journal", "publisher",
    "doi_url", "pubmed_url", "pmc_url",
    "databases", "n_branches", "branches", "n_leaf_tags", "leaf_tags",
    "in_pmc", "is_open_access",
    "source_fulltext", "source_chain", "match_method",
    "status", "action", "detail", "attempts",
    "first_seen", "first_seen_run", "last_run", "last_attempt", "parser_version",
    "paper_folder", "metadata_path", "fulltext_path",
    "fulltext_bytes", "fulltext_sha256",
    "pdf_path", "pdf_bytes",
    "supplementary_path", "supplementary_files",
]
EDGE_COLUMNS = ["paper_key", "branch", "leaf", "database", "method", "run_id",
                "first_seen", "query_id"]
RUN_COLUMNS = ["run_id", "started", "finished", "database", "config_hash",
               "branches_searched", "branches_todo", "leaves_searched",
               "matched", "retrieved", "open_access", "new", "skipped",
               "fulltext_saved", "pdf_saved", "supplementary_saved",
               "repeat_tags", "failed", "warnings"]

LINK_COLUMN = "doi_url"
MAX_LINKS_PER_SHEET = 60000
ESEARCH_HARD_CAP = 9998          # NCBI will not page an esearch past 10k


# ---------------------------------------------------------------- config

def _int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def load_config(path="config.yaml"):
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    for section in ("contact", "paths", "date_range", "limits", "download",
                    "disease", "branches", "search", "publisher_api"):
        cfg.setdefault(section, {})

    Entrez.email = cfg["contact"].get("email") or ""
    Entrez.api_key = cfg["contact"].get("ncbi_api_key") or None
    Entrez.tool = "AMD-knowledge-crawler"
    if not Entrez.email:
        raise SystemExit("contact.email is required - NCBI rejects anonymous use")
    HEADERS["User-Agent"] = f"AMD-knowledge-crawler/6 (mailto:{Entrez.email})"

    lim = cfg["limits"]
    lim["ncbi_per_second"] = 10 if Entrez.api_key else min(
        _int(lim.get("ncbi_per_second"), 3) or 3, 3)
    lim["epmc_per_second"] = _int(lim.get("epmc_per_second"), 2) or 2
    lim["batch_size"] = max(1, min(_int(lim.get("batch_size"), 50) or 50, 200))
    lim["max_attempts"] = _int(lim.get("max_attempts"), 3) or 3
    lim["page_size"] = max(1, min(_int(lim.get("page_size"), 200) or 200, 500))
    # null / 0 / missing now means "take everything PubMed will give"
    lim["max_per_query"] = _int(lim.get("max_per_query"), 0) or ESEARCH_HARD_CAP

    dr = cfg["date_range"]
    dr["from"], dr["to"] = _int(dr.get("from"), 1990), _int(dr.get("to"), date.today().year)
    if dr["from"] > dr["to"]:
        dr["from"], dr["to"] = dr["to"], dr["from"]

    cfg["_root"] = Path(cfg["paths"].get("output") or "./output").expanduser().resolve()
    cfg["_sync"] = (Path(cfg["paths"]["sync_to"]).expanduser()
                    if cfg["paths"].get("sync_to") else None)
    cfg["_link_tree"] = bool(cfg["paths"].get("link_into_branches"))
    cfg["_database"] = (cfg.get("source") or {}).get("database", "PubMed")
    material = json.dumps({"disease": cfg["disease"], "branches": cfg["branches"],
                           "date_range": cfg["date_range"],
                           "search": cfg["search"]}, sort_keys=True,
                          default=str)
    cfg["_config_hash"] = hashlib.sha256(material.encode()).hexdigest()[:8]
    cfg["_run_id"] = datetime.now(timezone.utc).strftime("r%Y%m%d_%H%M%S")
    return cfg


class SaveClock:
    """Keeps crash protection without rewriting a large csv every batch."""

    def __init__(self, seconds=60):
        self.seconds, self.last = seconds, None

    def due(self, force=False):
        if force or self.last is None or time.monotonic() - self.last >= self.seconds:
            self.last = time.monotonic()
            return True
        return False


class Throttle:
    def __init__(self, per_second):
        self.interval = 1.0 / per_second if per_second else 0
        self.last, self.calls = 0.0, 0

    def wait(self):
        gap = time.monotonic() - self.last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self.last = time.monotonic()
        self.calls += 1


# ---------------------------------------------------------------- identity

def normalize_doi(doi):
    doi = (doi or "").strip().lower()
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi)
    return re.sub(r"^doi:\s*", "", doi)


def normalize_title(title):
    """For the last-resort match: strip everything that varies by database."""
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())[:60]


def title_key(row):
    title = normalize_title(row.get("title"))
    if not title:
        return ""
    surname = (row.get("first_author") or "").split()[:1]
    return f"{title}|{(surname[0] if surname else '').lower()}|{row.get('pub_year','')}"


def make_key(row):
    for candidate, prefix in ((normalize_doi(row.get("doi")), ""),
                              (row.get("pmcid"), "pmcid:"),
                              (row.get("pmid"), "pmid:")):
        if candidate:
            return f"{prefix}{candidate}"
    return f"title:{title_key(row)}"


def publisher_from_doi(doi):
    doi = normalize_doi(doi)
    if not doi:
        return ""
    return DOI_PREFIXES.get(doi.split("/")[0], f"unknown ({doi.split('/')[0]})")


def source_chain(row, fulltext_source):
    found = row.get("found_in") or "PubMed"
    if fulltext_source:
        return f"{found} -> {fulltext_source}"
    return f"{found} -> {row.get('publisher') or 'unknown publisher'}"


class PaperIndex:
    """Dedup of PAPERS across databases and runs, most reliable id first.
    This is about bytes on disk, not about branch membership."""

    def __init__(self, papers=None):
        self.by_doi, self.by_pmid, self.by_pmcid, self.by_title = {}, {}, {}, {}
        for entry in (papers or {}).values():
            self.add(entry)

    def add(self, entry):
        key = entry["key"]
        if normalize_doi(entry.get("doi")):
            self.by_doi[normalize_doi(entry["doi"])] = key
        if entry.get("pmid"):
            self.by_pmid[str(entry["pmid"])] = key
        if entry.get("pmcid"):
            self.by_pmcid[entry["pmcid"]] = key
        tk = title_key(entry)
        if tk:
            self.by_title[tk] = key

    def find(self, row):
        doi = normalize_doi(row.get("doi"))
        if doi and doi in self.by_doi:
            return self.by_doi[doi], "doi"
        if row.get("pmid") and str(row["pmid"]) in self.by_pmid:
            return self.by_pmid[str(row["pmid"])], "pmid"
        if row.get("pmcid") and row["pmcid"] in self.by_pmcid:
            return self.by_pmcid[row["pmcid"]], "pmcid"
        # two records with ids of their own are two papers, however alike the
        # titles; the title match is only for a record that has no id
        if doi or row.get("pmid") or row.get("pmcid"):
            return None, "new"
        tk = title_key(row)
        if tk and tk in self.by_title:
            return self.by_title[tk], "title_fuzzy"
        return None, "new"


# ---------------------------------------------------------------- paths

def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip(". ")


def paper_stem(row):
    if row.get("pmcid"):
        return f"{row['pmcid']}_{row.get('pmid') or 'noPMID'}"
    if row.get("pmid"):
        return f"noPMC_{row['pmid']}"
    return "doi_" + re.sub(r"[^A-Za-z0-9]+", "-", normalize_doi(row.get("doi")))[:60]


def paper_dir(cfg, row):
    return cfg["_root"] / "papers" / (row.get("pub_year") or "unknown") / paper_stem(row)


def rel(cfg, path):
    try:
        return str(Path(path).relative_to(cfg["_root"]))
    except (ValueError, TypeError):
        return str(path) if path else ""


def write_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    os.replace(part, path)
    return path


def csv_bytes(frame):
    return frame.to_csv(index=False).encode("utf-8")


def cleanup_partials(cfg):
    removed = 0
    if not cfg["_root"].exists():
        return 0
    for leftover in cfg["_root"].rglob("*.part"):
        leftover.unlink()
        removed += 1
    return removed


# ---------------------------------------------------------------- validation

def validate_file(path):
    path = Path(path)
    if not path.exists():
        return False, "missing"
    size = path.stat().st_size
    if size == 0:
        return False, "empty"
    suffix = path.suffix.lower()
    try:
        if suffix == ".xml":
            ElementTree.parse(path)
        elif suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                if zf.testzip() is not None:
                    return False, "zip member corrupt"
                if not zf.namelist():
                    return False, "zip empty"
        elif suffix == ".pdf":
            with path.open("rb") as fh:
                if not fh.read(5).startswith(b"%PDF"):
                    return False, "not a pdf"
                fh.seek(max(0, size - 2048))
                if b"%%EOF" not in fh.read():
                    return False, "pdf truncated"
    except ElementTree.ParseError as exc:
        return False, f"xml truncated: {str(exc)[:60]}"
    except zipfile.BadZipFile:
        return False, "not a zip"
    except OSError as exc:
        return False, type(exc).__name__
    return True, f"{size // 1024} KB"


def sha256_of(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------- state

def state_path(cfg, name):
    return cfg["_root"] / "_state" / name


# The query belongs to the search, not to the tag, so edges hold a 10-char id
# and the text lives once in queries.csv.
QUERIES = {}


def query_id(query):
    if not query:
        return ""
    qid = hashlib.sha1(query.encode("utf-8")).hexdigest()[:10]
    QUERIES.setdefault(qid, query)
    return qid


def load_queries(cfg):
    p = state_path(cfg, "queries.csv")
    if not p.exists():
        return
    for _, row in pd.read_csv(p, dtype=str).fillna("").iterrows():
        if row["query_id"]:
            QUERIES.setdefault(row["query_id"], row["query"])


def save_queries(cfg):
    rows = [{"query_id": k, "query": v} for k, v in sorted(QUERIES.items())]
    write_atomic(state_path(cfg, "queries.csv"),
                 csv_bytes(pd.DataFrame(rows, columns=["query_id", "query"])))


def load_state(cfg):
    papers, edges, runs = {}, {}, []
    load_queries(cfg)
    kept = lost = 0
    p = state_path(cfg, "papers.csv")
    if p.exists():
        for _, row in pd.read_csv(p, dtype=str).fillna("").iterrows():
            entry = dict(row)
            papers[entry["key"]] = entry
    e = state_path(cfg, "paper_branches.csv")
    if e.exists():
        for _, row in pd.read_csv(e, dtype=str).fillna("").iterrows():
            edge = dict(row)
            edge.setdefault("leaf", "")          # older files had no leaf column
            # v5 stored the query text on every edge, cut at 300 chars. A
            # shorter one is whole and moves into queries.csv; a cut one
            # cannot be rebuilt, so its pointer stays blank.
            old = edge.pop("matched_query", "")
            if old and not edge.get("query_id"):
                if len(old) < 300:
                    edge["query_id"] = query_id(old)
                    kept += 1
                else:
                    lost += 1
            edge.setdefault("query_id", "")
            edges[edge_slot(edge["paper_key"], edge["branch"], edge["leaf"],
                            edge["database"], edge["method"])] = edge
    cfg["_legacy_edges"] = (kept, lost)
    r = state_path(cfg, "runs.csv")
    if r.exists():
        runs = pd.read_csv(r, dtype=str).fillna("").to_dict("records")
    return papers, edges, runs


def save_state(cfg, papers, edges, runs=None):
    df = pd.DataFrame(list(papers.values())) if papers else pd.DataFrame()
    write_atomic(state_path(cfg, "papers.csv"),
                 csv_bytes(df.reindex(columns=PAPER_COLUMNS).fillna("")))
    ed = pd.DataFrame(list(edges.values())) if edges else pd.DataFrame()
    write_atomic(state_path(cfg, "paper_branches.csv"),
                 csv_bytes(ed.reindex(columns=EDGE_COLUMNS).fillna("")))
    save_queries(cfg)
    if runs is not None:
        rd = pd.DataFrame(runs) if runs else pd.DataFrame()
        write_atomic(state_path(cfg, "runs.csv"),
                     csv_bytes(rd.reindex(columns=RUN_COLUMNS).fillna("")))


def edge_slot(paper_key, branch, leaf, database, method):
    return (paper_key, branch, leaf or "", database, method)


def add_edge(cfg, edges, paper_key, branch, leaf, method, query=""):
    """One row per paper x branch x leaf x method.

    Same paper in another branch        -> new row.
    Same paper in another leaf, same branch -> new row.
    Same paper, same branch, same leaf  -> refused, the row already exists.
    """
    if not paper_key:
        return False
    slot = edge_slot(paper_key, branch, leaf, cfg["_database"], method)
    if slot in edges:
        return False
    edges[slot] = {"paper_key": paper_key, "branch": branch, "leaf": leaf or "",
                   "database": cfg["_database"], "method": method,
                   "run_id": cfg["_run_id"],
                   "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                   "query_id": query_id(query)}
    return True


def refresh_paper_branches(papers, edges):
    branches, leaves, dbs = defaultdict(set), defaultdict(set), defaultdict(set)
    for edge in edges.values():
        key = edge["paper_key"]
        branches[key].add(edge["branch"])
        leaves[key].add(f"{edge['branch']}/{edge.get('leaf') or '_branch'}")
        dbs[key].add(edge["database"])
    for key, entry in papers.items():
        entry["branches"] = ";".join(sorted(branches.get(key, ())))
        entry["n_branches"] = str(len(branches.get(key, ())))
        entry["leaf_tags"] = ";".join(sorted(leaves.get(key, ())))
        entry["n_leaf_tags"] = str(len(leaves.get(key, ())))
        entry["databases"] = ";".join(sorted(dbs.get(key, ())))


def should_process(entry, cfg):
    if not entry:
        return True, "new"
    status = entry.get("status", "")
    attempts = _int(entry.get("attempts"), 0)
    stored = _int(entry.get("parser_version"), 0)
    if status == "ok":
        return (stored < PARSER_VERSION), (
            "reparse" if stored < PARSER_VERSION else "already done")
    if status == "embargo":
        try:
            waited = (date.today()
                      - date.fromisoformat(str(entry.get("last_attempt"))[:10])).days
        except (ValueError, TypeError):
            waited = 999
        return waited >= 90, "embargo recheck"
    if status in NEW_ROUTE_STATUSES and stored < PARSER_VERSION:
        return True, "new full-text routes"
    if attempts >= cfg["limits"]["max_attempts"]:
        return False, f"gave up after {attempts} tries"
    if status in RETRY_STATUSES:
        return True, f"retry {status}"
    if status == "parse_error":
        return stored < PARSER_VERSION, "parser updated"
    return False, f"{status} needs a human"


FILE_COLUMNS = (("fulltext_path", "fulltext_bytes", "fulltext_sha256"),
                ("pdf_path", "pdf_bytes"),
                ("supplementary_path", "supplementary_files"))


def drop_missing_files(cfg, entry):
    for path_col, *mates in FILE_COLUMNS:
        if entry.get(path_col) and not (cfg["_root"] / entry[path_col]).is_file():
            for col in (path_col, *mates):
                entry[col] = ""


def update_paper(cfg, papers, index, row, status, detail="", match_method="new"):
    key = row["key"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    old = papers.get(key, {})
    action, meaning = ACTIONS.get(status, ("unknown", ""))
    entry = {col: row.get(col) or old.get(col, "") for col in PAPER_COLUMNS}
    entry.update({
        "key": key, "status": status, "action": action,
        "detail": detail or meaning,
        "match_method": old.get("match_method") or match_method,
        # a success clears the counter, otherwise one bad spell retires a paper
        "attempts": "0" if status == "ok" else str(_int(old.get("attempts"), 0) + 1),
        "first_seen": old.get("first_seen") or now,
        "first_seen_run": old.get("first_seen_run") or cfg["_run_id"],
        "last_run": cfg["_run_id"], "last_attempt": now,
        "parser_version": str(PARSER_VERSION),
    })
    drop_missing_files(cfg, entry)
    papers[key] = entry
    index.add(entry)
    return entry


# ---------------------------------------------------------------- queries

OA_FILTER = " AND pubmed pmc open access[filter]"   # unquoted, as NLM writes it


def or_block(terms, mesh=()):
    """Keywords and MeSH descriptors are alternatives inside one block. MeSH
    catches the papers an indexer classified but whose abstract never uses
    the word."""
    parts = [f'"{t}"[Title/Abstract]' for t in terms]
    parts += [f'"{m}"[MeSH Terms]' for m in mesh]
    return " OR ".join(parts)


def build_query(anchor, terms, year_from, year_to, anchor_mesh=(), term_mesh=()):
    return (f'({or_block(anchor, anchor_mesh)}) AND ({or_block(terms, term_mesh)}) '
            f'AND ("{year_from}"[PDAT] : "{year_to}"[PDAT])')


def use_mesh_in_query(cfg):
    return cfg["search"].get("include_mesh", True)


def _mesh_list(body):
    if not isinstance(body, dict):
        return []
    return [str(m).strip() for m in (body.get("mesh") or []) if str(m).strip()]


def query_mesh(cfg, branch, leaf):
    """MeSH that goes into this leaf's query. A leaf uses its own list; branch
    mesh would pull the same papers into every leaf, so it only enters the
    query of a branch that has no leaves."""
    if not use_mesh_in_query(cfg):
        return []
    for name, spec in (cfg["branches"] or {}).items():
        if str(name) != branch:
            continue
        spec = spec or {}
        if leaf == "_all":
            return _mesh_list(spec)
        for leaf_name, body in (spec.get("leaves") or {}).items():
            if str(leaf_name) == leaf:
                return _mesh_list(body)
    return []


def keyword_hit(row, terms):
    """Did a keyword actually appear in the title, abstract or author keywords
    (the three things [Title/Abstract] searches)? If not, the
    paper came in through the MeSH half of the query. Rough on purpose -
    PubMed stems and we do not, so treat it as a signal, not a verdict."""
    text = re.sub(r"[^a-z0-9]+", " ", f"{row.get('title','')} "
                                      f"{row.get('abstract','')} "
                                      f"{row.get('author_keywords','')}".lower())
    return any(re.sub(r"[^a-z0-9]+", " ", t.lower()).strip() in text
               for t in terms)


def stored_row(cfg, entry):
    """The metadata.json of a paper already in the ledger, or None."""
    try:
        return json.loads((cfg["_root"] / entry["metadata_path"])
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return None


def branch_leaves(spec):
    """[(leaf, terms)] for one branch. A flat `terms:` list is one leaf, _all."""
    spec = spec or {}
    out = []
    for name, leaf in (spec.get("leaves") or {}).items():
        terms = leaf.get("terms") if isinstance(leaf, dict) else leaf
        terms = [str(t).strip() for t in (terms or []) if str(t).strip()]
        if terms:
            out.append((str(name), terms))
    if out:
        return out
    terms = [str(t).strip() for t in (spec.get("terms") or []) if str(t).strip()]
    return [("_all", terms)] if terms else []


def active_branches(cfg):
    live, todo = {}, []
    for name, spec in (cfg["branches"] or {}).items():
        leaves = branch_leaves(spec)
        if leaves:
            live[str(name)] = leaves
        else:
            todo.append(str(name))
    return live, todo


def query_plan(cfg, terms, term_mesh=()):
    anchor = cfg["disease"]["anchor"]
    anchor_mesh = _mesh_list(cfg["disease"]) if use_mesh_in_query(cfg) else []
    lo, hi = cfg["date_range"]["from"], cfg["date_range"]["to"]
    spans = ([(str(y), y, y) for y in range(lo, hi + 1)]
             if cfg["date_range"].get("split_by_year") else [(f"{lo}-{hi}", lo, hi)])
    for label, a, b in spans:
        yield label, build_query(anchor, terms, a, b, anchor_mesh, term_mesh)


def retry_call(fn, *args, tries=4, **kwargs):
    for attempt in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)


def esearch(throttle, **kwargs):
    def call():
        handle = Entrez.esearch(db="pubmed", **kwargs)
        try:
            return Entrez.read(handle)
        finally:
            handle.close()

    throttle.wait()
    return retry_call(call)


def count_only(throttle, query):
    return _int(esearch(throttle, term=query, retmax=0).get("Count"), 0)


def search(cfg, throttle, query, stats):
    """Page through esearch instead of keeping only the first screen.

    v4 asked for max_per_query ids and stopped, so a year with more matches was
    silently sampled newest-first - which is how a 2013 paper can go missing.
    """
    cap = min(cfg["limits"]["max_per_query"], ESEARCH_HARD_CAP)
    page, ids, count, start = cfg["limits"]["page_size"], [], 0, 0
    while True:
        want = min(page, cap - len(ids))
        if want <= 0:
            break
        result = esearch(throttle, term=query, retstart=start, retmax=want)
        count = _int(result.get("Count"), 0)
        batch = [str(i) for i in result.get("IdList", [])]
        ids.extend(batch)
        start += len(batch)
        if len(batch) < want or start >= min(count, ESEARCH_HARD_CAP):
            break
    ids = list(dict.fromkeys(ids))
    if count > len(ids):
        msg = (f"CAPPED: {count} match, {len(ids)} ids taken "
               f"(limits.max_per_query={cfg['limits']['max_per_query']}; PubMed "
               f"itself stops paging at 10,000 - split the years) | {query[:70]}")
        stats.warnings.append(msg)
        tqdm.write("  !! " + msg)
    return ids, count


def preflight(throttles=None):
    checks = [("NCBI eutils", EUTILS, None),
              ("Europe PMC REST",
               "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
               "?query=test&format=json&pageSize=1", None)]
    for label, url, _ in checks:
        try:
            resp = requests.get(url, timeout=30, headers=HEADERS)
            if resp.status_code >= 400:
                return False, f"{label} returned HTTP {resp.status_code}"
        except requests.exceptions.RequestException as exc:
            return False, f"{label} unreachable: {type(exc).__name__}"
    return True, "ok"


# ---------------------------------------------------------------- parsing

def _text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(_text(v) for v in value)
    return str(value)


def _attrs(element):
    return getattr(element, "attributes", {})


def _abstract(article):
    parts = []
    for chunk in article.get("Abstract", {}).get("AbstractText", []):
        label = _attrs(chunk).get("Label")
        body = _text(chunk)
        parts.append(f"{label}: {body}" if label else body)
    return " ".join(parts).strip()


def _authors(article):
    names, affs = [], []
    for author in article.get("AuthorList", []):
        if "CollectiveName" in author:
            names.append(_text(author["CollectiveName"]))
        else:
            full = (f"{_text(author.get('LastName',''))} "
                    f"{_text(author.get('ForeName',''))}").strip()
            if full:
                names.append(full)
        for info in author.get("AffiliationInfo", []):
            aff = _text(info.get("Affiliation", ""))
            if aff and aff not in affs:
                affs.append(aff)
    return names, affs


def _pub_date(article):
    pub = article["Journal"]["JournalIssue"].get("PubDate", {})
    if "MedlineDate" in pub:
        raw = _text(pub["MedlineDate"])
        match = re.search(r"\b(19|20)\d{2}\b", raw)
        return (match.group(0) if match else ""), raw
    year = _text(pub.get("Year", ""))
    parts = [year, _text(pub.get("Month", "")), _text(pub.get("Day", ""))]
    return year, " ".join(p for p in parts if p)


def _mesh(citation):
    plain, with_qual, major = [], [], []
    for heading in citation.get("MeshHeadingList", []):
        desc = heading["DescriptorName"]
        name = _text(desc)
        plain.append(name)
        if _attrs(desc).get("MajorTopicYN") == "Y":
            major.append(name)
        quals = heading.get("QualifierName", [])
        if quals:
            for qual in quals:
                with_qual.append(f"{name}/{_text(qual)}")
                if _attrs(qual).get("MajorTopicYN") == "Y":
                    major.append(f"{name}/{_text(qual)}")
        else:
            with_qual.append(name)
    return plain, with_qual, major


def _chemicals(citation):
    names, registry = [], []
    for chem in citation.get("ChemicalList", []):
        name = _text(chem.get("NameOfSubstance", ""))
        names.append(name)
        reg = _text(chem.get("RegistryNumber", ""))
        if reg and reg != "0":
            registry.append(f"{name}={reg}")
    return names, registry


def _keywords(citation):
    return [k for group in citation.get("KeywordList", [])
            for k in (_text(kw).strip() for kw in group) if k]


def _article_ids(record):
    return {_attrs(i).get("IdType", ""): _text(i)
            for i in record.get("PubmedData", {}).get("ArticleIdList", [])}


def _corrections(citation):
    return {_attrs(i).get("RefType") or i.get("RefType", "")
            for i in citation.get("CommentsCorrectionsList", [])}


def parse_article(cfg, record):
    citation = record["MedlineCitation"]
    article = citation["Article"]
    ids = _article_ids(record)
    pmid = _text(citation.get("PMID", "")) or ids.get("pubmed", "")
    doi = normalize_doi(ids.get("doi", "") or _text(next(
        (e for e in article.get("ELocationID", [])
         if _attrs(e).get("EIdType") == "doi"), "")))
    pmcid = ids.get("pmc", "")

    names, affs = _authors(article)
    year, pub_date = _pub_date(article)
    mesh, mesh_qual, mesh_major = _mesh(citation)
    chem_names, chem_reg = _chemicals(citation)
    corrections = _corrections(citation)
    pub_types = [_text(t) for t in article.get("PublicationTypeList", [])]
    journal = article["Journal"]

    row = {
        "pmid": pmid, "pmcid": pmcid, "doi": doi,
        "publisher": publisher_from_doi(doi),
        "title": _text(article.get("ArticleTitle", "")),
        "abstract": _abstract(article),
        "pub_year": year, "pub_date": pub_date,
        "journal": _text(journal.get("Title", "")),
        "journal_abbrev": _text(journal.get("ISOAbbreviation", "")),
        "volume": _text(journal["JournalIssue"].get("Volume", "")),
        "issue": _text(journal["JournalIssue"].get("Issue", "")),
        "pages": _text(article.get("Pagination", {}).get("MedlinePgn", "")),
        "n_authors": len(names),
        "first_author": names[0] if names else "",
        "last_author": names[-1] if names else "",
        "authors": "; ".join(names),
        "affiliations": " | ".join(affs),
        "publication_types": "; ".join(pub_types),
        "language": "; ".join(_text(l) for l in article.get("Language", [])),
        "mesh_terms": "; ".join(mesh),
        "mesh_with_qualifiers": "; ".join(mesh_qual),
        "mesh_major_topics": "; ".join(dict.fromkeys(mesh_major)),
        "author_keywords": "; ".join(_keywords(citation)),
        "substances": "; ".join(chem_names),
        "substance_registry_numbers": "; ".join(chem_reg),
        "gene_symbols": "; ".join(_text(g) for g in citation.get("GeneSymbolList", [])),
        "is_retracted": "yes" if ("RetractionIn" in corrections
                                  or "Retracted Publication" in pub_types) else "no",
        "has_erratum": "yes" if "ErratumIn" in corrections else "no",
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
        "doi_url": f"https://doi.org/{doi}" if doi else "",
        "pmc_url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/" if pmcid else "",
        "in_pmc": "yes" if pmcid else "no",
        "found_in": cfg["_database"],
        "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "parser_version": PARSER_VERSION,
    }
    row["key"] = make_key(row)
    return row


# ---------------------------------------------------------------- mesh tagging

def mesh_lookup(cfg):
    """MeSH descriptor (lowercased) -> the (branch, leaf) slots that claim it."""
    table = defaultdict(set)
    for branch, spec in (cfg["branches"] or {}).items():
        spec = spec or {}
        own = "" if spec.get("leaves") else "_all"
        for term in spec.get("mesh") or []:
            table[str(term).strip().lower()].add((str(branch), own))
        for leaf, body in (spec.get("leaves") or {}).items():
            if isinstance(body, dict):
                for term in body.get("mesh") or []:
                    table[str(term).strip().lower()].add((str(branch), str(leaf)))
    return table


def slots_from_mesh(table, row):
    found = set()
    for term in (row.get("mesh_terms") or "").split(";"):
        found |= table.get(term.strip().lower(), set())
    for term in (row.get("mesh_with_qualifiers") or "").split(";"):
        found |= table.get(term.strip().lower(), set())
    return found


# ---------------------------------------------------------------- fetching

def fetch_batch(cfg, throttle, pmids, tag):
    def call():
        handle = Entrez.efetch(db="pubmed", id=",".join(pmids), retmode="xml")
        try:
            raw = handle.read()
        finally:
            handle.close()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        return raw, Entrez.read(io.BytesIO(raw))

    throttle.wait()
    raw, parsed = retry_call(call)
    write_atomic(cfg["_root"] / "raw" / cfg["_run_id"] / f"{safe_name(tag)}.xml", raw)

    out, seen = [], set()
    for record in parsed.get("PubmedArticle", []):
        pmid = _text(record.get("MedlineCitation", {}).get("PMID", "?"))
        seen.add(pmid)
        try:
            out.append((parse_article(cfg, record), None))
        except Exception as exc:
            out.append(({"pmid": pmid, "key": f"pmid:{pmid}"},
                        f"{type(exc).__name__}: {exc}"))
    # books, chapters and withdrawn records come back under other keys, or not
    # at all; record them so they settle instead of being re-fetched every run
    for pmid in pmids:
        if pmid not in seen:
            out.append(({"pmid": pmid, "key": f"pmid:{pmid}"},
                        "not a PubmedArticle record (book, chapter or removed)"))
    return out


def http_get(throttle, url, timeout=60, tries=3):
    """EBI returns a sporadic 500 under load; a short backoff clears most."""
    resp = None
    for attempt in range(tries):
        throttle.wait()
        resp = requests.get(url, timeout=timeout, headers=HEADERS)
        if resp.status_code < 500:
            return resp
        if attempt < tries - 1:
            time.sleep(2 ** attempt)
    return resp


def epmc_get(throttle, pmcid, what, timeout=60):
    return http_get(throttle, EPMC.format(pmcid=pmcid, what=what), timeout)


def fetch_pdf(cfg, throttle, row):
    """The reading copy. XML is for the pipeline, this is for your eyes."""
    pmcid = row.get("pmcid")
    if not pmcid:
        return "", ""
    target = paper_dir(cfg, row) / "fulltext.pdf"
    if target.exists():
        if validate_file(target)[0]:
            return rel(cfg, target), str(target.stat().st_size)
        target.unlink()
    try:
        resp = http_get(throttle, EPMC_PDF.format(pmcid=pmcid))
    except requests.exceptions.RequestException:
        return "", ""
    # a non-OA article answers with an HTML page, not a pdf
    if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
        return "", ""
    write_atomic(target, resp.content)
    if not validate_file(target)[0]:
        target.unlink(missing_ok=True)
        return "", ""
    return rel(cfg, target), str(target.stat().st_size)


def fill_pmcids(throttle, rows):
    """efetch leaves PMCID blank for records the id converter still resolves.
    Called once per batch, before anything is written, so the folder name and
    every later route see the PMCID."""
    pending = {str(r["pmid"]): r for r in rows if not r.get("pmcid") and r.get("pmid")}
    if not pending:
        return 0
    throttle.wait()
    try:
        resp = requests.get(IDCONV, timeout=60, headers=HEADERS, params={
            "ids": ",".join(list(pending)[:200]), "idtype": "pmid",
            "format": "json", "tool": "amd-crawler", "email": Entrez.email})
        records = resp.json().get("records", [])
    except (requests.exceptions.RequestException, ValueError, AttributeError):
        return 0
    filled = 0
    for rec in records:
        row = pending.get(str(rec.get("pmid")))
        if row is not None and rec.get("pmcid"):
            row["pmcid"] = rec["pmcid"]
            row["pmc_url"] = f"https://pmc.ncbi.nlm.nih.gov/articles/{rec['pmcid']}/"
            row["in_pmc"] = "yes"
            filled += 1
    return filled


def ncbi_pmc_xml(throttle, pmcid):
    """Europe PMC answers 500, not 404, for a PMC author manuscript outside
    its open-access subset. NCBI serves many of them. No <body> means it
    really is shut. None means NCBI did not answer, which settles nothing."""
    throttle.wait()
    try:
        handle = Entrez.efetch(db="pmc", id=pmcid.replace("PMC", ""), retmode="xml")
        try:
            raw = handle.read()
        finally:
            handle.close()
    except urllib.error.HTTPError as exc:
        return b"" if 400 <= exc.code < 500 and exc.code != 429 else None
    except Exception:
        return None
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return raw if b"<body" in raw else b""


def unpaywall_pdf(throttle, doi):
    """A legal copy anywhere: repository, author manuscript, publisher OA.
    None means Unpaywall did not answer."""
    throttle.wait()
    try:
        resp = requests.get(UNPAYWALL.format(doi=doi), timeout=40,
                            headers=HEADERS, params={"email": Entrez.email})
        if resp.status_code == 429 or resp.status_code >= 500:
            return None
        data = resp.json() if resp.status_code == 200 else {}
    except (requests.exceptions.RequestException, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("is_oa"):
        return "", "", {}
    best = data.get("best_oa_location") or {}
    for loc in [best] + list(data.get("oa_locations") or []):
        if loc and loc.get("url_for_pdf"):
            host = loc.get("host_type") or data.get("oa_status") or "oa"
            return loc["url_for_pdf"], f"Unpaywall ({host})", {}
    return "", "", {}


def tdm_pdf(doi, keys):
    """The route the publisher sanctions. Entitlement comes from the campus
    IP, so this only pays off on the TMU network."""
    vendor = TDM_PREFIXES.get(doi.split("/")[0])
    if not vendor or not keys.get(vendor):
        return "", "", {}
    if vendor == "elsevier":
        headers = {"X-ELS-APIKey": keys["elsevier"], "Accept": "application/pdf"}
        if keys.get("elsevier_insttoken"):
            headers["X-ELS-Insttoken"] = keys["elsevier_insttoken"]
        return ELSEVIER.format(doi=doi), "Elsevier TDM API", headers
    return (WILEY.format(doi=doi.replace("/", "%2F")), "Wiley TDM API",
            {"Wiley-TDM-Client-Token": keys["wiley"], "Accept": "application/pdf"})


def save_pdf_from(throttle, url, target, headers=None):
    throttle.wait()
    try:
        resp = requests.get(url, timeout=90, allow_redirects=True,
                            headers={**HEADERS, **(headers or {})})
    except requests.exceptions.RequestException:
        return False
    if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
        return False
    write_atomic(target, resp.content)
    if not validate_file(target)[0]:
        target.unlink(missing_ok=True)
        return False
    return True


def _saved(cfg, path):
    size = str(path.stat().st_size)
    if path.suffix == ".pdf":
        return {"pdf_path": rel(cfg, path), "pdf_bytes": size}
    return {"fulltext_path": rel(cfg, path), "fulltext_bytes": size,
            "fulltext_sha256": sha256_of(path)}


def fetch_fulltext(cfg, throttles, row, stats):
    """Europe PMC xml -> NCBI xml -> Unpaywall pdf -> publisher TDM pdf."""
    ncbi, epmc = throttles["esearch/efetch (NCBI)"], throttles["Europe PMC"]
    folder = paper_dir(cfg, row)
    xml, pdf = folder / "fulltext.xml", folder / "fulltext.pdf"

    for target in (xml, pdf):
        if target.exists():
            ok, detail = validate_file(target)
            if ok:
                return "ok", f"cached, {detail}", "cached", _saved(cfg, target)
            target.unlink()
            stats.verified["repaired"] += 1

    unanswered = []
    pmcid = row.get("pmcid")
    if pmcid:
        try:
            resp = epmc_get(epmc, pmcid, "fullTextXML")
        except requests.exceptions.RequestException as exc:
            return "network_blocked", type(exc).__name__, "", {}
        if resp.status_code == 200:
            write_atomic(xml, resp.content)
            ok, detail = validate_file(xml)
            if ok:
                return "ok", detail, "Europe PMC (PMC)", _saved(cfg, xml)
            xml.unlink(missing_ok=True)
            return "corrupt_file", detail, "", {}
        if resp.status_code not in (401, 403, 404, 500):
            return "http_error", f"HTTP {resp.status_code}", "", {}
        raw = ncbi_pmc_xml(ncbi, pmcid)
        if raw is None:
            unanswered.append("NCBI pmc")
        elif raw:
            write_atomic(xml, raw)
            ok, detail = validate_file(xml)
            if ok:
                return "ok", detail, "NCBI efetch (pmc)", _saved(cfg, xml)
            xml.unlink(missing_ok=True)
        stats.bump("epmc_refused")

    doi = normalize_doi(row.get("doi"))
    if doi and cfg["download"].get("try_open_access", True):
        for route in (unpaywall_pdf(epmc, doi),
                      tdm_pdf(doi, cfg["publisher_api"] or {})):
            if route is None:
                unanswered.append("Unpaywall")
                continue
            url, source, headers = route
            if url and save_pdf_from(epmc, url, pdf, headers):
                return "ok", f"pdf, {validate_file(pdf)[1]}", source, _saved(cfg, pdf)

    # a route that did not answer leaves the question open, so it is retried
    # instead of being filed as "no copy exists"
    if unanswered:
        return "http_error", f"no answer from {', '.join(unanswered)}", "", {}
    if pmcid:
        return "pmc_not_oa", "in PMC, body not retrievable", "", {}
    return "not_in_pmc", "no PMC copy and no open copy found", "", {}


def fetch_supplementary(cfg, throttle, row):
    pmcid = row.get("pmcid")
    if not pmcid:
        return "", ""
    target = paper_dir(cfg, row) / "supplementary.zip"
    if target.exists():
        if validate_file(target)[0]:
            with zipfile.ZipFile(target) as zf:
                return rel(cfg, target), "; ".join(zf.namelist()[:20])
        target.unlink()
    try:
        resp = epmc_get(throttle, pmcid, "supplementaryFiles")
    except requests.exceptions.RequestException:
        return "", ""
    if resp.status_code != 200 or not resp.content.startswith(b"PK"):
        return "", ""
    write_atomic(target, resp.content)
    if not validate_file(target)[0]:
        target.unlink(missing_ok=True)
        return "", ""
    with zipfile.ZipFile(target) as zf:
        return rel(cfg, target), "; ".join(zf.namelist()[:20])


def verify_cached(cfg, papers, stats):
    rows = [e for e in papers.values() if e.get("status") == "ok"
            and (e.get("fulltext_path") or e.get("pdf_path"))]
    for entry in tqdm(rows, desc="verifying earlier downloads", unit="file"):
        stats.verified["checked"] += 1
        good, bad = [], []
        for col in ("fulltext_path", "pdf_path"):
            if not entry.get(col):
                continue
            path = cfg["_root"] / entry[col]
            ok, detail = validate_file(path)
            if ok:
                good.append(col)
            else:
                path.unlink(missing_ok=True)
                bad.append(f"{path.name}: {detail}")
        if not bad:
            continue
        drop_missing_files(cfg, entry)
        stats.verified["repaired"] += 1
        if good:
            # one good copy is still the paper; only the broken one is gone
            entry["detail"] = "xml" if good == ["fulltext_path"] else "pdf"
            tqdm.write(f"  dropped a corrupt file, paper still saved: "
                       f"{entry['key']} ({'; '.join(bad)})")
            continue
        entry.update({"status": "corrupt_file",
                      "action": ACTIONS["corrupt_file"][0],
                      "detail": f"re-check failed: {'; '.join(bad)}", "attempts": "0"})
        tqdm.write(f"  corrupt, will re-download: {entry['key']} ({'; '.join(bad)})")


# ---------------------------------------------------------------- analysis

def branch_coverage(cfg, edges):
    """Where the keywords and MeSH disagree - a keyword-gap report.
    found_by_query = a keyword really is in the title or abstract.
    mesh_only      = nothing but MeSH reached this paper. If that number is
                     large, either the keyword list has a hole, or the MeSH
                     descriptor is too broad and is dragging in junk."""
    by_query, by_mesh = defaultdict(set), defaultdict(set)
    for edge in edges.values():
        bucket = by_query if edge["method"] == "query" else by_mesh
        bucket[edge["branch"]].add(edge["paper_key"])
    rows = []
    for branch in (cfg["branches"] or {}):
        branch = str(branch)
        q, m = by_query[branch], by_mesh[branch]
        leaves = branch_leaves((cfg["branches"] or {}).get(branch))
        rows.append({
            "branch": branch,
            "leaves": len(leaves) or "TODO",
            "found_by_query": len(q),
            "found_by_mesh": len(m),
            "both": len(q & m),
            "mesh_only": len(m - q),
            "keyword_gap": "check keywords" if len(m - q) > max(3, 0.2 * len(q)) else "",
        })
    return pd.DataFrame(rows)


def leaf_coverage(cfg, edges):
    """Per leaf: how many papers it claims, and how many it claims alone."""
    per_slot, per_paper = defaultdict(set), defaultdict(set)
    for edge in edges.values():
        slot = (edge["branch"], edge.get("leaf") or "_branch")
        per_slot[slot].add(edge["paper_key"])
        per_paper[edge["paper_key"]].add(slot)
    rows = []
    for branch, leaves in active_branches(cfg)[0].items():
        for leaf, terms in leaves:
            keys = per_slot.get((branch, leaf), set())
            alone = sum(1 for k in keys if len(per_paper[k]) == 1)
            rows.append({"branch": branch, "leaf": leaf, "terms": len(terms),
                         "papers": len(keys), "only_here": alone,
                         "also_elsewhere": len(keys) - alone})
    return pd.DataFrame(rows)


def duplicate_report(papers, edges):
    """The repeats, written down - which is what you asked the crawler to allow."""
    per_paper = defaultdict(set)
    for edge in edges.values():
        per_paper[edge["paper_key"]].add(
            (edge["branch"], edge.get("leaf") or "_branch"))
    rows = []
    for key, slots in per_paper.items():
        if len(slots) < 2:
            continue
        entry = papers.get(key, {})
        rows.append({
            "paper_key": key, "title": entry.get("title", ""),
            "pub_year": entry.get("pub_year", ""),
            "n_branches": len({b for b, _ in slots}),
            "n_leaf_tags": len(slots),
            "branches": ";".join(sorted({b for b, _ in slots})),
            "leaf_tags": ";".join(sorted(f"{b}/{l}" for b, l in slots)),
            "doi_url": entry.get("doi_url", ""),
        })
    df = pd.DataFrame(rows)
    return (df.sort_values(["n_leaf_tags", "n_branches"], ascending=False)
            if not df.empty else df)


def branch_overlap(edges, top=25):
    """How many papers each pair of branches shares - a first graph edge list."""
    per_paper = defaultdict(set)
    for edge in edges.values():
        per_paper[edge["paper_key"]].add(edge["branch"])
    pairs = defaultdict(int)
    for branches in per_paper.values():
        ordered = sorted(branches)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:]:
                pairs[(a, b)] += 1
    df = pd.DataFrame([{"branch_a": a, "branch_b": b, "shared_papers": n}
                       for (a, b), n in pairs.items()])
    return (df.sort_values("shared_papers", ascending=False).head(top)
            if not df.empty else df)


def stamp_branches_into_json(cfg, papers, edges, keys):
    """metadata.json is written before tagging finishes, and a later leaf can
    tag the same paper, so the tag list is written back at the end."""
    tags = defaultdict(list)
    for edge in edges.values():
        tags[edge["paper_key"]].append(edge)
    updated = 0
    for key in keys:
        entry = papers.get(key)
        if not entry or not entry.get("metadata_path"):
            continue
        path = cfg["_root"] / entry["metadata_path"]
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        mine = tags.get(key, ())
        data["branches"] = sorted({e["branch"] for e in mine})
        data["n_branches"] = len(data["branches"])
        data["leaf_tags"] = sorted({f"{e['branch']}/{e.get('leaf') or '_branch'}"
                                    for e in mine})
        data["branch_tags"] = sorted(
            ({"branch": e["branch"], "leaf": e.get("leaf", ""),
              "method": e["method"], "database": e["database"],
              "run_id": e["run_id"], "query_id": e.get("query_id", "")}
             for e in mine),
            key=lambda t: (t["branch"], t["leaf"], t["method"]))
        write_atomic(path, json.dumps(data, ensure_ascii=False,
                                      indent=2).encode("utf-8"))
        updated += 1
    return updated


def branch_matrix(papers, edges):
    """One row per paper, one column per branch/leaf - the view for writing up."""
    if not edges:
        return pd.DataFrame()
    cells = defaultdict(dict)
    for edge in edges.values():
        cells[edge["paper_key"]][
            f"{edge['branch']}/{edge.get('leaf') or '_branch'}"] = 1
    df = pd.DataFrame.from_dict(cells, orient="index").fillna(0).astype(int)
    df = df.reindex(sorted(df.columns), axis=1)
    meta = pd.DataFrame([{"paper_key": k,
                          "title": papers.get(k, {}).get("title", ""),
                          "pub_year": papers.get(k, {}).get("pub_year", ""),
                          "doi_url": papers.get(k, {}).get("doi_url", ""),
                          "n_tags": int(df.loc[k].sum())} for k in df.index])
    return (pd.concat([meta.set_index("paper_key"), df], axis=1)
              .rename_axis("paper_key").reset_index())


def _manifest_row(paper, edge):
    return {"paper_key": paper["key"], "leaf": edge.get("leaf") or "_branch",
            "title": paper.get("title", ""), "pub_year": paper.get("pub_year", ""),
            "journal": paper.get("journal", ""), "doi_url": paper.get("doi_url", ""),
            "status": paper.get("status", ""), "action": paper.get("action", ""),
            "tagged_by": edge["method"], "n_leaf_tags": paper.get("n_leaf_tags", ""),
            "leaf_tags": paper.get("leaf_tags", ""),
            "paper_folder": paper.get("paper_folder", ""),
            "fulltext_path": paper.get("fulltext_path", ""),
            "pdf_path": paper.get("pdf_path", "")}


def _collapse(rows, on):
    """One row per `on`, with the methods that produced it merged."""
    df = pd.DataFrame(rows)
    methods = df.groupby(on)["tagged_by"].agg(lambda s: "+".join(sorted(set(s))))
    df = df.drop_duplicates(subset=on).copy()
    df["tagged_by"] = (df[on] if isinstance(on, str) else
                       df[on].apply(tuple, axis=1)).map(methods)
    return df


def write_branch_manifests(cfg, papers, edges):
    """Per-leaf CSV so the tree stays browsable without duplicating bytes."""
    out = cfg["_root"] / "_index" / "branches"
    by_slot, by_branch = defaultdict(list), defaultdict(list)
    for edge in edges.values():
        paper = papers.get(edge["paper_key"])
        if not paper:
            continue
        row = _manifest_row(paper, edge)
        by_slot[(edge["branch"], edge.get("leaf") or "_branch")].append(row)
        by_branch[edge["branch"]].append(row)

    written = 0
    for (branch, leaf), rows in by_slot.items():
        path = out / safe_name(branch) / f"{safe_name(leaf) or '_branch'}.csv"
        write_atomic(path, csv_bytes(_collapse(rows, "paper_key")))
        written += 1
    for branch, rows in by_branch.items():
        write_atomic(out / safe_name(branch) / "_all_leaves.csv",
                     csv_bytes(_collapse(rows, ["paper_key", "leaf"])))
    return written, len(by_branch)


def link_into_branches(cfg, papers, edges):
    """Optional: a per-leaf folder tree of hardlinks to the one stored copy."""
    if not cfg["_link_tree"]:
        return 0
    root = cfg["_root"] / "_index" / "tree"
    linked = 0
    for edge in edges.values():
        paper = papers.get(edge["paper_key"])
        if not paper or not paper.get("paper_folder"):
            continue
        src = cfg["_root"] / paper["paper_folder"]
        if not src.is_dir():
            continue
        dest = (root / safe_name(edge["branch"])
                / safe_name(edge.get("leaf") or "_branch") / src.name)
        dest.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if not item.is_file():
                continue
            target = dest / item.name
            if target.exists():
                continue
            try:
                os.link(item, target)
            except OSError:
                shutil.copy2(item, target)
            linked += 1
    return linked


# ---------------------------------------------------------------- reports

class RunStats:
    def __init__(self):
        self.started = datetime.now(timezone.utc)
        self.searches, self.warnings = [], []
        self.counters = {}
        self.verified = {"checked": 0, "repaired": 0}
        self.dedup = defaultdict(int)

    def bump(self, name, n=1):
        self.counters[name] = self.counters.get(name, 0) + n


ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_cell(value):
    if isinstance(value, str):
        value = ILLEGAL.sub("", value)
        if len(value) > 32000:
            value = value[:32000] + " [...truncated]"
    return value


def clean_frame(df):
    mapper = getattr(df, "map", None)           # pandas >= 2.1
    return mapper(clean_cell) if mapper else df.applymap(clean_cell)


def linkify(ws):
    if ws.max_row < 2:
        return
    col = {cell.value: cell.column for cell in ws[1]}.get(LINK_COLUMN)
    if not col:
        return
    for r in range(2, min(ws.max_row, MAX_LINKS_PER_SHEET + 1) + 1):
        cell = ws.cell(row=r, column=col)
        if isinstance(cell.value, str) and cell.value.startswith("http"):
            cell.hyperlink = cell.value
            cell.style = "Hyperlink"


def write_excel(cfg, papers, edges, runs):
    df = pd.DataFrame(list(papers.values())) if papers else pd.DataFrame()
    df = clean_frame(df.reindex(columns=PAPER_COLUMNS).fillna(""))
    saved, not_saved = df[df["status"] == "ok"], df[df["status"] != "ok"]
    out = cfg["_root"] / "_index" / "eye_disease_papers.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)

    sheets = {
        "saved": saved, "not_saved": not_saved,
        "need_TMU": not_saved[not_saved["action"] == "needs_tmu"],
        "need_request": not_saved[not_saved["action"] == "needs_request"],
        "paper_branches": (pd.DataFrame(list(edges.values())) if edges
                           else pd.DataFrame()).reindex(columns=EDGE_COLUMNS),
        "queries": clean_frame(pd.DataFrame(
            [{"query_id": k, "query": v} for k, v in sorted(QUERIES.items())],
            columns=["query_id", "query"])),
        "branch_coverage": branch_coverage(cfg, edges),
        "leaf_coverage": leaf_coverage(cfg, edges),
        "duplicates": duplicate_report(papers, edges),
        "branch_overlap": branch_overlap(edges),
        "branch_matrix": branch_matrix(papers, edges),
    }
    if not df.empty:
        sheets["summary_by_action"] = (
            df.groupby(["action", "status"]).size().reset_index(name="papers")
              .sort_values("papers", ascending=False))
        sheets["summary_by_publisher"] = (
            df[df["action"] == "needs_tmu"].groupby("publisher").size()
              .reset_index(name="papers").sort_values("papers", ascending=False))
    sheets["runs"] = pd.DataFrame(runs).reindex(columns=RUN_COLUMNS)

    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
        for name in ("saved", "not_saved", "need_TMU", "need_request",
                     "duplicates"):
            linkify(writer.book[name])
    return len(saved), len(not_saved), out


def write_report(cfg, stats, papers, edges, throttles, todo_branches, live):
    finished = datetime.now(timezone.utc)
    minutes = (finished - stats.started).total_seconds() / 60
    searches = pd.DataFrame(stats.searches)
    actions = pd.Series([e.get("action", "") for e in papers.values()]).value_counts()
    dups = duplicate_report(papers, edges)
    n_leaves = sum(len(v) for v in live.values())

    lines = [
        "AMD knowledge-framework crawler - run report", "=" * 74,
        f"run_id      {cfg['_run_id']}",
        f"database    {cfg['_database']}",
        f"config_hash {cfg['_config_hash']}   (same hash = same keywords)",
        f"started     {stats.started:%Y-%m-%d %H:%M} UTC",
        f"finished    {finished:%Y-%m-%d %H:%M} UTC",
        f"duration    {int(minutes // 60)}h {int(minutes % 60)}m",
        f"output      {cfg['_root']}", "",
        "SEARCHES - one row per API query", "-" * 74,
    ]
    if not searches.empty:
        lines.append(searches.to_string(index=False))
        lines += ["-" * 74,
                  f"matched in PubMed : {searches['matched'].sum():>8}",
                  f"ids retrieved     : {searches['retrieved'].sum():>8}",
                  f"open access       : {searches['open_access'].sum():>8}",
                  f"new this run      : {searches['new'].sum():>8}",
                  f"skipped (settled) : {searches['skipped'].sum():>8}"]
    else:
        lines.append("(none)")

    lines += ["", "DOWNLOADS THIS RUN", "-" * 74]
    for name in ("metadata_saved", "fulltext_saved", "pdf_saved",
                 "supplementary_saved", "fulltext_failed", "pdf_failed",
                 "supplementary_failed", "pmcid_recovered", "epmc_refused"):
        lines.append(f"{name:26} {stats.counters.get(name, 0):>8}")

    routes = pd.Series([e.get("source_fulltext", "") for e in papers.values()
                        if e.get("status") == "ok"]).value_counts()
    if not routes.empty:
        lines += ["", "FULL TEXT, by route", "-" * 74]
        for name, n in routes.items():
            lines.append(f"{name or 'unknown':26} {n:>8}")

    lines += ["", "PAPER IDENTITY - how each record matched an existing paper",
              "-" * 74]
    for method in ("new", "doi", "pmid", "pmcid", "title_fuzzy"):
        lines.append(f"{method:26} {stats.dedup.get(method, 0):>8}"
                     + ("   <- spot-check these" if method == "title_fuzzy"
                        and stats.dedup.get(method) else ""))

    lines += ["", "REPEATS - allowed across branches and across leaves", "-" * 74,
              f"{'branch/leaf tags':26} {len(edges):>8}",
              f"{'papers':26} {len(papers):>8}",
              f"{'papers with >1 tag':26} {len(dups):>8}",
              f"{'new tags this run':26} {stats.counters.get('tags_new', 0):>8}",
              f"{'repeat tags refused':26} "
              f"{stats.counters.get('tags_duplicate', 0):>8}"
              "   <- same branch AND same leaf"]
    if papers:
        lines.append(f"{'tags per paper (avg)':26} {len(edges)/len(papers):>8.1f}")
    if not dups.empty:
        lines += ["", "most repeated papers:",
                  dups.head(10)[["n_leaf_tags", "pub_year", "leaf_tags"]]
                      .to_string(index=False)]

    lines += ["", "FILE VERIFICATION", "-" * 74,
              f"{'files checked':26} {stats.verified['checked']:>8}",
              f"{'corrupt, re-queued':26} {stats.verified['repaired']:>8}"]

    lines += ["", "LEDGER TOTAL, by who can resolve it", "-" * 74]
    for action, n in actions.items():
        lines.append(f"{action:26} {n:>8}")

    cov = branch_coverage(cfg, edges)
    gaps = cov[cov["keyword_gap"] != ""] if not cov.empty else cov
    lines += ["", "BRANCH COVERAGE", "-" * 74, cov.to_string(index=False)]
    if not gaps.empty:
        lines += ["", "Branches where MeSH found papers your keywords missed:",
                  "  " + ", ".join(gaps["branch"])]

    leafcov = leaf_coverage(cfg, edges)
    if not leafcov.empty:
        lines += ["", f"LEAF COVERAGE - {n_leaves} leaves searched", "-" * 74,
                  leafcov.to_string(index=False)]
        dead = leafcov[leafcov["papers"] == 0]
        if not dead.empty:
            lines += ["", "leaves that found nothing (check the terms):",
                      "  " + ", ".join(f"{r.branch}/{r.leaf}"
                                       for r in dead.itertuples())]
    if todo_branches:
        lines += ["", f"TODO - {len(todo_branches)} branches have no terms yet:",
                  "  " + ", ".join(todo_branches)]

    overlap = branch_overlap(edges, top=15)
    if not overlap.empty:
        lines += ["", "BRANCH OVERLAP - shared papers (an early graph edge list)",
                  "-" * 74, overlap.to_string(index=False)]

    pub = pd.Series([e.get("publisher", "") for e in papers.values()
                     if e.get("action") == "needs_tmu"]).value_counts()
    if not pub.empty:
        lines += ["", "NEEDS TMU, by publisher", "-" * 74]
        for name, n in pub.head(15).items():
            lines.append(f"{name or 'unknown':26} {n:>8}")

    lines += ["", "API CALLS", "-" * 74]
    for name, throttle in throttles.items():
        lines.append(f"{name:26} {throttle.calls:>8}")

    lines += ["", "WARNINGS", "-" * 74]
    lines += [f"  {w}" for w in stats.warnings] or ["  (none)"]

    text = "\n".join(lines) + "\n"
    out = state_path(cfg, f"run_report_{cfg['_run_id']}.txt")
    write_atomic(out, text.encode("utf-8"))
    write_atomic(state_path(cfg, "run_report_latest.txt"), text.encode("utf-8"))

    total = (lambda col: int(searches[col].sum()) if not searches.empty else 0)
    row = {"run_id": cfg["_run_id"], "started": f"{stats.started:%Y-%m-%d %H:%M}",
           "finished": f"{finished:%Y-%m-%d %H:%M}", "database": cfg["_database"],
           "config_hash": cfg["_config_hash"],
           "branches_searched": len(live), "branches_todo": len(todo_branches),
           "leaves_searched": n_leaves,
           "matched": total("matched"), "retrieved": total("retrieved"),
           "open_access": total("open_access"), "new": total("new"),
           "skipped": total("skipped"),
           "fulltext_saved": stats.counters.get("fulltext_saved", 0),
           "pdf_saved": stats.counters.get("pdf_saved", 0),
           "supplementary_saved": stats.counters.get("supplementary_saved", 0),
           "repeat_tags": len(dups),
           "failed": stats.counters.get("fulltext_failed", 0),
           "warnings": len(stats.warnings)}
    return text, out, row


def sync(cfg):
    dest = cfg["_sync"]
    if not dest:
        return
    dest.mkdir(parents=True, exist_ok=True)
    if shutil.which("rsync"):
        subprocess.run(["rsync", "-a", "--no-perms", "--no-owner", "--no-group",
                        f"{cfg['_root']}/", f"{dest}/"], check=False)
    else:
        shutil.copytree(cfg["_root"], dest, dirs_exist_ok=True)


# ---------------------------------------------------------------- main

def main(config_path="config.yaml"):
    cfg = load_config(config_path)
    (cfg["_root"] / "_state").mkdir(parents=True, exist_ok=True)
    stats = RunStats()
    throttles = {"esearch/efetch (NCBI)": Throttle(cfg["limits"]["ncbi_per_second"]),
                 "Europe PMC": Throttle(cfg["limits"]["epmc_per_second"])}
    ncbi, epmc = throttles["esearch/efetch (NCBI)"], throttles["Europe PMC"]

    leftovers = cleanup_partials(cfg)
    if leftovers:
        print(f"removed {leftovers} half-written file(s) from a previous crash")

    papers, edges, runs = load_state(cfg)
    index = PaperIndex(papers)
    live, todo = active_branches(cfg)
    mesh_table = mesh_lookup(cfg) if cfg["download"].get("mesh_autotag") else {}

    print(f"run_id {cfg['_run_id']} | database {cfg['_database']} "
          f"| config {cfg['_config_hash']}")
    print(f"output {cfg['_root']}")
    print(f"state  {len(papers)} papers, {len(edges)} branch/leaf tags, "
          f"{len(runs)} earlier runs")
    print(f"branches: {len(live)} with terms "
          f"({sum(len(v) for v in live.values())} leaves), {len(todo)} TODO")
    kept, lost = cfg["_legacy_edges"]
    if kept or lost:
        print(f"migrated v5 tags: {kept} queries moved into queries.csv, "
              f"{lost} were cut at 300 chars and are left blank")
    touched, saves = set(), SaveClock()
    # a paper x branch x leaf carries "query" or "query_mesh", never both.
    # Earlier runs are in here too.
    query_tagged = {k[:3] for k in edges if k[4] in QUERY_METHODS}

    ok, why = preflight()
    if not ok:
        print(f"\nnetwork check failed: {why}\n"
              f"Nothing written. Run --test-api for the detail.")
        return
    if cfg["download"].get("verify_cached"):
        verify_cached(cfg, papers, stats)

    def tag(paper_key, branch, leaf, method, query=""):
        if add_edge(cfg, edges, paper_key, branch, leaf, method, query):
            stats.bump("tags_new")
            touched.add(paper_key)
        else:
            stats.bump("tags_duplicate")

    def tag_query(paper_key, branch, leaf, method, query):
        slot = (paper_key, branch, leaf)
        if slot in query_tagged:
            stats.bump("tags_duplicate")
            return
        query_tagged.add(slot)
        tag(paper_key, branch, leaf, method, query)

    for branch, leaves in live.items():
        for leaf, terms in leaves:
            leaf_mesh = query_mesh(cfg, branch, leaf)
            for year_tag, query in query_plan(cfg, terms, leaf_mesh):
                # every query that runs is registered, so queries.csv is the
                # full search log, not only the queries that tagged something
                qid = query_id(query)
                ids, matched = search(cfg, ncbi, query, stats)
                oa_total = 0
                if cfg["download"].get("measure_open_access") and ids:
                    oa_total = count_only(ncbi, query + OA_FILTER)

                todo_ids = []
                for pmid in ids:
                    key = index.by_pmid.get(pmid)
                    entry = papers.get(key) if key else None
                    if should_process(entry, cfg)[0]:
                        todo_ids.append(pmid)
                    else:
                        # already on disk: no re-download, but this leaf still
                        # gets its own row. The keyword test runs on the stored
                        # metadata.json; without one it is filed as "query".
                        old = stored_row(cfg, entry) if leaf_mesh else None
                        by_keyword = not old or keyword_hit(old, terms)
                        tag_query(key, branch, leaf,
                                  "query" if by_keyword else "query_mesh", query)

                stats.searches.append(
                    {"branch": branch, "leaf": leaf, "year": year_tag,
                     "query_id": qid, "matched": matched, "retrieved": len(ids),
                     "open_access": oa_total, "new": len(todo_ids),
                     "skipped": len(ids) - len(todo_ids)})
                if not todo_ids:
                    continue

                bar = tqdm(total=len(todo_ids), unit="paper",
                           desc=f"{branch}/{leaf} {year_tag}")
                size = cfg["limits"]["batch_size"]
                for i in range(0, len(todo_ids), size):
                    chunk = todo_ids[i:i + size]
                    batch_tag = f"{branch}_{leaf}_{year_tag}_{i // size:03d}"
                    batch = fetch_batch(cfg, ncbi, chunk, batch_tag)
                    stats.bump("pmcid_recovered",
                               fill_pmcids(ncbi, [r for r, err in batch if not err]))
                    for row, parse_err in batch:
                        if parse_err:
                            update_paper(cfg, papers, index, row,
                                         "parse_error", parse_err)
                            bar.update(1)
                            continue

                        existing, method = index.find(row)
                        stats.dedup[method] += 1
                        if existing:
                            row["key"] = existing

                        folder = paper_dir(cfg, row)
                        write_atomic(folder / "metadata.json",
                                     json.dumps(row, ensure_ascii=False,
                                                indent=2).encode("utf-8"))
                        row["paper_folder"] = rel(cfg, folder)
                        row["metadata_path"] = rel(cfg, folder / "metadata.json")
                        stats.bump("metadata_saved")
                        touched.add(row["key"])

                        status, detail, src, extra = "metadata_only", "", "", {}
                        if cfg["download"].get("fulltext"):
                            status, detail, src, extra = fetch_fulltext(
                                cfg, throttles, row, stats)
                        if src == "cached":
                            src = (papers.get(row["key"], {}).get("source_fulltext")
                                   or "cached")
                        row.update(extra)

                        # independent of the xml: the pdf often exists when the
                        # xml call fails, and it is what you open to read
                        if cfg["download"].get("pdf"):
                            pdf, nbytes = fetch_pdf(cfg, epmc, row)
                            if pdf:      # else keep a pdf the fallback saved
                                row["pdf_path"], row["pdf_bytes"] = pdf, nbytes
                                if status != "ok":
                                    # xml or pdf, either one is the paper
                                    status, src = "ok", "Europe PMC (PMC)"
                                    detail = f"pdf only, {int(nbytes) // 1024} KB"
                            stats.bump("pdf_saved" if row.get("pdf_path")
                                       else "pdf_failed")
                        if cfg["download"].get("fulltext") or row.get("pdf_path"):
                            stats.bump("fulltext_saved" if status == "ok"
                                       else "fulltext_failed")

                        row["source_fulltext"] = src
                        # in PMC is not the same thing as retrievable, and a
                        # TDM copy is a subscription copy, not an open one
                        row["is_open_access"] = ("yes" if status == "ok"
                                                 and "TDM" not in src else "no")
                        row["source_chain"] = source_chain(row, src)

                        if status == "ok" and cfg["download"].get("supplementary"):
                            sup, names = fetch_supplementary(cfg, epmc, row)
                            row["supplementary_path"] = sup
                            row["supplementary_files"] = names
                            stats.bump("supplementary_saved" if sup
                                       else "supplementary_failed")

                        entry = update_paper(cfg, papers, index, row, status,
                                             detail, method)
                        # "query_mesh" = the query found it, but no keyword is
                        # in the title or abstract, so MeSH is what caught it
                        by_keyword = not leaf_mesh or keyword_hit(row, terms)
                        tag_query(entry["key"], branch, leaf,
                                  "query" if by_keyword else "query_mesh", query)
                        for m_branch, m_leaf in slots_from_mesh(mesh_table, row):
                            tag(entry["key"], m_branch, m_leaf, "mesh")

                        bar.update(1)
                        bar.set_postfix(
                            saved=stats.counters.get("fulltext_saved", 0),
                            tags=len(edges))
                    if saves.due():
                        refresh_paper_branches(papers, edges)
                        save_state(cfg, papers, edges)
                bar.close()
        refresh_paper_branches(papers, edges)
        save_state(cfg, papers, edges)
        saves.due(force=True)
        sync(cfg)

    refresh_paper_branches(papers, edges)
    text, report, run_row = write_report(cfg, stats, papers, edges, throttles,
                                         todo, live)
    runs.append(run_row)
    save_state(cfg, papers, edges, runs)
    n_saved, n_missing, excel = write_excel(cfg, papers, edges, runs)
    n_leaf_files, n_branch_dirs = write_branch_manifests(cfg, papers, edges)
    n_stamped = stamp_branches_into_json(cfg, papers, edges, touched)
    n_linked = link_into_branches(cfg, papers, edges)
    write_atomic(cfg["_root"] / "_index" / "duplicates.csv",
                 csv_bytes(duplicate_report(papers, edges)))
    sync(cfg)

    print("\n" + text)
    print(f"excel     -> {excel}")
    print(f"report    -> {report}")
    print(f"manifests -> {n_leaf_files} leaf files in {n_branch_dirs} branch "
          f"folders under _index/branches/")
    print("duplicates-> _index/duplicates.csv")
    print(f"metadata  -> tags written back into {n_stamped} metadata.json")
    if cfg["_link_tree"]:
        print(f"tree      -> {n_linked} hardlinks under _index/tree/")


# ---------------------------------------------------------------- api check

def test_api(config_path):
    """Does the API actually answer? One command, five calls, a verdict."""
    cfg = load_config(config_path)
    ncbi = Throttle(cfg["limits"]["ncbi_per_second"])
    epmc = Throttle(cfg["limits"]["epmc_per_second"])
    results = []

    def record(name, ok, detail):
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:24} {detail}")

    print(f"API CHECK   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"email       {Entrez.email}")
    print(f"api key     {'set, 10 req/s' if Entrez.api_key else 'none, 3 req/s'}")
    print("-" * 74)

    for name, url in (("reach NCBI", EUTILS),
                      ("reach Europe PMC",
                       "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
                       "?query=test&format=json&pageSize=1")):
        try:
            resp = requests.get(url, timeout=30, headers=HEADERS)
            record(name, resp.status_code < 400, f"HTTP {resp.status_code}")
        except requests.exceptions.RequestException as exc:
            record(name, False, f"{type(exc).__name__}: {str(exc)[:70]}")

    live, _ = active_branches(cfg)
    if not live:
        print("\nno branch has terms - nothing to query")
        return
    branch, leaves = next(iter(live.items()))
    print(f"\n  {branch}: counting every leaf, and asking each one whether it")
    print(f"  claims PMID {BECKMAN_PMID} (Ferris 2013, the Beckman paper)")

    counts, claimed_by = {}, []
    try:
        for leaf, terms in leaves:
            year_tag, query = next(iter(query_plan(
                cfg, terms, query_mesh(cfg, branch, leaf))))
            counts[leaf] = count_only(ncbi, query)
            hit = count_only(ncbi, f"{BECKMAN_PMID}[uid] AND ({query})")
            if hit:
                claimed_by.append((leaf, query))
            print(f"    {leaf:22} {counts[leaf]:>6} match in {year_tag}"
                  f"   beckman: {'YES' if hit else '-'}")
        record("esearch", any(counts.values()),
               f"{len(counts)} leaves, {sum(counts.values())} matches in the "
               f"first year")
    except Exception as exc:
        record("esearch", False, f"{type(exc).__name__}: {str(exc)[:70]}")

    # the known answer, asked for by PMID rather than hoped for
    ids = []
    if claimed_by:
        leaf, query = claimed_by[0]
        try:
            ids, _ = search(cfg, ncbi, query, RunStats())
        except Exception:
            ids = []
        in_page = BECKMAN_PMID in ids
        record("known answer (Beckman)", True,
               f"claimed by {len(claimed_by)} leaf(s): "
               f"{', '.join(l for l, _ in claimed_by)} | inside the "
               f"{len(ids)} ids actually pulled from {leaf}: "
               f"{'yes' if in_page else 'NO'}")
        if not in_page:
            print("         -> query right, page too short: "
                  "set limits.max_per_query: null")
    else:
        record("known answer (Beckman)", False,
               f"no leaf matches PMID {BECKMAN_PMID} - the terms, the anchor "
               f"or the year window is wrong")

    row = None
    try:
        parsed = fetch_batch(cfg, ncbi, [BECKMAN_PMID], "api_check")
        row, err = parsed[0]
        record("efetch + parse", err is None and bool(row.get("title")),
               err or f"{row.get('pub_year')} {row.get('journal')[:28]} | "
                      f"pmcid {row.get('pmcid') or '-'} | doi {row.get('doi') or '-'}")
    except Exception as exc:
        record("efetch + parse", False, f"{type(exc).__name__}: {str(exc)[:70]}")

    if row and row.get("pmcid"):
        try:
            resp = epmc_get(epmc, row["pmcid"], "fullTextXML")
            ok = resp.status_code == 200 and resp.content.lstrip()[:5] == b"<?xml"
            record("Europe PMC fullTextXML", ok,
                   f"HTTP {resp.status_code}, {len(resp.content)} bytes")
        except requests.exceptions.RequestException as exc:
            record("Europe PMC fullTextXML", False, type(exc).__name__)
        try:
            resp = http_get(epmc, EPMC_PDF.format(pmcid=row["pmcid"]))
            ok = resp.status_code == 200 and resp.content.startswith(b"%PDF")
            record("Europe PMC pdf", ok,
                   f"HTTP {resp.status_code}, "
                   f"{'pdf' if resp.content.startswith(b'%PDF') else 'not a pdf'}, "
                   f"{len(resp.content)} bytes")
        except requests.exceptions.RequestException as exc:
            record("Europe PMC pdf", False, type(exc).__name__)
    else:
        record("Europe PMC", False, "no pmcid to test with")

    if cfg["download"].get("try_open_access", True):
        try:
            resp = requests.get(UNPAYWALL.format(doi="10.1038/nature12373"),
                                timeout=30, headers=HEADERS,
                                params={"email": Entrez.email})
            record("Unpaywall", resp.status_code == 200, f"HTTP {resp.status_code}")
        except requests.exceptions.RequestException as exc:
            record("Unpaywall", False, type(exc).__name__)

    bad = [n for n, ok, _ in results if not ok]
    print("-" * 74)
    print("ALL GOOD - the API answers, run it for real" if not bad
          else f"{len(bad)} check(s) failed: {', '.join(bad)}")
    if bad:
        print("  reach* failed      -> firewall, VPN or proxy, not the code")
        print("  esearch failed     -> query syntax or rate limit; paste the")
        print("                        --check query into pubmed.ncbi.nlm.nih.gov")
        print("  Europe PMC failed  -> that paper is in PMC but not open access")
        print("  Unpaywall failed   -> only the open-copy fallback is affected;")
        print("                        the run still works without it")


def check_only(config_path):
    """Validate the config and print the queries. Touches no network."""
    cfg = load_config(config_path)
    live, todo = active_branches(cfg)
    n_leaves = sum(len(v) for v in live.values())
    print(f"config      {Path(config_path).resolve()}")
    print(f"output dir  {cfg['_root']}")
    print(f"  exists?   {cfg['_root'].exists()}  writable parent? "
          f"{os.access(cfg['_root'].parent, os.W_OK)}")
    print(f"database    {cfg['_database']}   (a label for the ledger, "
          f"not a switch - this script only searches PubMed)")
    print(f"api key     {'set, 10 req/s' if Entrez.api_key else 'NOT set, 3 req/s'}")
    print(f"email       {Entrez.email}")
    keys = cfg["publisher_api"] or {}
    print(f"open access {'Unpaywall on' if cfg['download'].get('try_open_access', True) else 'OFF'}"
          f"   elsevier TDM {'set' if keys.get('elsevier') else '-'}"
          f"   wiley TDM {'set' if keys.get('wiley') else '-'}")
    on = use_mesh_in_query(cfg)
    print(f"mesh        {'IN the query, and used for tagging' if on else 'tagging only - search.include_mesh is false'}")
    dr = cfg["date_range"]
    per_year = bool(dr.get("split_by_year"))
    n_years = (dr["to"] - dr["from"] + 1) if per_year else 1
    print(f"years       {dr['from']}-{dr['to']}  "
          f"({'one query per year' if per_year else 'one query'})")
    print(f"branches    {len(cfg['branches'])} total, {len(live)} with terms, "
          f"{len(todo)} TODO")
    print(f"leaves      {n_leaves}   (a paper may repeat across leaves; "
          f"inside one leaf it appears once)")
    oa = 2 if cfg["download"].get("measure_open_access") else 1
    print(f"searches    ~{n_leaves * n_years * oa} per run, before paging")
    print(f"page cap    max_per_query={cfg['limits']['max_per_query']}, "
          f"page_size={cfg['limits']['page_size']}")
    print(f"link tree   {cfg['_link_tree']}")

    bad = [b for b in cfg["branches"] if not isinstance(b, str)]
    if bad:
        print(f"\n!! these branch keys are not text - quote them in the yaml: {bad}")

    print("\nQUERIES (paste one into pubmed.ncbi.nlm.nih.gov to check the count)")
    print("-" * 74)
    for branch, leaves in live.items():
        print(f"\n=== {branch} - {len(leaves)} leaf(s)")
        for leaf, terms in leaves:
            leaf_mesh = query_mesh(cfg, branch, leaf)
            year_tag, query = next(iter(query_plan(cfg, terms, leaf_mesh)))
            print(f"\n[{branch}/{leaf}]  {len(terms)} terms, {len(leaf_mesh)} mesh "
                  f"in the query, first year = {year_tag}")
            print(query)
        mesh = next((_mesh_list(spec) for name, spec in cfg["branches"].items()
                     if str(name) == branch), [])
        where = ("in the query above" if on and leaves[0][0] == "_all"
                 else "tagging only")
        print(f"\n  branch mesh ({where}): {', '.join(mesh) if mesh else '(none)'}")
    if todo:
        print(f"\nTODO, skipped this run: {', '.join(todo)}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    path = args[0] if args else "config.yaml"
    if "--test-api" in sys.argv:
        test_api(path)
    elif "--check" in sys.argv:
        check_only(path)
    else:
        main(path)
