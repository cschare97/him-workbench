"""Patient-education pages for an ICD-10-CM code, from NLM's MedlinePlus Connect service.

Opt-in: the code you ask about is sent to https://connect.medlineplus.gov. Results are cached on
disk for 24 hours (NLM recommends 12-24). Max 100 requests per minute per IP.

Terms (https://medlineplus.gov/medlineplus-connect/web-service/): display and link what the
service returns, do not copy MedlinePlus pages, do not use the MedlinePlus logo or imply
endorsement, say the information comes from MedlinePlus.gov, and keep any other-agency
attribution the service includes. This module shortens each summary and always links to the page.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from . import audit
from .icd10 import format_code, looks_like_code, normalize_code

BASE = "https://connect.medlineplus.gov/service"
TOPICS_BASE = "https://wsearch.nlm.nih.gov/ws/query"
ICD10CM_OID = "2.16.840.1.113883.6.90"
SNOMED_OID = "2.16.840.1.113883.6.96"
RXNORM_OID = "2.16.840.1.113883.6.88"
SOURCE = "MedlinePlus Connect (NLM)"
ATTRIBUTION = ("Health information from MedlinePlus.gov, a service of the U.S. National Library of Medicine. "
               "NLM is not responsible for this product and does not endorse or recommend it.")
SUMMARY_CHARS = 600
_VOID = {"br", "hr", "img", "meta", "link", "input"}


class MedlinePlusError(Exception):
    """The service could not be reached or answered in a format this tool does not recognize."""


def _http_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "him-workbench"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise MedlinePlusError(f"Could not get an answer from MedlinePlus Connect ({type(exc).__name__}). "
                               "Check the internet connection and try again.") from exc


def _http_get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "him-workbench"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MedlinePlusError(f"Could not get an answer from MedlinePlus ({type(exc).__name__}). "
                               "Check the internet connection and try again.") from exc


class _Summary(HTMLParser):
    """Plain text of a summary, with NLM's attribution note and related-link list kept apart."""

    SPECIAL = ("NLMattribution", "NLMrelatedLinks", "NLMalsoCalled")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.main: list[str] = []
        self.parts: dict[str, list[str]] = {k: [] for k in self.SPECIAL}
        self._mode: str | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        cls = dict(attrs).get("class") or ""
        if self._mode is None:
            for k in self.SPECIAL:
                if k in cls:
                    self._mode, self._depth = k, 1
                    return
        elif self._mode:
            self._depth += 1
        if tag in ("p", "li", "div", "br"):
            (self.parts[self._mode] if self._mode else self.main).append(" ")

    def _end_list_item(self):
        """Separate list items in the plain text: 'a, b, c.' instead of 'a b c'."""
        last = next((x.strip() for x in reversed(self.main) if x.strip()), "")
        self.main.append(" " if last.endswith((".", ";", ":", "!", "?")) else ", ")

    def handle_endtag(self, tag):
        if self._mode is None:
            if tag == "li":
                self._end_list_item()
            elif tag in ("ul", "ol"):
                # real pages have line breaks between tags, so look past blank text for the last separator
                i = next((k for k in range(len(self.main) - 1, -1, -1) if self.main[k].strip()), None)
                if i is not None and self.main[i] == ", ":
                    self.main[i] = ". "
        if tag in _VOID or self._mode is None:
            return
        self._depth -= 1
        if self._depth <= 0:
            self._mode = None

    def handle_data(self, data):
        (self.parts[self._mode] if self._mode else self.main).append(data)


