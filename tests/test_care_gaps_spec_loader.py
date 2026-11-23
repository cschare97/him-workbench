"""Parser tests on a made-up page with the same layout as the eCQI spec pages.

The real spec text is copyrighted by the measure stewards, so it is never copied into this repo.
"""

import pytest

from him_workbench.care_gaps.spec_loader import SpecError, parse_spec


def row(label, value):
    return (f'<tr><th scope="row" class="row-header"><span class="td_label">{label}</span></th>'
            f'<td style="width:80%" colspan="3"><div><pre>{value}</pre></div></td></tr>')


PAGE = "<table>" + "".join([
    row("eCQM Title", "Example Measure"),
    row("eCQM Version Number", "1.0.000"),
    row("Measure Steward", "Example Steward"),
    row("Copyright", "Copyright 2025 Example Steward. All Rights Reserved."),
    row("Disclaimer", "Example disclaimer text."),
    row("Initial Population", "Patients 18-75 years of age"),
    row("Numerator", "Result is &gt;9.0% or is missing"),
    row("Reference", "first"),
    row("Reference", "second"),
    row("Guidance", "Line one.\n\nLine two."),
]) + "</table>"


def test_parses_sections_and_notice():
    spec = parse_spec(PAGE, "CMSX", "https://example.test/x.html")
    assert spec["meta"]["Copyright"].startswith("Copyright 2025 Example Steward")
    assert spec["sections"]["Initial Population"] == "Patients 18-75 years of age"
    assert spec["url"] == "https://example.test/x.html"


def test_text_is_kept_verbatim_apart_from_entities():
    spec = parse_spec(PAGE, "CMSX", "u")
    assert spec["sections"]["Numerator"] == "Result is >9.0% or is missing"
    assert spec["sections"]["Guidance"] == "Line one.\n\nLine two."


def test_unneeded_rows_are_dropped():
    spec = parse_spec(PAGE, "CMSX", "u")
    assert "Reference" not in spec["sections"] and "Reference" not in spec["meta"]


def test_missing_copyright_is_an_error_not_a_silent_skip():
    with pytest.raises(SpecError, match="Copyright"):
        parse_spec(PAGE.replace("Copyright", "Something"), "CMSX", "u")


def test_ncqa_text_is_not_marked_for_public_display():
    from him_workbench.care_gaps.spec_loader import PUBLIC_TEXT_OK
    assert "CMS122" not in PUBLIC_TEXT_OK and "CMS131" not in PUBLIC_TEXT_OK
