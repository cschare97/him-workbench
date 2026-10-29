from pathlib import Path

from him_workbench.hipaa_assessment import (MET, NOT_MET, RECHECK, ROOT, markdown_report, run_checks, summary,
                                            text_report, top_gaps)


def test_every_row_has_a_citation_type_and_claim():
    rows = run_checks()
    assert len({(c.cite, c.spec) for c in rows}) == len(rows)          # no duplicate rows
    for c in rows:
        assert c.cite.startswith("164.3") and c.kind in ("R", "A", "R*") and c.claim and c.area


def test_covers_every_standard_in_the_security_rule_matrix():
    cites = " ".join(c.cite for c in run_checks())
    for sec in ["164.308(a)(1)", "164.308(a)(2)", "164.308(a)(3)", "164.308(a)(4)", "164.308(a)(5)", "164.308(a)(6)",
                "164.308(a)(7)", "164.308(a)(8)", "164.308(b)(1)", "164.310(a)(2)", "164.310(b)", "164.310(c)", "164.310(d)(2)",
                "164.312(a)(2)(i)", "164.312(a)(2)(ii)", "164.312(a)(2)(iii)", "164.312(a)(2)(iv)", "164.312(b)",
                "164.312(c)(1)", "164.312(d)", "164.312(e)(1)", "164.314(a)", "164.314(b)", "164.316(a)", "164.316(b)(2)"]:
        assert sec in cites, sec


def test_the_repo_as_it_is_passes_its_own_checks():
    bad = [(c.cite, c.failed) for c in run_checks() if c.status == RECHECK]
    assert bad == []


def test_no_authentication_is_reported_not_hidden():
    by_cite = {c.cite: c for c in run_checks()}
    assert by_cite["164.312(a)(2)(i)"].status == NOT_MET and by_cite["164.312(d)"].status == NOT_MET
    assert by_cite["164.312(a)(2)(iv)"].status == NOT_MET
    assert MET not in summary(run_checks())          # nothing is claimed as fully met


def test_status_becomes_recheck_when_the_code_stops_showing_it(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/ui_cli.py").write_text('app.run(host="0.0.0.0")')      # no longer loopback
    (tmp_path / "src/him_workbench").mkdir(parents=True)
    rows = {c.cite: c for c in run_checks(tmp_path)}
    assert rows["164.312(a)(2)(i)"].status == RECHECK and any("host=" in f for f in rows["164.312(a)(2)(i)"].failed)
    assert rows["164.312(e)(1)"].status == RECHECK


def test_a_not_met_row_is_rechecked_when_a_login_appears(tmp_path):
    (tmp_path / "src/him_workbench").mkdir(parents=True)
    (tmp_path / "src/him_workbench/webapp.py").write_text("@login_required\ndef x(): pass")
    row = {c.cite: c for c in run_checks(tmp_path)}["164.312(a)(2)(i)"]
    assert row.status == RECHECK and any("login_required" in f for f in row.failed)


def test_top_gaps_lists_required_unmet_technical_rows_first():
    gaps = top_gaps(run_checks())
    assert gaps[0].status == NOT_MET and all(c.area == "Technical" for c in gaps)


def test_committed_report_matches_the_code():
    """docs/hipaa-self-assessment.md is generated. If this fails: python scripts/hipaa_assessment_cli.py --write docs/hipaa-self-assessment.md"""
    doc = Path(ROOT / "docs/hipaa-self-assessment.md").read_text(encoding="utf-8")
    assert doc == markdown_report(run_checks())


def test_reports_say_what_they_are():
    md = markdown_report(run_checks())
    assert "not an audit" in md and "Organizational" in md and "proposal, not a final rule" in md
