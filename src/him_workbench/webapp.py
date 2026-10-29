"""Local web UI for the workbench (Flask, optional dependency: pip install -e ".[ui]").

Runs on this machine only. It refuses requests whose Host header is not localhost (blocks DNS
rebinding) and accepts POSTs only as JSON from the same origin. Synthetic data only; the banner
is on every page. The assistant box shows the model's answer next to the raw tool results.
"""

from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory

from . import audit
from .assistant import AGENT_PROFILES, Assistant, list_models, ollama_chat
from .chart import clean, load_chart, active
from .icd10 import ICD10Index, find_code_in_text, real_words
from .hcpcs import HCPCSIndex, find_code_in_text as find_hcpcs_in_text
from .matching import Patient, compare, find_duplicates
from .quality import CHECK_LABELS, FIELDS, analyze
from .render import result_lines
from . import roi as roi_mod
from .roi import RoiError, RoiStore
from . import smart_fhir
from . import referrals as referrals_mod
from .referrals import ReferralError, ReferralStore
from .docsearch import NoteIndex
from .notes import generate_note
from .embeddings import default_model as default_embed_model
from datetime import date
import re

from .medlineplus import ATTRIBUTION as EDU_ATTRIBUTION, MedlinePlusError
from .rxnorm import ATTRIBUTION

TEXTURE_NAMES = ("patients", "duplicates", "quality", "codes", "hcpcs", "header")
TABS = [
    ("patients", "Patients", "/patients"),
    ("duplicates", "Duplicates", "/duplicates"),
    ("quality", "Data quality", "/quality"),
    ("codes", "Codes", "/codes"),
    ("hcpcs", "HCPCS", "/hcpcs"),
]


def _host_ok(value: str | None) -> bool:
    if not value:
        return False
    return urlparse("//" + value).hostname in {"127.0.0.1", "localhost", "::1"}


