"""Download the official 2026 eCQM specifications and split them into labeled sections.

The specs are copyrighted by their measure stewards, and their terms differ:

  - NCQA (CMS122, CMS131), 2026 notice: the measure may be used for internal,
    noncommercial purposes without approval; all other uses must be approved by NCQA.
  - National Kidney Foundation (CMS951): the measures may be reproduced and distributed,
    without modification, for noncommercial purposes.

So this module:

  - keeps every section's text exactly as published (only HTML entities are decoded),
  - stores each measure's own Copyright and Disclaimer next to its sections,
  - saves everything under data/cache/ (gitignored), so this repo never contains or
    redistributes spec text. Each person downloads it from eCQI for their own use,
  - marks which measures' text may be shown on a public site (PUBLIC_TEXT_OK). For the
    others, a public page links to the eCQI source instead of showing the text.

Each measure's notice is printed with any text it shows, so check the current notice
after every fetch; stewards update their terms between versions.

Usage (from the repo root; needs internet access to ecqi.healthit.gov):
    python3 -m him_workbench.care_gaps.spec_loader fetch
    python3 -m him_workbench.care_gaps.spec_loader show CMS122 Numerator
"""
import argparse
import html
import json
import re
import sys
import urllib.request
from pathlib import Path

BASE = "https://ecqi.healthit.gov/sites/default/files/ecqm/measures/"
SPECS = {
    "CMS122": "CMS122-v14.0.000-QDM.html",
    "CMS131": "CMS131-v14.0.000-QDM.html",
    "CMS951": "CMS951-v4.0.000-QDM.html",
}
# Measures whose spec text may appear on a public site, based on each steward's 2026 notice.
# NCQA's notice covers internal, noncommercial use only, so CMS122 and CMS131 are left out.
PUBLIC_TEXT_OK = {"CMS951"}

# Rows kept from each spec's header table. Others (references, data elements, the
# measure logic itself) aren't needed to explain a gap.
SECTIONS = ("Description", "Initial Population", "Denominator", "Denominator Exclusions",
            "Numerator", "Numerator Exclusions", "Denominator Exceptions", "Guidance", "Definition")
META = ("eCQM Title", "CMS ID", "eCQM Version Number", "Measurement Period", "Measure Steward",
        "Copyright", "Disclaimer")

REPO = Path(__file__).resolve().parents[3]
CACHE = REPO / "data" / "cache" / "ecqm"
PARSED = CACHE / "specs.json"

_ROW = re.compile(r'<span class="td_label">(.*?)</span>\s*</th>\s*<td[^>]*>(.*?)</td>', re.S)
_PRE = re.compile(r"<pre>(.*?)</pre>", re.S)
_TAG = re.compile(r"<[^>]+>")


class SpecError(RuntimeError):
    """A spec could not be downloaded or did not have the expected layout. The message says which."""


def _cell_text(cell):
    """Text of a value cell, unchanged apart from decoding HTML entities and trimming the ends."""
    pres = _PRE.findall(cell)
    raw = "\n\n".join(pres) if pres else _TAG.sub("", cell)
    return html.unescape(raw).strip()


def parse_spec(page, measure, url):
    """Pull the labeled rows out of one spec page. Repeated labels keep their first value."""
    rows = {}
    for label, cell in _ROW.findall(page):
        label = html.unescape(_TAG.sub("", label)).strip()
        rows.setdefault(label, _cell_text(cell))
    missing = [k for k in ("Copyright", "Disclaimer", "Numerator", "Initial Population") if k not in rows]
    if missing:
        raise SpecError(f"{measure}: page layout changed, missing rows: {', '.join(missing)}")
    return {
        "measure": measure,
        "url": url,
        "meta": {k: rows.get(k, "") for k in META},
        "sections": {k: rows[k] for k in SECTIONS if k in rows},
    }


def fetch(timeout=60, only=None):
    """Download the specs (all three, or just `only`), save raw pages and parsed sections under data/cache/ecqm."""
    CACHE.mkdir(parents=True, exist_ok=True)
    parsed = {}
    for measure, filename in SPECS.items():
        if only and measure not in only:
            continue
        url = BASE + filename
        request = urllib.request.Request(url, headers={"User-Agent": "him-workbench (noncommercial portfolio)"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310: fixed https URL
                page = response.read().decode("utf-8", errors="replace")
        except OSError as exc:
            raise SpecError(f"{measure}: could not download {url}: {exc}") from exc
        (CACHE / filename).write_text(page, encoding="utf-8")
        parsed[measure] = parse_spec(page, measure, url)
    PARSED.write_text(json.dumps(parsed, indent=1, ensure_ascii=False), encoding="utf-8")
    return parsed


def load(path=PARSED):
    """Parsed specs from the cache, or None if `fetch` hasn't been run yet."""
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    fetch_cmd = sub.add_parser("fetch", help="download and parse the specs")
    fetch_cmd.add_argument("--only", nargs="+", choices=sorted(SPECS),
                           help="only these measures, e.g. --only CMS951 for a public deploy")
    show = sub.add_parser("show", help="print one section, verbatim, with its notice")
    show.add_argument("measure", choices=sorted(SPECS))
    show.add_argument("section", nargs="?", default="Numerator")
    args = parser.parse_args()

    if args.cmd == "fetch":
        try:
            parsed = fetch(only=set(args.only) if args.only else None)
        except SpecError as exc:
            sys.exit(str(exc))
        for m, spec in parsed.items():
            print(f"{m} ({spec['meta']['eCQM Version Number']}, {spec['meta']['Measure Steward']}): "
                  f"{len(spec['sections'])} sections")
        print(f"Saved to {PARSED.relative_to(REPO)} (gitignored)")
    else:
        specs = load()
        if specs is None:
            sys.exit("No specs cached yet. Run: python3 -m him_workbench.care_gaps.spec_loader fetch")
        spec = specs[args.measure]
        if args.section not in spec["sections"]:
            sys.exit(f"No section '{args.section}'. Available: {', '.join(spec['sections'])}")
        print(spec["sections"][args.section])
        print(f"\n{spec['meta']['Copyright']}\nSource: {spec['url']}")


if __name__ == "__main__":
    main()
