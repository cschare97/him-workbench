"""Local web UI for the workbench (Flask, optional dependency: pip install -e ".[ui]").

Runs on this machine only. It refuses requests whose Host header is not localhost (blocks DNS
rebinding) and accepts POSTs only as JSON from the same origin. Synthetic data only; the banner
is on every page. The assistant box shows the model's answer next to the raw tool results.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory

from . import audit
from .assistant import Assistant, list_models, ollama_chat
from .icd10 import ICD10Index, find_code_in_text, real_words
from .matching import Patient, compare, find_duplicates
from .quality import CHECK_LABELS, FIELDS, analyze
from .render import result_lines
from .rxnorm import ATTRIBUTION

TEXTURE_NAMES = ("patients", "duplicates", "quality", "codes", "header")
TABS = [
    ("patients", "Patients", "/patients"),
    ("duplicates", "Duplicates", "/duplicates"),
    ("quality", "Data quality", "/quality"),
    ("codes", "Codes", "/codes"),
]


def _host_ok(value: str | None) -> bool:
    if not value:
        return False
    return urlparse("//" + value).hostname in {"127.0.0.1", "localhost", "::1"}


def create_app(*, icd: ICD10Index | None = None, patients: list[Patient] | None = None,
               model: str | None = None, chat_factory=None, texture_dir: str | Path = "data/ui-textures",
               models_lister=None, drugs=None) -> Flask:
    app = Flask(__name__)
    patients = patients or []
    by_id = {p.id: p for p in patients}
    chat_factory = chat_factory or ollama_chat
    models_lister = models_lister or list_models
    texture_dir = Path(texture_dir)
    lock = threading.Lock()

    st = SimpleNamespace(model=model, bot=None, icd=icd, patients=patients)
    st.dups = find_duplicates(patients) if patients else []
    st.quality = analyze(patients) if patients else None

    def new_bot() -> Assistant | None:
        return Assistant(chat_factory(st.model), icd=icd, patients=patients, drugs=drugs) if st.model else None

    st.bot = new_bot()

    # ---- safety: local only, JSON only ----
    @app.before_request
    def _guard():
        if not _host_ok(request.host):
            abort(403)
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and not _host_ok(urlparse(origin).netloc):
                abort(403)
            if not request.is_json:
                abort(415)

    @app.after_request
    def _headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        return resp

    @app.context_processor
    def _shared():
        local = {n: f"/local-textures/{n}.jpg" for n in TEXTURE_NAMES if (texture_dir / f"{n}.jpg").exists()}
        counts = {
            "patients": f"{len(patients)} records",
            "duplicates": f"{len(st.dups)} pairs",
            "quality": (f"{sum(1 for v in st.quality.problems.values() if v)} checks flagged"
                        if st.quality else "no data"),
            "codes": f"ICD-10-CM {icd.year}" if icd and icd.year else "ICD-10-CM",
        }
        return {"model": st.model, "drugs_on": drugs is not None, "attribution": ATTRIBUTION, "tabs": TABS, "tab_counts": counts, "local_textures": local}

    def name_of(pid: str) -> str:
        p = by_id.get(pid)
        return f"{p.first} {p.last}" if p else pid

    # ---- pages ----
    @app.get("/")
    def home():
        return redirect("/patients")

    @app.get("/patients")
    def patients_page():
        q = request.args.get("q", "").strip().lower()
        rows = [p for p in patients if not q or q in f"{p.first} {p.last} {p.id} {p.dob} {p.zip}".lower()]
        return render_template("patients.html", active="patients", page_title="Patients",
                               rows=rows[:300], total=len(patients), matched=len(rows), q=request.args.get("q", ""))

    @app.get("/duplicates")
    def duplicates_page():
        n = max(5, min(int(request.args.get("n", 20) or 20), 200))
        pairs = []
        for r in st.dups[:n]:
            a, b = by_id.get(r.a), by_id.get(r.b)
            rows = []
            if a and b:
                for label, va, vb, key in [("First name", a.first, b.first, "first"), ("Last name", a.last, b.last, "last"),
                                           ("Birth date", a.dob, b.dob, "dob"), ("Sex", a.sex, b.sex, "sex"),
                                           ("Zip", a.zip, b.zip, "zip")]:
                    rows.append({"label": label, "a": va or "(blank)", "b": vb or "(blank)",
                                 "points": r.detail.get(key, 0), "same": (va or "") == (vb or "")})
            pairs.append({"r": r, "name_a": name_of(r.a), "name_b": name_of(r.b), "rows": rows})
        n_review = sum(1 for r in st.dups if r.label == "review")
        return render_template("duplicates.html", active="duplicates", page_title="Duplicates", pairs=pairs,
                               found=len(st.dups), shown=len(pairs), n_review=n_review,
                               n_match=len(st.dups) - n_review, n=n)

    @app.get("/quality")
    def quality_page():
        return render_template("quality.html", active="quality", page_title="Data quality", rep=st.quality,
                               labels=CHECK_LABELS, fields=FIELDS)

    @app.get("/codes")
    def codes_page():
        q = request.args.get("q", "").strip()
        ctx = {"q": q, "results": [], "total": 0, "ignored": [], "lookup": None, "unmatched": [], "loaded": icd is not None}
        if icd and q:
            code = find_code_in_text(q)
            found = icd.describe(code) if code else None
            if found and found["status"] != "unknown":
                ctx["lookup"] = found          # a code was typed: show that code, not a word search
            else:
                results, ignored, total = icd.search_detail(q, limit=25)
                ctx.update(results=results, ignored=ignored, total=total, unmatched=real_words(ignored))
        return render_template("codes.html", active="codes", page_title="Codes", source=icd.source if icd else "",
                               **ctx)

    @app.get("/audit")
    def audit_page():
        entries = []
        path = audit.LOG_PATH
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines()[-200:]:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return render_template("audit.html", active=None, nav="audit", page_title="Audit log",
                               entries=list(reversed(entries)))

    @app.get("/models")
    def models_page():
        names, error = [], ""
        try:
            names = models_lister()
        except Exception as exc:
            error = f"Could not reach Ollama ({type(exc).__name__}). Start it, then reload this page."
        return render_template("models.html", active=None, nav="models", page_title="Models", names=names, error=error)

    @app.get("/sources")
    def sources_page():
        return render_template("sources.html", active=None, nav="sources", page_title="Data sources",
                               icd=icd, n_patients=len(patients), empty=(st.quality.empty_fields if st.quality else []))

    @app.get("/about")
    def about_page():
        return render_template("about.html", active=None, nav="about", page_title="About")

    @app.get("/local-textures/<name>")
    def local_texture(name):
        if name.rsplit(".", 1)[0] not in TEXTURE_NAMES:
            abort(404)
        return send_from_directory(texture_dir.resolve(), name)

    # ---- assistant ----
    @app.post("/api/ask")
    def api_ask():
        question = str((request.get_json(silent=True) or {}).get("question", "")).strip()
        if not question:
            return jsonify(error="Type a question first."), 400
        if len(question) > 500:
            return jsonify(error="Keep questions under 500 characters."), 400
        if st.bot is None:
            return jsonify(error="No model selected. Choose one under Models."), 409
        calls: list[dict] = []
        with lock:
            bot = st.bot
            bot.on_tool = lambda n, a: calls.append({"tool": n, "args": a, "headline": "", "lines": []})
            def _res(n, r):
                head, lines = result_lines(n, r)
                if calls:
                    calls[-1].update(headline=head, lines=lines)
            bot.on_result = _res
            try:
                answer = bot.ask(question)
            except Exception as exc:
                return jsonify(error=f"The model did not answer ({type(exc).__name__}). Is Ollama running?"), 502
        return jsonify(answer=answer, calls=calls, model=st.model)

    @app.post("/api/reset")
    def api_reset():
        with lock:
            st.bot = new_bot()
        return jsonify(ok=True)

    @app.post("/api/model")
    def api_model():
        name = str((request.get_json(silent=True) or {}).get("model", "")).strip()
        if not name:
            return jsonify(error="No model given."), 400
        with lock:
            st.model = name
            st.bot = new_bot()
        return jsonify(ok=True, model=name)

    return app
