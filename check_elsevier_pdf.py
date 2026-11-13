"""Check what Elsevier really returns for a PDF. Does not touch the crawler,
the ledger or any paper folder.

python check_elsevier_pdf.py config.yaml 10.1016/j.ophtha.2012.10.036
python check_elsevier_pdf.py config.yaml 10.1016/j.ophtha.2012.10.036 --save

It asks twice - with the key alone, then with the key + elsevier_insttoken
(only if you put one in the yaml) - and prints status, X-ELS-Status, size
and, if pypdf is installed, the page count. The key is never printed.
--save writes the answer to ./_elsevier_test/ (never into a paper folder).
Run it on the TMU network / VPN.
"""
import argparse
import os
import sys
from pathlib import Path

import requests
import yaml

URL = "https://api.elsevier.com/content/article/doi/{doi}"


def pages(blob):
    try:
        import io
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(blob)).pages)
    except Exception as exc:
        return f"? ({type(exc).__name__})"


def ask(doi, headers, label, save):
    print(f"[{label}]")
    try:
        resp = requests.get(URL.format(doi=doi), headers=headers, timeout=90)
    except requests.exceptions.RequestException as exc:
        print(f"  no answer: {type(exc).__name__} - off the TMU network/VPN, or Elsevier is down")
        return
    kind = resp.headers.get("Content-Type", "").split(";")[0]
    is_pdf = resp.content.startswith(b"%PDF")
    print(f"  HTTP {resp.status_code}  {kind}  {len(resp.content)} bytes  pdf={is_pdf}")
    status = resp.headers.get("X-ELS-Status", "-")
    print(f"  X-ELS-Status: {status}")
    if not is_pdf:
        # Elsevier explains a refusal in the body (bad key, no entitlement ...)
        print(f"  answer: {resp.text[:300].strip()}")
    if is_pdf and "warning" in status.lower():
        print("  -> first page only: this network is not entitled (the crawler now refuses it)")
    if is_pdf:
        print(f"  pages: {pages(resp.content)}")
        if save:
            out = Path("_elsevier_test")
            out.mkdir(exist_ok=True)
            target = out / (doi.replace("/", "_") + f"__{label.replace(' ', '_')}.pdf")
            target.write_bytes(resp.content)
            print(f"  saved -> {target}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("doi")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    keys = dict(cfg.get("publisher_api") or {})
    for name, env in (("elsevier", "ELSEVIER_API_KEY"),
                      ("elsevier_insttoken", "ELSEVIER_INSTTOKEN")):
        keys[name] = keys.get(name) or os.environ.get(env)
    if not keys.get("elsevier"):
        sys.exit("no Elsevier key: set publisher_api.elsevier in the yaml or ELSEVIER_API_KEY")
    base = {"X-ELS-APIKey": keys["elsevier"], "Accept": "application/pdf"}
    ask(args.doi, base, "key only", args.save)
    if keys.get("elsevier_insttoken"):
        ask(args.doi, {**base, "X-ELS-Insttoken": keys["elsevier_insttoken"]},
            "key + insttoken", args.save)
    else:
        print("[key + insttoken] skipped: elsevier_insttoken is null in the yaml")


if __name__ == "__main__":
    main()
