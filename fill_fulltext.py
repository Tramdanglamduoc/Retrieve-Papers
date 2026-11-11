"""Full-text filler for the AMD crawler ledger.

The crawler searches PubMed and takes what PMC gives. This script goes back
over the papers that are in the ledger and tries to complete them - xml AND
pdf - then rewrites every ledger and index file so they say what is really
on disk.

Routes, in order, for whatever a paper is still missing:
    1. _inbox/           files you downloaded by hand (browser, EndNote)
    2. PMC               Europe PMC xml, NCBI xml, Europe PMC pdf
    3. Unpaywall         every open pdf location, not only the first
    4. publisher TDM     Elsevier xml + pdf, Wiley pdf (needs publisher_api keys
                         in the yaml and the TMU network)
    5. publisher site    follows the DOI and takes the pdf the page itself
                         advertises. Works where the campus IP is enough
                         (Springer, BMJ ...). A site that asks "are you a
                         human" is left to a human: see --open.

Files land in the folder that already holds the paper's metadata.json.

A paper that is free but whose site refuses scripts gets its own status,
oa_blocked / needs_browser, with the link - so need_TMU only lists papers
that really need TMU.

_inbox naming: <pmid>.pdf, PMC<id>.pdf, or the DOI with "/" as "_"
(10.1167_iovs.13-12931.pdf). With pypdf installed, a pdf with any other name
is matched by the DOI printed inside it.

    python fill_fulltext.py config.yaml
    python fill_fulltext.py config.yaml --inbox-only     # no network
    python fill_fulltext.py config.yaml --skip-saved     # only unsaved papers
    python fill_fulltext.py config.yaml --limit 20       # a trial run
    python fill_fulltext.py config.yaml --open 10        # the by-hand loop:
        opens the next 10 unsaved papers in YOUR browser. You pass the human
        check and save each pdf into <output>/_inbox/ (any file name), then
    python fill_fulltext.py config.yaml --inbox-only     # files them

Every run OVERWRITES the live files - there is one of each, always current:
    _state/papers.csv, paper_branches.csv, queries.csv, runs.csv (+1 row),
    _state/run_report_latest.txt, _state/fulltext_fill_log.csv (+rows),
    _index/eye_disease_papers.xlsx, _index/branches/..., metadata.json
Kept out of the way, never needed for daily reading:
    _state/previous/    the four ledger csv as they were before the last run
    _state/history/     one dated report per run

Keep this file beside the crawler; it imports it.
"""

import argparse
import importlib
import importlib.util
import json
import re
import shutil
import time
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

CRAWLERS = ("pubmed_crawler_v6_1", "pubmed_crawler_v5")
TODO = {"not_in_pmc", "pmc_not_oa", "oa_blocked", "http_error",
        "network_blocked", "corrupt_file", "embargo", "metadata_only"}
BLOCKED = ("needs_browser", "free copy exists, the site refuses scripts - open the link")
MEANING = {
    "done": "full text on disk",
    "needs_browser": "free to read, site wants a human - --open, then _inbox/",
    "needs_tmu": "not open, script could not get it - --open, then _inbox/",
    "needs_request": "in PMC but closed - ask the lab to request it",
    "retry_auto": "a server did not answer - run again",
    "retry_later": "PMC embargo",
    "blocked_network": "host unreachable",
    "done_metadata": "full text was never requested",
    "needs_code_fix": "metadata could not be parsed",
}
LOG_COLUMNS = ["run_id", "paper_key", "pmid", "doi", "route", "result", "detail",
               "http_status", "content_type", "final_url", "bytes", "seconds", "attempts"]
# a route that is refused this many times in a row, without one success, is
# skipped for the rest of the run (and logged as skipped) instead of being
# hammered for every remaining paper
BREAKER = 5
RETRY_STATUS = {429, 500, 502, 503, 504}
# the plain "?pdf=render" page answers 403 to scripts; these are the endpoints
# behind it, tried in order
PMC_PDF = ("https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf",
           "https://europepmc.org/api/getPdf?pmcid={pmcid}",
           "https://europepmc.org/articles/{pmcid}?pdf=render")
PDF_META = re.compile(
    r"""<meta[^>]+name=["']citation_pdf_url["'][^>]+content=["']([^"']+)"""
    r"""|<meta[^>]+content=["']([^"']+)["'][^>]+name=["']citation_pdf_url["']""", re.I)


def load_crawler(name):
    names = [Path(name).stem] if name else CRAWLERS
    for candidate in names:
        try:
            return importlib.import_module(candidate)
        except ModuleNotFoundError as exc:
            if exc.name != candidate:
                raise
    raise SystemExit(f"crawler not found beside this file: {', '.join(names)}")


