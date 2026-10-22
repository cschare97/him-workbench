"""Drug name lookup through NLM's public RxNorm API (no key, no license for these endpoints).

Opt-in: the drug name you type is sent to https://rxnav.nlm.nih.gov. Everything else in the
workbench stays on this computer. Results are cached on disk for 24 hours, as NLM recommends.
Names and identifiers only. No dosing, no interactions, no advice.

Terms: https://lhncbc.nlm.nih.gov/RxNav/TermsofService.html (attribution below is required;
do not use the NLM name or logo otherwise; max 20 requests per second per IP).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import audit

BASE = "https://rxnav.nlm.nih.gov/REST"
SOURCE = "RxNorm API (NLM)"
ATTRIBUTION = ("This product uses publicly available data from the U.S. National Library of Medicine (NLM), "
               "National Institutes of Health, Department of Health and Human Services; NLM is not "
               "responsible for the product and does not endorse or recommend this or any other product.")

TTY_LABELS = {
    "IN": "ingredient", "PIN": "precise ingredient", "MIN": "multiple ingredients", "BN": "brand name",
    "SCD": "clinical drug", "SBD": "branded drug", "SCDC": "clinical drug component",
    "SBDC": "branded drug component", "SCDF": "clinical drug form", "SBDF": "branded drug form",
    "SCDG": "clinical dose form group", "SBDG": "branded dose form group",
    "GPCK": "generic pack", "BPCK": "branded pack", "DF": "dose form", "DFG": "dose form group",
}
_FIRST = {"IN": 0, "MIN": 1, "PIN": 2, "BN": 3, "SCD": 4, "SBD": 5}


class RxNormError(Exception):
    """The service could not be reached or answered in an unexpected way."""


def _http_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "him-workbench"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RxNormError(f"Could not get an answer from NLM's RxNorm service ({type(exc).__name__}). "
                          "Check the internet connection and try again.") from exc


class RxNormClient:
    def __init__(self, fetch: Callable[[str], dict] | None = None, cache_path: str | Path = "data/cache/rxnorm.json",
                 ttl: int = 24 * 3600, now: Callable[[], float] = time.time):
        self.fetch = fetch or _http_get
        self.cache_path = Path(cache_path)
        self.ttl = ttl
        self.now = now

    # ---- cache ----
    def _load(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = BASE + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        cache = self._load()
        hit = cache.get(url)
        if hit and self.now() - hit["t"] < self.ttl:
            return hit["data"]
        data = self.fetch(url)
        cache[url] = {"t": self.now(), "data": data}
        cache = {k: v for k, v in cache.items() if self.now() - v["t"] < self.ttl}
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass  # caching is a convenience; never fail a lookup over it
        return data

    # ---- lookup ----
    def search(self, name: str, limit: int = 10) -> dict:
        name = name.strip()
        if not name:
            return {"error": "no drug name given"}
        audit.log_call("rxnorm_search", {"query_chars": len(name)}, "called")
        found: dict[str, dict] = {}
        # 1. the concept(s) the name itself maps to (for "lisinopril", the ingredient)
        ids = ((self._get("/rxcui.json", {"name": name, "search": 2}).get("idGroup") or {}).get("rxnormId")) or []
        for rxcui in ids[:5]:
            props = self._get(f"/rxcui/{rxcui}/properties.json").get("properties") or {}
            if props.get("name"):
                found[rxcui] = {"rxcui": rxcui, "name": props["name"], "type": props.get("tty", "")}
        # 2. drug products that contain it (tablets, brands)
        data = self._get("/drugs.json", {"name": name})
        for group in (data.get("drugGroup") or {}).get("conceptGroup") or []:
            for c in group.get("conceptProperties") or []:
                found.setdefault(c["rxcui"], {"rxcui": c["rxcui"], "name": c["name"], "type": c.get("tty", "")})
        match = "name"
        if not found:  # nothing matched the spelling
            match = "approximate"
            cands = (self._get("/approximateTerm.json", {"term": name, "maxEntries": 5}).get("approximateGroup") or {}).get("candidate") or []
            for rxcui in list(dict.fromkeys(c["rxcui"] for c in cands))[:5]:
                props = self._get(f"/rxcui/{rxcui}/properties.json").get("properties") or {}
                if props.get("name"):
                    found[rxcui] = {"rxcui": rxcui, "name": props["name"], "type": props.get("tty", "")}
        entries = sorted(found.values(), key=lambda e: _FIRST.get(e["type"], 9))  # stable: keeps NLM order inside a type
        for e in entries:
            e["type_label"] = TTY_LABELS.get(e["type"], e["type"])
        out = {"results": entries[:limit], "total_matches": len(entries), "match": match, "source": SOURCE}
        notes = []
        if not entries:
            notes.append("No drug concept found. Check the spelling. This lookup covers drug names and RxNorm "
                         "identifiers only.")
        if match == "approximate" and entries:
            notes.append("No exact name match. These are NLM's closest matches to the spelling, so check they fit.")
        if len(entries) > limit:
            notes.append(f"Only {limit} of {len(entries)} matches are shown. Tell the user the list is partial.")
        if notes:
            out["note"] = " ".join(notes)
        audit.log_call("rxnorm_search", {"query_chars": len(name)}, f"{len(entries)} results, match={match}")
        return out
