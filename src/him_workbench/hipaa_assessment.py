"""A HIPAA Security Rule self-assessment of this project, checked against the code.

Each row is one standard or implementation specification from 45 CFR 164 Subpart C (the Security Rule), with
(R) Required or (A) Addressable as the rule's own matrix gives it. For each row this file states:
  - status: what the project claims about itself (met, partly met, not met, not applicable, organizational),
  - evidence: what in the repo supports the claim,
  - gap: what is missing,
  - checks: small tests run against the repo files. If a check no longer holds, the row's status becomes
    RE-CHECK, so the report cannot keep claiming something the code stopped showing. This applies to "not met"
    rows too: if someone adds a login, the "no authentication" row goes to RE-CHECK until it is re-assessed.

Read this first:
  - This is a self-assessment by the author, not an audit, a certification or legal advice.
  - HIPAA applies to covered entities and business associates handling real ePHI. This project uses synthetic
    data only, so it is not regulated. The question answered here is "what would have to be true before real
    ePHI went near it", and the answer is mostly "a lot".
  - Many standards are about an organization (training, policies, contracts, buildings). Software cannot show
    those, so they are marked ORGANIZATIONAL, not "met".
  - The rule is being rewritten. HHS proposed removing the required/addressable distinction; as of mid-2026 that
    is a proposal, not a final rule (see the README note). This file uses the current R/A designations.
  - NIST SP 800-66 Rev 2 is the guidance for how to carry out each standard. Per-row crosswalk to its sections is
    not included, because it has not been checked against the publication.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MET, PARTIAL, NOT_MET, NA, ORG, RECHECK = "Met", "Partly met", "Not met", "Not applicable", "Organizational", "RE-CHECK"
ROOT = Path(__file__).resolve().parents[2]


# ---- checks: each returns (ok, what it looked for) ---------------------------------------------------------

def _read(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8")
    except OSError:
        return ""


def has(rel: str, text: str):
    return (lambda root: (text in _read(root, rel), f"{rel} contains {text!r}"))


def lacks(globs: tuple[str, ...], pattern: str):
    """None of the matching files contain the regex (case-insensitive). This file is skipped: it names the patterns."""
    def run(root: Path):
        rx = re.compile(pattern, re.IGNORECASE)
        for g in globs:
            for f in sorted(root.glob(g)):
                if f.is_file() and f.name != "hipaa_assessment.py" and rx.search(f.read_text(encoding="utf-8", errors="ignore")):
                    return False, f"{f.relative_to(root)} matches /{pattern}/"
        return True, f"no match for /{pattern}/ in {', '.join(globs)}"
    return run


def tests_named(pattern: str, at_least: int = 1):
    def run(root: Path):
        rx = re.compile(r"^def (test_\w*(?:%s)\w*)\(" % pattern, re.MULTILINE | re.IGNORECASE)
        n = sum(len(rx.findall(f.read_text(encoding="utf-8", errors="ignore"))) for f in (root / "tests").glob("test_*.py"))
        return n >= at_least, f"at least {at_least} test(s) named like /{pattern}/ (found {n})"
    return run


def modules_calling(call: str, at_least: int):
    def run(root: Path):
        n = sum(1 for f in (root / "src/him_workbench").glob("*.py") if call in f.read_text(encoding="utf-8", errors="ignore")
                and f.name != "audit.py")
        return n >= at_least, f"{call} used in at least {at_least} modules (found {n})"
    return run


@dataclass
class Control:
    cite: str
    standard: str
    spec: str
    kind: str                  # "R", "A", or "R*" = the standard itself is required and has no sub-specifications
    claim: str
    evidence: str = ""
    gap: str = ""
    checks: list = field(default_factory=list)
    area: str = ""             # Administrative, Physical, Technical, Organizational, Documentation
    status: str = ""           # filled by run_checks
    failed: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)


A_, P_, T_, O_, D_ = "Administrative", "Physical", "Technical", "Organizational", "Documentation"

# Checks used by several rows.
NO_AUTH = lacks(("src/him_workbench/*.py",), r"login_required|flask_login|check_password|session\[|SECRET_KEY|request\.authorization")
LOOPBACK = has("scripts/ui_cli.py", 'host="127.0.0.1"')
HOST_GUARD = has("src/him_workbench/webapp.py", "if not _host_ok(request.host):")
NO_ENCRYPTION = lacks(("src/him_workbench/*.py",), r"sqlcipher|\bcryptography\b|\bFernet\b|\bAES\b")


def _controls() -> list[Control]:
    C = Control
    return [
        # ---------- 164.308 Administrative safeguards ----------
        C("164.308(a)(1)(ii)(A)", "Security management process", "Risk analysis", "R", PARTIAL, area=A_,
          evidence="Security tests in tests/test_webapp.py (foreign and look-alike Host headers, Origin checks, path traversal, "
                   "HTML escaping) and this assessment.",
          gap="No documented asset-and-threat risk analysis. This file is a first pass, not a substitute.",
          checks=[tests_named("lookalike|foreign_host|traversal|escaped", 3)]),
        C("164.308(a)(1)(ii)(B)", "Security management process", "Risk management", "R", PARTIAL, area=A_,
          evidence="Gaps are listed in this report. Fixes land in git with tests.",
          gap="No tracked remediation plan with owners and dates."),
        C("164.308(a)(1)(ii)(C)", "Security management process", "Sanction policy", "R", ORG, area=A_,
          gap="A workforce policy. A single-developer project has no workforce to sanction."),
        C("164.308(a)(1)(ii)(D)", "Security management process", "Information system activity review", "R", PARTIAL, area=A_,
          evidence="The Audit log page shows the last 200 recorded actions.",
          gap="No scheduled review, alerting or retention rule. Nothing prompts anyone to look.",
          checks=[has("src/him_workbench/webapp.py", '@app.get("/audit")')]),
        C("164.308(a)(2)", "Assigned security responsibility", "(standard only)", "R*", ORG, area=A_,
          gap="Needs a named person in a real organization."),
        C("164.308(a)(3)(ii)", "Workforce security", "Authorization/supervision (A), workforce clearance (A), termination procedures (A)",
          "A", ORG, area=A_, gap="Workforce processes. Not something code can show."),
        C("164.308(a)(4)(ii)(A)", "Information access management", "Isolating health care clearinghouse function", "R", NA, area=A_,
          gap="This is not a clearinghouse."),
        C("164.308(a)(4)(ii)(B)-(C)", "Information access management", "Access authorization (A), access establishment and modification (A)",
          "A", NOT_MET, area=A_,
          evidence="Compensating control: the app is single-user and reachable from this computer only.",
          gap="No accounts or roles. Every page and the assistant can read every loaded patient.",
          checks=[NO_AUTH, LOOPBACK]),
        C("164.308(a)(5)(ii)(A)", "Security awareness and training", "Security reminders", "A", ORG, area=A_,
          gap="Workforce training."),
        C("164.308(a)(5)(ii)(B)", "Security awareness and training", "Protection from malicious software", "A", PARTIAL, area=A_,
          evidence="The core has no third-party dependencies (Flask and openpyxl are optional extras). A pre-commit hook scans for secret-looking strings "
                   "and personal terms.",
          gap="No dependency vulnerability scanning (for example pip-audit) and no malware controls.",
          checks=[has("pyproject.toml", "dependencies = []"), has("scripts/install_hooks.sh", "secret_check.sh")]),
        C("164.308(a)(5)(ii)(C)-(D)", "Security awareness and training", "Log-in monitoring (A), password management (A)", "A", NA, area=A_,
          gap="There is no log-in, which is itself the gap in the authentication rows below.",
          checks=[NO_AUTH]),
        C("164.308(a)(6)(ii)", "Security incident procedures", "Response and reporting", "R", NOT_MET, area=A_,
          evidence="The audit log would be a starting point for reconstructing what happened.",
          gap="No incident response or reporting procedure."),
        C("164.308(a)(7)(ii)(A)", "Contingency plan", "Data backup plan", "R", NOT_MET, area=A_,
          evidence="Data is synthetic and regenerable (Synthea). SQLite trackers live under data/, which is gitignored.",
          gap="No backup of the ROI and referral databases or the audit log.",
          checks=[has(".gitignore", "data/*"), has(".gitignore", "*.db")]),
        C("164.308(a)(7)(ii)(B)-(E)", "Contingency plan",
          "Disaster recovery (R), emergency mode operation (R), testing and revision (A), criticality analysis (A)", "R", ORG, area=A_,
          gap="Organizational plans."),
        C("164.308(a)(8)", "Evaluation", "(standard only)", "R*", PARTIAL, area=A_,
          evidence="An automated test suite runs the security behaviors above, and this assessment re-checks itself against the code.",
          gap="No scheduled periodic evaluation and no outside review.",
          checks=[tests_named("", 100)]),
        C("164.308(b)(1)", "Business associate contracts and other arrangements", "Written contract or other arrangement", "R", NA, area=A_,
          evidence="No ePHI leaves the machine. The opt-in lookups send a drug name or a code, and the page says no patient "
                   "information is sent.",
          gap="If real ePHI were ever involved, any outside service that could receive it would need an agreement first.",
          checks=[has("src/him_workbench/templates/learn.html", "No patient information was sent.")]),

        # ---------- 164.310 Physical safeguards ----------
        C("164.310(a)(2)", "Facility access controls", "Contingency operations, facility security plan, access control and validation, "
          "maintenance records (all A)", "A", ORG, area=P_, gap="About the building and room the computer is in."),
        C("164.310(b)", "Workstation use", "(standard only)", "R*", ORG, area=P_, gap="Rules for how the workstation may be used."),
        C("164.310(c)", "Workstation security", "(standard only)", "R*", ORG, area=P_,
          gap="Screen lock, disk encryption and physical access cannot be seen from the repo."),
        C("164.310(d)(2)(i)-(ii)", "Device and media controls", "Disposal (R), media re-use (R)", "R", ORG, area=P_,
          gap="How disks and exports are wiped. The data is synthetic."),
        C("164.310(d)(2)(iii)-(iv)", "Device and media controls", "Accountability (A), data backup and storage (A)", "A", ORG, area=P_,
          gap="Tracking of devices and media."),

        # ---------- 164.312 Technical safeguards ----------
        C("164.312(a)(2)(i)", "Access control", "Unique user identification", "R", NOT_MET, area=T_,
          evidence="Single-user local tool. Reachable from this computer only: binds to 127.0.0.1, and every request's Host "
                   "header is checked.",
          gap="No user identity anywhere. Nothing records or could record who did something.",
          checks=[NO_AUTH, LOOPBACK, HOST_GUARD]),
        C("164.312(a)(2)(ii)", "Access control", "Emergency access procedure", "R", NA, area=T_,
          gap="No stored ePHI that a clinician would need in an emergency."),
        C("164.312(a)(2)(iii)", "Access control", "Automatic logoff", "A", NOT_MET, area=T_,
          gap="No sessions, so nothing times out. Anyone at the unlocked computer has full access.", checks=[NO_AUTH]),
        C("164.312(a)(2)(iv)", "Access control", "Encryption and decryption (at rest)", "A", NOT_MET, area=T_,
          evidence="Data files are gitignored, so they are not committed.",
          gap="SQLite databases, the audit log and data files are plain files. Disk encryption (for example FileVault) would be "
              "an operating-system control that this repo cannot show.",
          checks=[NO_ENCRYPTION, has(".gitignore", "data/*")]),
        C("164.312(b)", "Audit controls", "(standard only)", "R*", PARTIAL, area=T_,
          evidence="Every tool call is appended to logs/audit.jsonl with tool name and counts only. Tests check that questions, "
                   "patient values and note text never reach the log. ROI and referral status changes are also kept as dated events.",
          gap="No user identity in entries. The log is a plain file: nothing stops it being edited or deleted, and there is no "
              "rotation or retention rule.",
          checks=[has("src/him_workbench/audit.py", 'LOG_PATH.open("a"'), modules_calling("audit.log_call", 8),
                  tests_named("audit", 6)]),
        C("164.312(c)(1)-(2)", "Integrity", "Mechanism to authenticate ePHI", "A", PARTIAL, area=T_,
          evidence="The ROI tracker enforces allowed status moves (no fulfilling before verification, no reopening) and input limits. "
                   "CDA files are parsed with DOCTYPE and ENTITY declarations refused.",
          gap="No checksums or signatures on stored data. Anyone with file access can edit the databases directly.",
          checks=[has("src/him_workbench/roi.py", "MOVES = {"), has("src/him_workbench/cda.py", "DOCTYPE")]),
        C("164.312(d)", "Person or entity authentication", "(standard only)", "R*", NOT_MET, area=T_,
          evidence="The Host and Origin checks stop a web page in another tab from driving the local app (DNS rebinding and "
                   "cross-site requests). POSTs must be JSON.",
          gap="Those checks are not authentication. Nothing verifies who is using the app.",
          checks=[NO_AUTH, HOST_GUARD, has("src/him_workbench/webapp.py", "abort(415)"), tests_named("lookalike|foreign_host", 2)]),
        C("164.312(e)(1)", "Transmission security", "Integrity controls (A), encryption (A)", "A", PARTIAL, area=T_,
          evidence="The web UI talks over plain HTTP, but only on the loopback interface, so it does not cross a network. Optional "
                   "outside lookups use HTTPS. Responses carry a Content-Security-Policy, no-store and nosniff.",
          gap="If the host binding were changed to a LAN address there would be no TLS. The SMART server URL can be overridden "
              "by an environment variable and is not forced to HTTPS in code, so an http:// override would send the bearer token in clear.",
          checks=[LOOPBACK, has("src/him_workbench/medlineplus.py", 'BASE = "https://'),
                  has("src/him_workbench/rxnorm.py", 'BASE = "https://'),
                  has("src/him_workbench/webapp.py", "Content-Security-Policy"),
                  has("src/him_workbench/smart_fhir.py", 'DEFAULT_ISS = "https://'),
                  lacks(("src/him_workbench/smart_fhir.py",), r"startswith\(.https")]),

        # ---------- 164.314 and 164.316 ----------
        C("164.314(a)", "Organizational requirements", "Business associate contracts or other arrangements", "R", NA, area=O_,
          gap="No business associates. Needed if real ePHI is handled by a vendor."),
        C("164.314(b)", "Organizational requirements", "Requirements for group health plans", "R", NA, area=O_,
          gap="Not a group health plan."),
        C("164.316(a)", "Policies and procedures", "(standard only)", "R*", PARTIAL, area=D_,
          evidence="README principles: synthetic data only, local by default, tool results are the source of truth. Enforced in part "
                   "by the pre-commit secret and term scans.",
          gap="Principles, not formal written policies and procedures.",
          checks=[has("README.md", "## Principles"), has("data/README.md", "Not allowed: anything from a real patient")]),
        C("164.316(b)(2)(i)", "Documentation", "Time limit (keep 6 years)", "R", ORG, area=D_,
          gap="Retention of required documentation. The git history keeps this repo's documents, but retention policy is an "
              "organizational decision."),
        C("164.316(b)(2)(ii)-(iii)", "Documentation", "Availability (R), updates (R)", "R", PARTIAL, area=D_,
          evidence="Documentation is in the repo, versioned in git.", gap="No scheduled review of the documentation."),
    ]


def run_checks(root: Path | str | None = None) -> list[Control]:
    """Build the rows and run every check. A row whose checks do not all hold gets status RE-CHECK."""
    root = Path(root) if root else ROOT
    controls = _controls()
    for c in controls:
        c.status = c.claim
        for chk in c.checks:
            ok, what = chk(root)
            (c.checked if ok else c.failed).append(what)
        if c.failed:
            c.status = RECHECK
    return controls


def summary(controls: list[Control]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in controls:
        out[c.status] = out.get(c.status, 0) + 1
    return out


def top_gaps(controls: list[Control]) -> list[Control]:
    """Technical-safeguard rows that are not met, required ones first."""
    rows = [c for c in controls if c.area == T_ and c.status in (NOT_MET, PARTIAL)]
    return sorted(rows, key=lambda c: (c.status != NOT_MET, c.kind not in ("R", "R*")))


def markdown_report(controls: list[Control]) -> str:
    s = summary(controls)
    order = [MET, PARTIAL, NOT_MET, NA, ORG, RECHECK]
    out = ["# HIPAA Security Rule self-assessment of HIM Workbench", "",
           "<!-- Generated by scripts/hipaa_assessment_cli.py --write. Do not edit by hand: a test fails if this file drifts from the code. -->",
           "",
           "**Synthetic data only. A self-assessment by the author, not an audit, certification or legal advice.** "
           "HIPAA applies to covered entities and business associates handling real ePHI; this project handles none. "
           "The question here is what would have to be true before real ePHI went near it.", "",
           "Each row is a standard or implementation specification from [45 CFR 164 Subpart C]"
           "(https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-C), with (R) required or (A) addressable "
           "as the rule's own matrix gives it. Software cannot show organizational, workforce or physical measures, so those rows say "
           "**Organizational** instead of claiming they are met. Rows with automated checks are verified against the repo each time "
           "this is generated: if a check stops holding the status becomes **RE-CHECK**, including for \"not met\" rows.", "",
           "## Result", "", "| Status | Rows |", "|---|---|"]
    out += [f"| {k} | {s[k]} |" for k in order if k in s]
    out += ["", "## Biggest technical gaps", ""]
    for c in top_gaps(controls):
        out.append(f"- **{c.cite} {c.spec if c.spec != '(standard only)' else c.standard}** ({c.status}): {c.gap}")
    out += ["", "None of this is a flaw for a local, single-user, synthetic-data learning project. It is the list of what would "
            "need to be built or arranged first: user accounts and unique IDs, automatic logoff, encryption at rest, a tamper-resistant "
            "audit log that records who, backups, and the written policies around them.", ""]
    for area in (A_, P_, T_, O_, D_):
        rows = [c for c in controls if c.area == area]
        out += [f"## {area} safeguards" if area in (A_, P_, T_) else f"## {area} requirements" if area == O_ else "## Policies and documentation",
                "", "| Citation | Standard / specification | Type | Status | Evidence | Gap |", "|---|---|---|---|---|---|"]
        for c in rows:
            what = c.standard if c.spec == "(standard only)" else f"{c.standard}: {c.spec}"
            kind = {"R": "R", "A": "A", "R*": "R (standard)"}[c.kind]
            ev = c.evidence or "-"
            gap = c.gap or "-"
            if c.failed:
                gap = f"CHECK FAILED: {'; '.join(c.failed)}. " + gap
            out.append(f"| {c.cite} | {what} | {kind} | {c.status} | {ev} | {gap} |")
        out.append("")
    out += ["## What is checked", "", "Automated checks run against the repo for the rows above, for example: the server binds to 127.0.0.1, "
            "every request's Host header is verified, POSTs must be JSON, no authentication code exists, no encryption library is used, "
            "audit logging is called from many modules and tested never to hold patient values or questions, data and logs are gitignored, "
            "and the pre-commit hook scans for secrets. See `src/him_workbench/hipaa_assessment.py`.", "",
            "## About the rule changing", "",
            "HHS has proposed rewriting the Security Rule, including removing the required/addressable distinction so that currently "
            "addressable items become mandatory. As of mid-2026 reporting this is still a proposal, not a final rule, with a final rule "
            "targeted for July 2027. This table uses the current designations. For how to carry out each standard, NIST SP 800-66 "
            "Revision 2 is the guidance; a per-row crosswalk to it is not included here.", ""]
    return "\n".join(out)


def text_report(controls: list[Control]) -> str:
    s = summary(controls)
    out = ["HIPAA Security Rule self-assessment of HIM Workbench (synthetic data; not an audit)", "",
           "  " + ", ".join(f"{k}: {v}" for k, v in s.items()), "", "Biggest technical gaps:"]
    out += [f"  [{c.status}] {c.cite} {c.standard}{'' if c.spec == '(standard only)' else ': ' + c.spec}\n      {c.gap}" for c in top_gaps(controls)]
    bad = [c for c in controls if c.failed]
    if bad:
        out += ["", "Rows to re-check (a check no longer holds):"]
        out += [f"  {c.cite}: {'; '.join(c.failed)}" for c in bad]
    out += ["", f"{len(controls)} rows. Full table: python scripts/hipaa_assessment_cli.py --write docs/hipaa-self-assessment.md"]
    return "\n".join(out)