def squash(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def now():
    return datetime.now(timezone.utc)


class Fill:
    def __init__(self, c, cfg, args):
        self.c, self.cfg, self.args = c, cfg, args
        self.root = cfg["_root"]
        self.ncbi = c.Throttle(cfg["limits"]["ncbi_per_second"])
        self.web = c.Throttle(cfg["limits"]["epmc_per_second"])
        self.site = c.Throttle(1)            # publisher sites: one request a second
        self.keys = cfg.get("publisher_api") or {}
        self.started = now()
        self.log, self.routes = [], Counter()
        self.count = Counter()
        self.inbox_left = []
        self.flushed = 0                  # log rows already written to disk
        self.last = {}                    # facts about the last http answer
        self.refused_run = Counter()      # consecutive refusals per route key
        self.tries = max(1, int(cfg["limits"].get("max_attempts", 3) or 3))
        self.unpaywall_tries = max(1, int(cfg["limits"].get("unpaywall_locations", 4) or 4))

    # ------------------------------------------------------------ plumbing

    def note(self, entry, route, result, detail="", **facts):
        """facts: http_status, content_type, final_url, bytes, seconds, attempts.
        Never put keys, cookies or tokens in here."""
        row = {"run_id": self.cfg["_run_id"], "paper_key": entry["key"],
               "pmid": entry.get("pmid", ""), "doi": entry.get("doi", ""),
               "route": route, "result": result, "detail": str(detail)[:300]}
        row.update({k: facts.get(k, "") for k in LOG_COLUMNS[7:]})
        self.log.append(row)

    def flush_log(self, path):
        """Append the rows not yet on disk; safe to call as often as you like."""
        fresh = self.log[self.flushed:]
        if not fresh:
            return
        new = pd.DataFrame(fresh, columns=LOG_COLUMNS)
        if path.exists():
            old = pd.read_csv(path, dtype=str).fillna("")
            new = pd.concat([old, new], ignore_index=True)
        new = new.reindex(columns=LOG_COLUMNS).fillna("")
        self.c.write_atomic(path, new.to_csv(index=False).encode("utf-8"))
        self.flushed = len(self.log)

    def get(self, url, headers=None, params=None, timeout=90):
        """One request. Timeouts, connection errors and 429/5xx are retried
        with a back-off (Retry-After honoured); 401/403/404 are not - asking
        again with the same identity gives the same answer."""
        resp, started = None, time.monotonic()
        for attempt in range(1, self.tries + 1):
            self.web.wait()
            try:
                resp = requests.get(url, timeout=timeout, allow_redirects=True,
                                    params=params,
                                    headers={**self.c.HEADERS, **(headers or {})})
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == self.tries:
                    raise
                time.sleep(2 ** attempt)
                continue
            if resp.status_code not in RETRY_STATUS or attempt == self.tries:
                break
            wait = resp.headers.get("Retry-After", "")
            time.sleep(min(int(wait), 60) if wait.isdigit() else 2 ** attempt)
        self.last = {"http_status": resp.status_code,
                     "content_type": resp.headers.get("Content-Type", "").split(";")[0],
                     "final_url": resp.url, "bytes": len(resp.content),
                     "seconds": round(time.monotonic() - started, 2),
                     "attempts": attempt}
        return resp

    def save(self, target, content):
        self.c.write_atomic(target, content)
        if self.c.validate_file(target)[0]:
            return True
        target.unlink(missing_ok=True)
        return False

    def try_pdf(self, entry, route, url, pdf, headers=None):
        try:
            resp = self.get(url, headers)
        except requests.exceptions.RequestException as exc:
            self.note(entry, route, "no answer", f"{type(exc).__name__} {url}")
            return False
        facts = dict(self.last)
        if resp.status_code == 200 and resp.content.startswith(b"%PDF"):
            if "warning" in resp.headers.get("X-ELS-Status", "").lower():
                # Elsevier answers 200 with page one only when the network
                # is not entitled; that is not the paper
                self.note(entry, route, "not entitled", "first page only", **facts)
                return False
            if self.save(pdf, resp.content):
                self.note(entry, route, "saved pdf", url, **facts)
                return True
            self.note(entry, route, "invalid_file",
                      f"bytes were a pdf but validate_file refused them {url}", **facts)
            return False
        kind = resp.headers.get("Content-Type", "").split(";")[0]
        # 200 + html where a pdf was promised is a landing / check page
        result = "unexpected_html" if resp.status_code == 200 and "html" in kind else "refused"
        self.note(entry, route, result, f"HTTP {resp.status_code} {kind} {url}", **facts)
        return False

    # ------------------------------------------------------------ inbox

    def scan_inbox(self, papers):
        inbox = self.root / "_inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        by_doi, by_pmid, by_pmcid, by_tail, plain = {}, {}, {}, {}, {}
        for key, e in papers.items():
            if e.get("doi"):
                doi = self.c.normalize_doi(e["doi"])
                by_doi[squash(doi)] = key
                plain[doi] = key
                # publishers name the file after the part behind the slash
                tail = squash(doi.split("/", 1)[-1])
                if len(tail) >= 6:
                    by_tail[tail] = None if tail in by_tail else key
            if e.get("pmid"):
                by_pmid[str(e["pmid"])] = key
            if e.get("pmcid"):
                by_pmcid[e["pmcid"].upper()] = key

        def match(path):
            stem = path.stem
            name = squash(re.sub(r"\s*\(\d+\)$", "", stem))     # "x (1).pdf"
            if name in by_doi:
                return by_doi[name]
            if by_tail.get(name):
                return by_tail[name]
            for pmc in re.findall(r"PMC\d+", stem.upper()):
                if pmc in by_pmcid:
                    return by_pmcid[pmc]
            for digits in re.findall(r"\d{5,9}", stem):
                if digits in by_pmid:
                    return by_pmid[digits]
            if path.suffix.lower() == ".pdf":
                # the paper's own DOI is printed before any DOI it cites
                text = pdf_text(path)
                hits = [(text.find(doi), key) for doi, key in plain.items()
                        if doi in text]
                if hits:
                    return min(hits)[1]
            return None

        found = defaultdict(list)
        for path in sorted(inbox.iterdir()):
            if path.suffix.lower() not in (".pdf", ".xml"):
                continue
            key = match(path)
            if key:
                found[key].append(path)
            else:
                self.inbox_left.append(f"{path.name} - no paper in the ledger matches")
        return found

    def take_inbox(self, entry, files, xml, pdf):
        got = False
        for path in files:
            target = xml if path.suffix.lower() == ".xml" else pdf
            ok, why = self.c.validate_file(path)
            if not ok:
                self.inbox_left.append(f"{path.name} - {why}")
                self.note(entry, "inbox", "rejected", f"{path.name}: {why}")
            elif target.exists():
                self.inbox_left.append(f"{path.name} - {target.name} already saved")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
                self.note(entry, "inbox", f"saved {target.suffix[1:]}", path.name)
                self.count["inbox_placed"] += 1
                got = True
        return got

    # ------------------------------------------------------------ routes

    def pmc_xml(self, entry, xml):
        c, pmcid = self.c, entry["pmcid"]
        try:
            resp = c.epmc_get(self.web, pmcid, "fullTextXML")
        except requests.exceptions.RequestException as exc:
            self.note(entry, "Europe PMC xml", "no answer", type(exc).__name__)
            return "", True
        if resp.status_code == 200 and self.save(xml, resp.content):
            self.note(entry, "Europe PMC xml", "saved xml")
            return "Europe PMC (PMC)", False
        raw = c.ncbi_pmc_xml(self.ncbi, pmcid)
        if raw and self.save(xml, raw):
            self.note(entry, "NCBI PMC xml", "saved xml")
            return "NCBI efetch (pmc)", False
        self.note(entry, "PMC xml", "no answer" if raw is None else "refused",
                  f"Europe PMC HTTP {resp.status_code}")
        return "", raw is None

    def pmc_pdf(self, entry, pdf):
        for url in PMC_PDF:
            key = "pmc_pdf:" + url.split("?")[0]
            if self.refused_run[key] >= BREAKER:
                self.note(entry, "Europe PMC pdf", "skipped",
                          f"{self.refused_run[key]} refusals in a row this run, not retried {url.split('?')[0]}")
                continue
            if self.try_pdf(entry, "Europe PMC pdf", url.format(pmcid=entry["pmcid"]), pdf):
                self.refused_run[key] = 0
                return True
            self.refused_run[key] += 1
        return False

    def publisher_site(self, entry, doi, pdf):
        """The DOI's own page, read the way Google Scholar reads it: the
        citation_pdf_url tag. Entitlement is the network you are on. Returns
        "" when saved, otherwise why not."""
        route = "publisher site"
        try:
            self.site.wait()
            page = self.get(f"https://doi.org/{doi}", {"Accept": "text/html"})
        except requests.exceptions.RequestException as exc:
            self.note(entry, route, "no answer", type(exc).__name__)
            return "publisher site did not answer"
        host = page.url.split("/")[2] if "//" in page.url else ""
        if page.content.startswith(b"%PDF") and self.save(pdf, page.content):
            self.note(entry, route, "saved pdf", page.url)
            return ""
        found = PDF_META.search(page.text) if page.status_code == 200 else None
        if not found:
            why = (f"{host} refused the script (HTTP {page.status_code}, often a human check)"
                   if page.status_code in (401, 403, 429, 503)
                   else f"{host} page names no pdf")
            self.note(entry, route, "refused", f"{why} {page.url}", **self.last)
            return why
        url = requests.compat.urljoin(page.url, (found.group(1) or found.group(2))
                                      .replace("&amp;", "&"))
        self.site.wait()
        if self.try_pdf(entry, route, url, pdf, {"Referer": page.url}):
            return ""
        return f"{host} gave a page, not the pdf - no access from this network, or a human check"

    def unpaywall(self, entry, doi):
        """(pdf urls, a link a person can open, oa_status); None = no answer."""
        self.web.wait()
        try:
            resp = requests.get(self.c.UNPAYWALL.format(doi=doi), timeout=40,
                                headers=self.c.HEADERS,
                                params={"email": self.c.Entrez.email})
            if resp.status_code == 404:
                return [], "", "unknown to Unpaywall"
            data = resp.json() if resp.status_code == 200 else None
        except (requests.exceptions.RequestException, ValueError):
            data = None
        if not isinstance(data, dict):
            self.note(entry, "Unpaywall", "no answer")
            return None
        places = [data.get("best_oa_location") or {}] + list(data.get("oa_locations") or [])
        places = [p for p in places if p]
        pdfs = list(dict.fromkeys(
            (p["url_for_pdf"], p.get("host_type") or "oa")
            for p in places if p.get("url_for_pdf")))
        human = next((p.get("url_for_landing_page") or p.get("url")
                      for p in places if p.get("url_for_landing_page") or p.get("url")), "")
        return pdfs, human if data.get("is_oa") else "", data.get("oa_status") or "closed"

    def elsevier(self, entry, doi, xml, pdf):
        got = []
        auth = {"X-ELS-APIKey": self.keys["elsevier"]}
        if self.keys.get("elsevier_insttoken"):
            auth["X-ELS-Insttoken"] = self.keys["elsevier_insttoken"]
        url = self.c.ELSEVIER.format(doi=doi)
        if not xml.exists():
            try:
                resp = self.get(url, {**auth, "Accept": "text/xml"}, {"view": "FULL"})
                # an unentitled answer is the abstract wrapped in the same envelope
                body = b"ce:sections" in resp.content or b"xocs:rawtext" in resp.content
                if resp.status_code == 200 and body and self.save(xml, resp.content):
                    self.note(entry, "Elsevier TDM", "saved xml")
                    got.append("xml")
                else:
                    self.note(entry, "Elsevier TDM", "not entitled",
                              f"xml HTTP {resp.status_code}")
            except requests.exceptions.RequestException as exc:
                self.note(entry, "Elsevier TDM", "no answer", type(exc).__name__)
        if not pdf.exists() and self.try_pdf(
                entry, "Elsevier TDM", url, pdf, {**auth, "Accept": "application/pdf"}):
            got.append("pdf")
        return got

    # ------------------------------------------------------------ one paper

    def fill(self, entry, inbox_files):
        c = self.c
        folder = self.root / entry["paper_folder"]
        xml, pdf = folder / "fulltext.xml", folder / "fulltext.pdf"
        for path in (xml, pdf):
            if path.exists() and not c.validate_file(path)[0]:
                path.unlink()
        had = (xml.exists(), pdf.exists())
        was_ok = entry.get("status") == "ok"
        sources, open_link, oa_status, unanswered, site_why = [], "", "", False, ""

        if inbox_files and self.take_inbox(entry, inbox_files, xml, pdf):
            sources.append("manual download")

        pmcid = entry.get("pmcid")
        doi = c.normalize_doi(entry.get("doi"))
        if not self.args.inbox_only:
            if pmcid and not xml.exists():
                src, silent = self.pmc_xml(entry, xml)
                unanswered |= silent
                if src:
                    sources.append(src)
            if pmcid and not pdf.exists() and self.pmc_pdf(entry, pdf):
                sources.append("Europe PMC (PMC)")

            if doi and not pdf.exists():
                answer = self.unpaywall(entry, doi)
                if answer is None:
                    unanswered = True
                else:
                    pdfs, open_link, oa_status = answer
                    if not pdfs:
                        self.note(entry, "Unpaywall", "no pdf url",
                                  f"oa_status={oa_status}; open link: {open_link or '-'}")
                    elif len(pdfs) > self.unpaywall_tries:
                        self.note(entry, "Unpaywall", "locations not tried",
                                  f"{len(pdfs)} pdf locations, trying {self.unpaywall_tries} "
                                  "(limits.unpaywall_locations)")
                    for url, host in pdfs[:self.unpaywall_tries]:
                        if self.try_pdf(entry, f"Unpaywall ({host})", url, pdf):
                            sources.append(f"Unpaywall ({host})")
                            break
                    else:
                        open_link = (pdfs[0][0] if pdfs else open_link)

            vendor = c.TDM_PREFIXES.get(doi.split("/")[0]) if doi else None
            if vendor == "elsevier" and not self.keys.get("elsevier"):
                self.note(entry, "Elsevier TDM", "skipped", "no publisher_api.elsevier key in the yaml")
            elif vendor == "wiley" and not self.keys.get("wiley") and not pdf.exists():
                self.note(entry, "Wiley TDM", "skipped", "no publisher_api.wiley key in the yaml")
            if vendor == "elsevier" and self.keys.get("elsevier"):
                if self.elsevier(entry, doi, xml, pdf):
                    sources.append("Elsevier TDM API")
            elif vendor == "wiley" and self.keys.get("wiley") and not pdf.exists():
                if self.try_pdf(entry, "Wiley TDM",
                                c.WILEY.format(doi=doi.replace("/", "%2F")), pdf,
                                {"Wiley-TDM-Client-Token": self.keys["wiley"],
                                 "Accept": "application/pdf"}):
                    sources.append("Wiley TDM API")

            if doi and not pdf.exists() and self.args.no_publisher:
                self.note(entry, "publisher site", "skipped", "--no-publisher")
            if doi and not pdf.exists() and not self.args.no_publisher:
                if vendor == "elsevier":
                    # ScienceDirect serves scripts only through its API
                    site_why = ("Elsevier: needs publisher_api.elsevier in the yaml"
                                if not self.keys.get("elsevier") else
                                "Elsevier API: not entitled from this network")
                else:
                    site_why = self.publisher_site(entry, doi, pdf)
                    if not site_why:
                        sources.append("publisher site (TMU network)")

        has = (xml.exists(), pdf.exists())
        before = (entry.get("status"), entry.get("action"), entry.get("detail"))
        if has[0]:
            entry.update({"fulltext_path": c.rel(self.cfg, xml),
                          "fulltext_bytes": str(xml.stat().st_size),
                          "fulltext_sha256": c.sha256_of(xml)})
        if has[1]:
            entry.update({"pdf_path": c.rel(self.cfg, pdf),
                          "pdf_bytes": str(pdf.stat().st_size)})
        self.count["xml_new"] += has[0] and not had[0]
        self.count["pdf_new"] += has[1] and not had[1]
        for s in dict.fromkeys(sources):
            self.routes[s] += 1

        if any(has):
            if has != had or not was_ok:
                old = [s for s in (entry.get("source_fulltext") or "").split(" + ")
                       if s and s != "cached"] if was_ok else []
                merged = list(dict.fromkeys(old + sources)) or ["cached"]
                closed = all("TDM" in s or "TMU" in s or "manual" in s for s in merged)
                entry.update({
                    "status": "ok", "action": "done",
                    "detail": " + ".join(n for n, on in zip(("xml", "pdf"), has) if on),
                    "source_fulltext": " + ".join(merged),
                    "source_chain": f"{entry.get('databases') or 'PubMed'} -> "
                                    f"{' + '.join(merged)}",
                    "is_open_access": "no" if closed else "yes", "attempts": "0"})
                self.count["newly_saved"] += not was_ok
        elif unanswered or self.args.inbox_only:
            pass          # nothing learned, so the ledger keeps what it said
        elif open_link:
            entry.update({"status": "oa_blocked", "action": BLOCKED[0],
                          "detail": f"free ({oa_status}), site refuses scripts - open: {open_link}"})
        elif pmcid:
            entry.update({"status": "pmc_not_oa", "action": "needs_request",
                          "detail": "in PMC; the script could not fetch a copy "
                                    f"(Unpaywall: {oa_status or 'no doi'}) - "
                                    "not proof it is closed, try --open"})
        else:
            entry.update({"status": "not_in_pmc", "action": "needs_tmu",
                          "detail": f"not open (Unpaywall: {oa_status or 'no doi'}); "
                                    f"{site_why or 'publisher site not tried'} "
                                    "- by hand: --open, then _inbox/"})

        if has != had or before != (entry["status"], entry["action"], entry["detail"]):
            stamp = now().strftime("%Y-%m-%d %H:%M")
            entry["last_run"], entry["last_attempt"] = self.cfg["_run_id"], stamp
            self.stamp_metadata(entry, has, stamp)

    def stamp_metadata(self, entry, has, stamp):
        path = self.root / (entry.get("metadata_path") or "")
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        data["fulltext"] = {"status": entry["status"], "action": entry["action"],
                            "xml": bool(has[0]), "pdf": bool(has[1]),
                            "source": entry.get("source_fulltext", ""),
                            "detail": entry.get("detail", ""),
                            "updated": stamp, "run_id": self.cfg["_run_id"]}
        for col in ("pmcid", "pmc_url"):
            if entry.get(col):
                data[col] = entry[col]
        self.c.write_atomic(path, json.dumps(data, ensure_ascii=False,
                                             indent=2).encode("utf-8"))


def pdf_text(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
        text = " ".join(str(v) for v in (reader.metadata or {}).values())
        for page in reader.pages[:2]:
            text += " " + (page.extract_text() or "")
    except Exception:
        return ""
    return text.lower()


def open_in_browser(c, cfg, papers, n):
    """The by-hand loop. A person passes the human check; the script only
    opens the tabs and remembers which papers it has already shown."""
    log_path = c.state_path(cfg, "fulltext_fill_log.csv")
    log = (pd.read_csv(log_path, dtype=str).fillna("") if log_path.exists()
           else pd.DataFrame(columns=LOG_COLUMNS))
    shown = set(log[log["route"] == "browser"]["paper_key"])
    def lacks_pdf(e):
        # not saved at all, or saved as xml only: a pdf is still wanted
        folder = e.get("paper_folder")
        return e.get("status") != "ok" or not (
            folder and (cfg["_root"] / folder / "fulltext.pdf").is_file())
    todo = [e for e in papers.values() if lacks_pdf(e)]
    todo.sort(key=lambda e: e["key"] in shown)           # not yet shown first
    rows = []
    for e in todo[:n]:
        link = re.search(r"open: (\S+)", e.get("detail", ""))
        url = link.group(1) if link else e.get("doi_url") or e.get("pubmed_url")
        if not url:
            continue
        webbrowser.open_new_tab(url)
        time.sleep(1)
        rows.append({"run_id": cfg["_run_id"], "paper_key": e["key"],
                     "pmid": e.get("pmid", ""), "doi": e.get("doi", ""),
                     "route": "browser", "result": "opened", "detail": url})
        print(f"  {e.get('pmid') or e['key']:>12}  {url}")
    c.write_atomic(log_path, pd.concat([log, pd.DataFrame(rows, columns=LOG_COLUMNS)])
                   .to_csv(index=False).encode("utf-8"))
    left = sum(1 for e in todo if e["key"] not in shown) - len(rows)
    print(f"\nopened {len(rows)} | not saved {len(todo)} | never shown yet {max(left, 0)}")
    print(f"save each pdf into {cfg['_root'] / '_inbox'}  (any file name), then run")
    print("    python fill_fulltext.py <config> --inbox-only")
    if importlib.util.find_spec("pypdf") is None:
        print("pip install pypdf  - without it a pdf is only recognised when its "
              "file name is the PMID or the DOI")


# ---------------------------------------------------------------- outputs

def backup_state(c, cfg):
    """One undo level, always in the same folder, so it never piles up."""
    dest = c.state_path(cfg, "previous")
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("papers.csv", "paper_branches.csv", "runs.csv", "queries.csv",
                 "fulltext_fill_log.csv"):
        src = c.state_path(cfg, name)
        if src.exists():
            shutil.copy2(src, dest / name)
    return dest


def tidy_state(c, cfg):
    """_state holds the live files only. Dated reports, and the per-run
    backup folders an earlier version of this script left, go to history/."""
    state, history = c.state_path(cfg, ""), c.state_path(cfg, "history")
    old = list(state.glob("run_report_r*.txt")) + list(state.glob("backup_r*"))
    if old:
        history.mkdir(parents=True, exist_ok=True)
    for path in old:
        target = history / path.name
        if not target.exists():
            shutil.move(str(path), str(target))
    return len(old)


def refresh_flat_manifests(c, cfg, papers):
    """v5 wrote _index/branches/<branch>.csv; v6 writes a folder per branch.
    Whichever is on disk, its status columns must not go stale."""
    n = 0
    for path in (cfg["_root"] / "_index" / "branches").glob("*.csv"):
        df = pd.read_csv(path, dtype=str).fillna("")
        if "paper_key" not in df.columns:
            continue
        for col in ("status", "action", "fulltext_path", "pdf_path"):
            if col in df.columns:
                df[col] = [papers.get(k, {}).get(col, "") for k in df["paper_key"]]
        c.write_atomic(path, df.to_csv(index=False).encode("utf-8"))
        n += 1
    return n


def write_excel(c, cfg, log, papers, edges, runs):
    _, _, out = c.write_excel(cfg, papers, edges, runs)
    df = pd.DataFrame(list(papers.values())).reindex(columns=c.PAPER_COLUMNS).fillna("")
    blocked = df[df["action"] == BLOCKED[0]]
    with pd.ExcelWriter(out, engine="openpyxl", mode="a",
                        if_sheet_exists="replace") as writer:
        blocked.to_excel(writer, sheet_name="free_but_blocked", index=False)
        log.to_excel(writer, sheet_name="fill_log", index=False)
        c.linkify(writer.book["free_but_blocked"])
    return out


def write_report(c, cfg, run, papers, tried, inbox_seen):
    finished = now()
    actions = Counter(e.get("action", "") for e in papers.values())
    missing = [e for e in papers.values() if e.get("status") != "ok"]
    both = sum(1 for e in papers.values()
               if e.get("fulltext_path") and e.get("pdf_path"))
    xml_only = sum(1 for e in papers.values()
                   if e.get("fulltext_path") and not e.get("pdf_path"))
    pdf_only = sum(1 for e in papers.values()
                   if e.get("pdf_path") and not e.get("fulltext_path"))
    L = [
        "AMD knowledge-framework - full-text fill report", "=" * 74,
        f"run_id      {cfg['_run_id']}",
        f"crawler     {c.__name__}",
        f"mode        {'inbox only, no network' if run.args.inbox_only else 'inbox + PMC + Unpaywall + publisher TDM'}",
        f"TDM keys    elsevier {'set' if run.keys.get('elsevier') else '-'}"
        f"   wiley {'set' if run.keys.get('wiley') else '-'}",
        f"started     {run.started:%Y-%m-%d %H:%M} UTC",
        f"finished    {finished:%Y-%m-%d %H:%M} UTC",
        f"output      {cfg['_root']}", "",
        "THIS RUN", "-" * 74,
        f"{'papers tried':34} {len(tried):>8}",
        f"{'moved to saved':34} {run.count['newly_saved']:>8}",
        f"{'new fulltext.xml':34} {run.count['xml_new']:>8}",
        f"{'new fulltext.pdf':34} {run.count['pdf_new']:>8}",
        "", "WHERE THE NEW FILES CAME FROM", "-" * 74,
    ]
    L += [f"{name:34} {n:>8}" for name, n in run.routes.most_common()] or ["(nothing new)"]

    L += ["", "LEDGER NOW", "-" * 74,
          f"{'papers':34} {len(papers):>8}",
          f"{'saved (xml or pdf)':34} {len(papers) - len(missing):>8}",
          f"{'  with both xml and pdf':34} {both:>8}",
          f"{'not saved':34} {len(missing):>8}",
        f"{'saved, xml only (pdf missing)':34} {xml_only:>8}",
        f"{'saved, pdf only (xml missing)':34} {pdf_only:>8}",
          "", "BY WHAT HAPPENS NEXT", "-" * 74]
    for action, n in actions.most_common():
        L.append(f"{action or '(blank)':18} {n:>6}   {MEANING.get(action, '')}")

    for action, title in ((BLOCKED[0], "FREE BUT BLOCKED, by publisher"),
                          ("needs_tmu", "NEEDS TMU, by publisher")):
        pub = Counter(e.get("publisher") or "unknown" for e in papers.values()
                      if e.get("action") == action)
        if pub:
            L += ["", title, "-" * 74]
            L += [f"{name:34} {n:>8}" for name, n in pub.most_common(15)]

    L += ["", "_inbox", "-" * 74,
          f"{'files seen':34} {inbox_seen:>8}",
          f"{'filed into paper folders':34} {run.count['inbox_placed']:>8}"]
    if run.inbox_left:
        L += ["left in _inbox:"] + [f"  {x}" for x in run.inbox_left]

    L += ["", "API CALLS", "-" * 74,
          f"{'NCBI':34} {run.ncbi.calls:>8}",
          f"{'Europe PMC, Unpaywall, publishers':34} {run.web.calls:>8}"]

    why = Counter(re.sub(r"^.*?\); | - by hand.*$", "", e.get("detail", ""))
                  for e in papers.values() if e.get("action") == "needs_tmu")
    if why:
        L += ["", "NEEDS TMU, by what stopped the script", "-" * 74]
        L += [f"{n:>5}  {reason}" for reason, n in why.most_common(20)]

    text = "\n".join(L) + "\n"
    out = c.state_path(cfg, "history") / f"run_report_{cfg['_run_id']}.txt"
    c.write_atomic(out, text.encode("utf-8"))
    c.write_atomic(c.state_path(cfg, "run_report_latest.txt"), text.encode("utf-8"))
    row = {"run_id": cfg["_run_id"], "started": f"{run.started:%Y-%m-%d %H:%M}",
           "finished": f"{finished:%Y-%m-%d %H:%M}", "database": "fulltext-fill",
           "config_hash": cfg["_config_hash"],
           "retrieved": len(tried), "new": run.count["newly_saved"],
           "fulltext_saved": run.count["xml_new"], "pdf_saved": run.count["pdf_new"],
           "failed": len(missing), "warnings": len(run.inbox_left)}
    return text, out, row


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("config", nargs="?", default="config.yaml")
    ap.add_argument("--crawler", help="module to import, default: newest found")
    ap.add_argument("--inbox-only", action="store_true", help="file _inbox/, no network")
    ap.add_argument("--skip-saved", action="store_true",
                    help="leave saved papers alone, even with xml or pdf missing")
    ap.add_argument("--limit", type=int, default=0, help="stop after N papers")
    ap.add_argument("--no-publisher", action="store_true",
                    help="do not follow DOIs to publisher sites")
    ap.add_argument("--open", type=int, default=0, metavar="N",
                    help="open the next N unsaved papers in your browser, then stop")
    args = ap.parse_args()

    c = load_crawler(args.crawler)
    cfg = c.load_config(args.config)
    c.ACTIONS["oa_blocked"] = BLOCKED
    if not c.state_path(cfg, "papers.csv").exists():
        raise SystemExit(f"no ledger in {cfg['_root']} - run the crawler first")

    papers, edges, runs = c.load_state(cfg)
    if args.open:
        return open_in_browser(c, cfg, papers, args.open)
    backup = backup_state(c, cfg)
    run = Fill(c, cfg, args)
    inbox = run.scan_inbox(papers)
    inbox_seen = sum(len(v) for v in inbox.values()) + len(run.inbox_left)

    def wanted(e):
        if not e.get("paper_folder"):
            return False
        if e["key"] in inbox:
            return True
        if args.inbox_only:
            return False
        if e.get("status") in TODO:
            return True
        return (e.get("status") == "ok" and not args.skip_saved
                and not (e.get("fulltext_path") and e.get("pdf_path")))

    tried = [e for e in papers.values() if wanted(e)]
    # unsaved first, so a --limit or an interrupted run spends itself on them
    tried.sort(key=lambda e: (e["key"] not in inbox, e.get("status") == "ok"))
    if args.limit:
        tried = tried[:args.limit]

    print(f"run_id {cfg['_run_id']} | crawler {c.__name__}")
    print(f"output {cfg['_root']}")
    print(f"ledger {len(papers)} papers | to try {len(tried)} "
          f"({sum(e.get('status') != 'ok' for e in tried)} not saved) "
          f"| _inbox {inbox_seen} file(s)")
    print(f"undo   {backup}  (the ledger as it was before this run)")

    if not args.inbox_only:
        blank = [e for e in tried if not e.get("pmcid") and e.get("pmid")]
        for i in range(0, len(blank), 200):
            run.count["pmcid_recovered"] += c.fill_pmcids(run.ncbi, blank[i:i + 200])

    clock = c.SaveClock()
    log_path = c.state_path(cfg, "fulltext_fill_log.csv")
    try:
        for entry in tqdm(tried, unit="paper", desc="filling"):
            run.fill(entry, inbox.get(entry["key"], []))
            if clock.due():
                c.save_state(cfg, papers, edges)
                run.flush_log(log_path)
    finally:                      # Ctrl-C or a crash must not lose the log
        run.flush_log(log_path)

    c.refresh_paper_branches(papers, edges)
    text, report, row = write_report(c, cfg, run, papers, tried, inbox_seen)
    runs.append(row)
    c.save_state(cfg, papers, edges, runs)
    run.flush_log(log_path)
    log = pd.read_csv(log_path, dtype=str).fillna("")

    excel = write_excel(c, cfg, log, papers, edges, runs)
    c.write_branch_manifests(cfg, papers, edges)
    flat = refresh_flat_manifests(c, cfg, papers)
    moved = tidy_state(c, cfg)
    c.sync(cfg)

    print("\n" + text)
    print(f"excel     -> {excel}")
    print(f"report    -> {c.state_path(cfg, 'run_report_latest.txt')}")
    print(f"            a dated copy is in {report.parent}")
    print(f"log       -> {log_path}")
    if moved:
        print(f"tidied    -> {moved} old dated report(s) / backup folder(s) moved "
              f"to {report.parent}")
    print("manifests -> _index/branches/ rewritten"
          + (f", {flat} flat v5 file(s) refreshed" if flat else ""))


if __name__ == "__main__":
    main()