def create_app(*, icd: ICD10Index | None = None, hcpcs: HCPCSIndex | None = None,
               patients: list[Patient] | None = None,
               model: str | None = None, chat_factory=None, texture_dir: str | Path = "data/ui-textures",
               models_lister=None, drugs=None, education=None, patient_source: str = "",
               chart_files: dict | None = None, roi_store: RoiStore | None = None,
               referral_store: ReferralStore | None = None, notes_embed=None, today=None) -> Flask:
    app = Flask(__name__)
    app.jinja_env.filters["clean"] = clean
    chart_files = chart_files or {}
    roi = roi_store or RoiStore(":memory:")
    referrals = referral_store or ReferralStore(":memory:")
    get_today = today or date.today
    patients = patients or []
    by_id = {p.id: p for p in patients}
    chat_factory = chat_factory or ollama_chat
    models_lister = models_lister or list_models
    texture_dir = Path(texture_dir)
    lock = threading.Lock()

    st = SimpleNamespace(model=model, bot=None, icd=icd, patients=patients)
    st.dups = find_duplicates(patients) if patients else []
    st.quality = analyze(patients) if patients else None
    st.notes = NoteIndex(embed_fn=notes_embed)
    st.notes_built = False
    embed_model = default_embed_model()
    # Live SMART on FHIR connection state. 'pending' holds a not-yet-completed launch's state/PKCE
    # verifier while the browser is off at the sandbox's own authorize page; 'token'/'server'/
    # 'patient_id' hold a completed connection. Single-user local tool, so one shared slot is enough
    # (same pattern as st.model/st.bots above) rather than a per-session store.
    st.smart = {"pending": None, "token": None, "server": "", "patient_id": "", "error": ""}

    def ensure_notes_index() -> None:
        if st.notes_built:
            return
        with lock:
            if not st.notes_built:   # re-check: another request may have built it while we waited
                def gen():
                    for pid, f in chart_files.items():
                        try:
                            chart = load_chart(f)
                        except (OSError, ValueError):
                            continue
                        p = by_id.get(pid)
                        name = f"{p.first} {p.last}" if p else pid
                        yield pid, generate_note(pid, chart, name).text
                st.notes.build(gen())
                st.notes_built = True

    def new_bot(profile: str) -> Assistant | None:
        return (Assistant(chat_factory(st.model), icd=icd, hcpcs=hcpcs, patients=patients, drugs=drugs,
                         education=education, charts=chart_files, profile=profile) if st.model else None)

    # One Assistant per agent profile, each with its own tool set, system prompt, and conversation
    # history — so asking the "Codes" agent something doesn't carry state into the "Duplicates"
    # agent, and vice versa. Built lazily so an unused profile never calls Ollama for nothing.
    st.bots: dict[str, Assistant] = {}

    def get_bot(profile: str) -> Assistant | None:
        if profile not in st.bots:
            st.bots[profile] = new_bot(profile)
        return st.bots[profile]

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
            "hcpcs": f"HCPCS Level II {hcpcs.year}" if hcpcs and hcpcs.year else "HCPCS Level II",
        }
        agents = [("general", AGENT_PROFILES["general"]["label"])]
        if icd or hcpcs:
            agents.append(("codes", AGENT_PROFILES["codes"]["label"]))
        if patients:
            agents.append(("duplicates", AGENT_PROFILES["duplicates"]["label"]))
        if drugs or education:
            agents.append(("drugs_education", AGENT_PROFILES["drugs_education"]["label"]))
        if chart_files:
            agents.append(("chart", AGENT_PROFILES["chart"]["label"]))
        return {"model": st.model, "drugs_on": drugs is not None, "attribution": ATTRIBUTION,
                "education_on": education is not None, "edu_attribution": EDU_ATTRIBUTION, "tabs": TABS,
                "tab_counts": counts, "local_textures": local, "agents": agents}

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
                               rows=rows[:300], total=len(patients), matched=len(rows), q=request.args.get("q", ""),
                               charts=chart_files)

    @app.get("/patients/<pid>/chart")
    def chart_page(pid):
        p = by_id.get(pid)
        if p is None:
            abort(404)
        chart, problem = None, ""
        f = chart_files.get(pid)
        if f is None:
            problem = "No chart for this patient. Charts come from FHIR files, so start the page with --fhir."
        else:
            try:
                chart = load_chart(f)
                if chart.patient_id != pid:
                    chart, problem = None, "The file for this patient holds a different patient's record, so it was not shown."
            except (OSError, ValueError):
                problem = "That patient's FHIR file could not be read."
        return render_template("chart.html", active="patients", page_title=f"{p.first} {p.last}", p=p,
                               ask_patient=p if chart else None, chart=chart, problem=problem, act=active(chart.problems) if chart else [],
                               past=[x for x in chart.problems if x.status != "active"] if chart else [])

    @app.get("/patients/<pid>/learn")
    def learn_page(pid):
        p = by_id.get(pid)
        if p is None:
            abort(404)
        kind = request.args.get("kind", "")
        code = request.args.get("code", "").strip()
        name = request.args.get("name", "").strip()[:120]
        result, problem = None, ""
        if education is None:
            problem = "Patient education is off. Start the page with --education to turn it on."
        elif kind not in ("problem", "drug") or not re.fullmatch(r"\d{3,18}", code):
            problem = "That link is not a problem or drug code this page can look up."
        else:
            try:
                result = education.for_chart_item(kind, code, name)
            except MedlinePlusError as exc:
                problem = str(exc)
        return render_template("learn.html", active="patients", page_title="Patient education", p=p,
                               result=result, problem=problem, kind=kind, code=code, name=name)

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
        ctx = {"q": q, "results": [], "total": 0, "ignored": [], "lookup": None, "unmatched": [], "maybe": {}, "loaded": icd is not None}
        if icd and q:
            code = find_code_in_text(q)
            found = icd.describe(code) if code else None
            if found and found["status"] != "unknown":
                ctx["lookup"] = found          # a code was typed: show that code, not a word search
            else:
                results, ignored, total = icd.search_detail(q, limit=25)
                ctx.update(results=results, ignored=ignored, total=total, unmatched=real_words(ignored),
                           maybe=icd.did_you_mean(q))
        return render_template("codes.html", active="codes", page_title="Codes", source=icd.source if icd else "",
                               **ctx)

    @app.get("/hcpcs")
    def hcpcs_page():
        q = request.args.get("q", "").strip()
        ctx = {"q": q, "results": [], "total": 0, "ignored": [], "lookup": None, "unmatched": [], "maybe": {},
               "loaded": hcpcs is not None}
        if hcpcs and q:
            code = find_hcpcs_in_text(q)
            found = hcpcs.describe(code) if code else None
            if found and found["status"] != "unknown":
                ctx["lookup"] = found
            else:
                results, ignored, total = hcpcs.search_detail(q, limit=25)
                ctx.update(results=results, ignored=ignored, total=total, unmatched=real_words(ignored),
                           maybe=hcpcs.did_you_mean(q))
        return render_template("hcpcs.html", active="hcpcs", page_title="HCPCS", nav="hcpcs",
                               source=hcpcs.source if hcpcs else "", **ctx)


    # ---- release of information requests ----
    @app.get("/roi")
    def roi_page():
        status = request.args.get("status", "")
        t = get_today()
        rows = roi.list(status or None)
        return render_template("roi.html", active=None, nav="roi", page_title="ROI requests", rows=rows, today=t,
                               summary=roi.summary(t), status=status, name_of=name_of, statuses=roi_mod.LABELS,
                               types=roi_mod.REQUESTER_TYPES, days=roi.days, patients=patients[:300],
                               have_patients=bool(patients), today_iso=t.isoformat())

    @app.get("/roi/<int:rid>")
    def roi_detail(rid):
        q = roi.get(rid)
        if q is None:
            abort(404)
        t = get_today()
        return render_template("roi_detail.html", active=None, nav="roi", page_title=f"ROI request {rid}", q=q,
                               events=roi.events(rid), today=t, name_of=name_of, labels=roi_mod.LABELS,
                               moves=roi_mod.MOVES.get(q.status, ()), days=roi.days)

    def _roi_json(fn):
        try:
            fn()
        except RoiError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True)

    @app.post("/roi/new")
    def roi_new():
        d = request.get_json(silent=True) or {}
        pid = str(d.get("patient_id", "")).strip()
        if patients and pid not in by_id:
            return jsonify(error="Pick a patient from the list. This tool only tracks the synthetic patients loaded."), 400
        out = {}

        def go():
            out["id"] = roi.create(pid, str(d.get("requester_type", "")), d.get("summary"),
                                   str(d.get("received") or get_today().isoformat()), today=get_today())
        r = _roi_json(go)
        return jsonify(ok=True, id=out["id"]) if out else r

    @app.post("/roi/<int:rid>/status")
    def roi_status(rid):
        d = request.get_json(silent=True) or {}
        return _roi_json(lambda: roi.set_status(rid, str(d.get("status", "")), d.get("note"), today=get_today()))

    @app.post("/roi/<int:rid>/extend")
    def roi_extend(rid):
        d = request.get_json(silent=True) or {}
        return _roi_json(lambda: roi.extend(rid, d.get("reason"), today=get_today()))

    # ---- specialty referrals ----
    @app.get("/referrals")
    def referrals_page():
        status = request.args.get("status", "")
        t = get_today()
        rows = referrals.list(status or None, today=t)
        return render_template("referrals.html", active=None, nav="referrals", page_title="Referrals", rows=rows,
                               today=t, summary=referrals.summary(t), status=status, name_of=name_of,
                               statuses=referrals_mod.LABELS, priorities=referrals_mod.PRIORITIES,
                               coordinators=referrals_mod.COORDINATORS, days=referrals.followup_days,
                               patients=patients[:300], have_patients=bool(patients))

    @app.get("/referrals/<int:rid>")
    def referral_detail(rid):
        q = referrals.get(rid)
        if q is None:
            abort(404)
        t = get_today()
        return render_template("referral_detail.html", active=None, nav="referrals", page_title=f"Referral {rid}",
                               q=q, events=referrals.events(rid), today=t, name_of=name_of,
                               labels=referrals_mod.LABELS, moves=referrals_mod.MOVES.get(q.status, ()))

    def _ref_json(fn):
        try:
            fn()
        except ReferralError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True)

    @app.post("/referrals/new")
    def referral_new():
        d = request.get_json(silent=True) or {}
        pid = str(d.get("patient_id", "")).strip()
        if patients and pid not in by_id:
            return jsonify(error="Pick a patient from the list. This tool only tracks the synthetic patients loaded."), 400
        out = {}

        def go():
            out["id"] = referrals.create(
                pid, str(d.get("specialty", "")), str(d.get("priority", "")), d.get("reason"),
                str(d.get("diagnosis", "")), referring_provider=str(d.get("referring_provider", "")),
                receiving_provider=str(d.get("receiving_provider", "")), coordinator=str(d.get("coordinator", "")),
                notes=str(d.get("notes", "")), icd10_codes=str(d.get("icd10_codes", "")),
                created=d.get("created") or None, today=get_today())
        r = _ref_json(go)
        return jsonify(ok=True, id=out["id"]) if out else r

    @app.post("/referrals/<int:rid>/status")
    def referral_status(rid):
        d = request.get_json(silent=True) or {}
        return _ref_json(lambda: referrals.set_status(rid, str(d.get("status", "")), d.get("note"),
                                                       appointment_date=d.get("appointment_date") or None,
                                                       today=get_today()))

    # ---- document search ----
    @app.get("/search")
    def search_page():
        q = request.args.get("q", "").strip()[:300]
        have_charts = bool(chart_files)
        results, error = [], ""
        if have_charts:
            ensure_notes_index()
            error = st.notes.build_error
            if q and not error:
                results, error = st.notes.search(q, limit=25)
        return render_template("search.html", active=None, nav="search", page_title="Document search", q=q,
                               results=results, error=error, have_charts=have_charts, name_of=name_of,
                               n_indexed=len(st.notes), skipped_count=st.notes.skipped_count,
                               embed_model=embed_model)

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
                               icd=icd, n_patients=len(patients), patient_source=patient_source, empty=(st.quality.empty_fields if st.quality else []))

    @app.get("/about")
    def about_page():
        return render_template("about.html", active=None, nav="about", page_title="About")

    @app.get("/local-textures/<name>")
    def local_texture(name):
        if name.rsplit(".", 1)[0] not in TEXTURE_NAMES:
            abort(404)
        return send_from_directory(texture_dir.resolve(), name)

    # ---- SMART on FHIR (live sandbox, read-only) ----
    @app.get("/smart")
    def smart_page():
        patient, chart, problem = None, None, ""
        if st.smart["token"] and st.smart["patient_id"]:
            try:
                patient, chart = smart_fhir.fetch_chart(st.smart["server"], st.smart["patient_id"],
                                                        st.smart["token"])
            except smart_fhir.SmartError as exc:
                problem = f"Connected, but could not read the chart: {exc}"
        return render_template("smart.html", active=None, nav="smart", page_title="SMART on FHIR",
                               connected=bool(st.smart["token"]), server=st.smart["server"] or smart_fhir.iss(),
                               default_server=smart_fhir.iss(), client_id=smart_fhir.client_id(),
                               patient=patient, chart=chart, problem=problem, error=st.smart["error"],
                               act=active(chart.problems) if chart else [],
                               past=[x for x in chart.problems if x.status != "active"] if chart else [])

    @app.get("/smart/connect")
    def smart_connect():
        server = smart_fhir.iss()
        try:
            endpoints = smart_fhir.discover(server)
        except smart_fhir.SmartError as exc:
            with lock:
                st.smart["error"] = f"Could not start the connection: {exc}"
            return redirect("/smart")
        verifier, challenge = smart_fhir.new_pkce()
        state = secrets.token_urlsafe(24)
        redirect_uri = request.url_root.rstrip("/") + "/smart/callback"
        with lock:
            st.smart["pending"] = {"state": state, "verifier": verifier, "server": server,
                                   "token_endpoint": endpoints["token_endpoint"], "redirect_uri": redirect_uri}
            st.smart["error"] = ""
        url = smart_fhir.authorize_url(endpoints["authorization_endpoint"], server=server,
                                       cid=smart_fhir.client_id(), redirect_uri=redirect_uri,
                                       state=state, code_challenge=challenge)
        return redirect(url)

    @app.get("/smart/callback")
    def smart_callback():
        pending = st.smart["pending"]
        err = request.args.get("error")
        if err:
            with lock:
                st.smart["error"] = f"The sandbox declined the launch: {err} — " + request.args.get(
                    "error_description", "(no further detail given)")
                st.smart["pending"] = None
            return redirect("/smart")
        if not pending or request.args.get("state") != pending["state"]:
            with lock:
                st.smart["error"] = "This connection attempt expired or did not match. Try connecting again."
                st.smart["pending"] = None
            return redirect("/smart")
        code = request.args.get("code")
        if not code:
            with lock:
                st.smart["error"] = "The sandbox did not return an authorization code."
                st.smart["pending"] = None
            return redirect("/smart")
        try:
            token = smart_fhir.exchange_code(pending["token_endpoint"], code=code,
                                             redirect_uri=pending["redirect_uri"], cid=smart_fhir.client_id(),
                                             code_verifier=pending["verifier"])
        except smart_fhir.SmartError as exc:
            with lock:
                st.smart["error"] = str(exc)
                st.smart["pending"] = None
            return redirect("/smart")
        patient_id = token.get("patient")
        with lock:
            if not patient_id:
                st.smart["error"] = ("Connected, but the sandbox did not hand back a patient — pick one "
                                     "when the sandbox prompts you, then try connecting again.")
            else:
                st.smart["token"], st.smart["server"] = token.get("access_token"), pending["server"]
                st.smart["patient_id"], st.smart["error"] = patient_id, ""
            st.smart["pending"] = None
        return redirect("/smart")

    @app.post("/smart/disconnect")
    def smart_disconnect():
        with lock:
            st.smart.update(pending=None, token=None, server="", patient_id="", error="")
        return jsonify(ok=True)

    # ---- assistant ----
    @app.post("/api/ask")
    def api_ask():
        body = request.get_json(silent=True) or {}
        question = str(body.get("question", "")).strip()
        patient_id = body.get("patient_id")
        agent = str(body.get("agent") or "general")
        if agent not in AGENT_PROFILES:
            return jsonify(error="Not a known agent."), 400
        if patient_id is not None and (not isinstance(patient_id, str) or patient_id not in by_id):
            return jsonify(error="That patient is not in the loaded list."), 400
        if not question:
            return jsonify(error="Type a question first."), 400
        if len(question) > 500:
            return jsonify(error="Keep questions under 500 characters."), 400
        if st.model is None:
            return jsonify(error="No model selected. Choose one under Models."), 409
        calls: list[dict] = []
        with lock:
            bot = get_bot(agent)
            if bot is None:
                return jsonify(error="No model selected. Choose one under Models."), 409
            bot.on_tool = lambda n, a: calls.append({"tool": n, "args": a, "headline": "", "lines": []})
            def _res(n, r):
                head, lines = result_lines(n, r)
                if calls:
                    calls[-1].update(headline=head, lines=lines)
            bot.on_result = _res
            try:
                answer = bot.ask(question, patient_id=patient_id)
            except Exception as exc:
                return jsonify(error=f"The model did not answer ({type(exc).__name__}). Is Ollama running?"), 502
        return jsonify(answer=answer, calls=calls, model=st.model, agent=agent)

    @app.post("/api/reset")
    def api_reset():
        with lock:
            st.bots = {}
        return jsonify(ok=True)

    @app.post("/api/model")
    def api_model():
        name = str((request.get_json(silent=True) or {}).get("model", "")).strip()
        if not name:
            return jsonify(error="No model given."), 400
        with lock:
            st.model = name
            st.bots = {}
        return jsonify(ok=True, model=name)

    return app