def _squash(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _val(x) -> str:
    """Atom-in-JSON text fields are either a string or {"_value": "..."}."""
    if isinstance(x, dict):
        x = x.get("_value", "")
    return x if isinstance(x, str) else ""


def _href(link) -> str:
    if isinstance(link, list):
        link = link[0] if link else {}
    return link.get("href", "") if isinstance(link, dict) else ""


def parse(data: dict) -> list[dict]:
    feed = data.get("feed") if isinstance(data, dict) else None
    if not isinstance(feed, dict):
        raise MedlinePlusError("MedlinePlus Connect returned a format this tool does not recognize.")
    entries = feed.get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    out = []
    for e in entries:
        title, url = _val(e.get("title")), _href(e.get("link"))
        if not title or not url:
            raise MedlinePlusError("MedlinePlus Connect returned an entry this tool does not recognize.")
        p = _Summary()
        p.feed(_val(e.get("summary")))
        text = _squash(p.main)
        if len(text) > SUMMARY_CHARS:
            text = text[:SUMMARY_CHARS].rsplit(" ", 1)[0] + " ..."
        item = {"title": title, "url": url, "summary": text}
        credit = _squash(p.parts["NLMattribution"])
        if credit:
            item["credit"] = credit
        out.append(item)
    return out


def _shorten(html: str) -> tuple[str, str]:
    p = _Summary()
    p.feed(html)
    text = _squash(p.main)
    if len(text) > SUMMARY_CHARS:
        text = text[:SUMMARY_CHARS].rsplit(" ", 1)[0] + " ..."
    return text, _squash(p.parts["NLMattribution"])


def parse_topics(xml_text: str) -> tuple[list[dict], int]:
    """Health Topics search XML -> (pages, total count). Refuses anything it does not recognize."""
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        raise MedlinePlusError("MedlinePlus returned XML this tool will not read.")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise MedlinePlusError("MedlinePlus returned a format this tool does not recognize.") from exc
    if root.tag != "nlmSearchResult":
        raise MedlinePlusError("MedlinePlus returned a format this tool does not recognize.")
    try:
        total = int((root.findtext("count") or "0").strip())
    except ValueError:
        total = 0
    out = []
    for doc in root.iter("document"):
        url = doc.get("url", "")
        topic = doc.find("./content/health-topic")
        if topic is not None:  # rettype=topic: title and summary live inside <health-topic>
            title = topic.get("title", "")
            url = url or topic.get("url", "")
            body = topic.find("full-summary")
        else:  # flat fields (rettype=brief)
            fields = {c.get("name"): c for c in doc.findall("content")}
            title = _squash(list(fields["title"].itertext())) if "title" in fields else ""
            body = fields.get("FullSummary")
            if body is None:
                body = fields.get("snippet")
        if not title or not url:
            raise MedlinePlusError("MedlinePlus returned an entry this tool does not recognize.")
        text, credit = _shorten("".join(body.itertext())) if body is not None else ("", "")
        item = {"title": title, "url": url, "summary": text}
        if credit:
            item["credit"] = credit
        out.append(item)
    return out, total


class MedlinePlusClient:
    def __init__(self, fetch: Callable[[str], dict] | None = None, cache_path: str | Path = "data/cache/medlineplus.json",
                 ttl: int = 24 * 3600, now: Callable[[], float] = time.time,
                 fetch_text: Callable[[str], str] | None = None):
        self.fetch = fetch or _http_get
        self.fetch_text = fetch_text or _http_get_text
        self.cache_path = Path(cache_path)
        self.ttl = ttl
        self.now = now

    def _load(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _get(self, url: str) -> dict:
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
            pass
        return data

    def _get_text(self, url: str) -> str:
        cache = self._load()
        hit = cache.get(url)
        if hit and self.now() - hit["t"] < self.ttl:
            return hit["data"]
        text = self.fetch_text(url)
        cache[url] = {"t": self.now(), "data": text}
        cache = {k: v for k, v in cache.items() if self.now() - v["t"] < self.ttl}
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
        return text

    def education(self, query: str) -> dict:
        """A code goes to MedlinePlus Connect; anything else is searched by name in Health Topics."""
        query = query.strip()
        if not query:
            return {"error": "no disease name or code given"}
        if looks_like_code(query):
            return self._by_code(query)
        return self._by_name(query)

    def _by_name(self, term: str, limit: int = 5) -> dict:
        audit.log_call("medlineplus_search", {"query_chars": len(term)}, "called")
        url = TOPICS_BASE + "?" + urllib.parse.urlencode({
            "db": "healthTopics", "term": term, "rettype": "topic", "retmax": limit, "tool": "him-workbench"})
        pages, total = parse_topics(self._get_text(url))
        out = {"term": term, "results": pages, "total_matches": max(total, len(pages)),
               "source": "MedlinePlus Health Topics (NLM)"}
        notes = []
        if not pages:
            notes.append("MedlinePlus has no health topic for this wording. Try another name or a code.")
        if out["total_matches"] > len(pages):
            notes.append(f"Only {len(pages)} of {out['total_matches']} matching topics are shown. Tell the user the list is partial.")
        notes.append("Name search matches words, so check each topic fits what was asked.")
        out["note"] = " ".join(notes)
        audit.log_call("medlineplus_search", {"query_chars": len(term)}, f"{len(pages)} pages")
        return out

    def _connect(self, oid: str, code: str) -> list[dict]:
        url = BASE + "?" + urllib.parse.urlencode({
            "mainSearchCriteria.v.cs": oid, "mainSearchCriteria.v.c": code,
            "knowledgeResponseType": "application/json", "informationRecipient.languageCode.c": "en"})
        return parse(self._get(url))

    def for_chart_item(self, kind: str, code: str, name: str = "") -> dict:
        """Education for one line of a patient chart, by the code the FHIR file uses.
        kind 'problem': SNOMED CT code, and if MedlinePlus has no page for it, a name search (marked matched_by='name').
        kind 'drug': RxNorm code only (a drug's long name makes a poor search).
        Only the code (and the name, when the code finds nothing) is sent to NLM."""
        code = str(code).strip()
        if kind not in ("problem", "drug") or not re.fullmatch(r"\d{3,18}", code):
            return {"error": "need a problem or drug kind and a numeric SNOMED CT or RxNorm code"}
        oid, label = (SNOMED_OID, "SNOMED CT") if kind == "problem" else (RXNORM_OID, "RxNorm")
        audit.log_call("medlineplus_chart_lookup", {"kind": kind, "code_chars": len(code)}, "called")
        pages = self._connect(oid, code)
        out = {"kind": kind, "code": code, "system": label, "results": pages, "matched_by": "code" if pages else "",
               "source": SOURCE}
        if not pages and kind == "problem" and name.strip():
            by_name = self._by_name(name.strip(), limit=3)
            out.update(results=by_name["results"], matched_by="name" if by_name["results"] else "", term=name.strip())
            if by_name["results"]:
                out["note"] = (f"MedlinePlus has no page for {label} code {code}, so these come from a search by name. "
                               "Name search matches words, so check each page fits.")
        if not out["results"]:
            out["note"] = f"MedlinePlus has no patient-education page for {label} code {code}."
        audit.log_call("medlineplus_chart_lookup", {"kind": kind}, f"{len(out['results'])} pages")
        return out

    def _by_code(self, code: str) -> dict:
        shown = format_code(normalize_code(code))
        audit.log_call("medlineplus_lookup", {"code_chars": len(code)}, "called")
        url = BASE + "?" + urllib.parse.urlencode({
            "mainSearchCriteria.v.cs": ICD10CM_OID, "mainSearchCriteria.v.c": shown,
            "knowledgeResponseType": "application/json", "informationRecipient.languageCode.c": "en"})
        pages = parse(self._get(url))
        out = {"code": shown, "results": pages, "source": SOURCE}
        if not pages:
            out["note"] = ("MedlinePlus has no patient-education page for this code. Try a more general code "
                           "(for example the category before the dot).")
        audit.log_call("medlineplus_lookup", {"code_chars": len(code)}, f"{len(pages)} pages")
        return out
