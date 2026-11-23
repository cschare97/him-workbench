"""Tests for the A1c data-quality check. Hand-built histories, no Synthea files."""
from datetime import date

from him_workbench.care_gaps.data_quality import check, low_values, repeat_runs
from him_workbench.care_gaps.fhir_load import A1C_LOINC, Condition, Observation, PatientFacts


def patient(values):
    facts = PatientFacts("p1", "Test Patient", date(1970, 1, 1), None)
    facts.conditions.append(Condition("44054006", "Diabetes mellitus type 2", date(2010, 1, 1), None))
    for day, value in values:
        facts.observations.append(Observation(A1C_LOINC, day, value))
    return facts


def test_low_values_use_a_strict_cutoff():
    history = [(date(2026, 1, 1), 3.99), (date(2026, 2, 1), 4.0), (date(2026, 3, 1), 6.2)]
    assert low_values(history) == [(date(2026, 1, 1), 3.99)]


def test_repeat_runs_need_three_in_a_row():
    d = [date(2025, m, 1) for m in range(1, 9)]
    history = [(d[0], 6.1), (d[1], 6.1), (d[2], 7.0), (d[3], 5.5), (d[4], 5.5), (d[5], 5.5), (d[6], 5.5), (d[7], 6.0)]
    assert repeat_runs(history) == [(5.5, d[3], d[6], 4)]      # the pair of 6.1 is not a run


def test_repeat_run_at_the_end_is_counted():
    d = [date(2025, m, 1) for m in range(1, 4)]
    assert repeat_runs([(day, 6.6) for day in d]) == [(6.6, d[0], d[2], 3)]


def test_clean_history_is_not_flagged():
    assert check(patient([(date(2026, 3, 1), 6.2), (date(2026, 9, 1), 7.1)])) is None


def test_latest_in_period_low_is_called_out_first():
    result = check(patient([(date(2025, 6, 1), 6.0), (date(2026, 3, 1), 5.9), (date(2026, 10, 1), 3.2)]))
    assert result["latest_in_period_low"] is True and result["low_in_period"] == 1
    assert result["notes"][0].startswith("Most recent A1c in the measurement period is 3.2% on 2026-10-01")


def test_old_low_value_is_not_the_graded_one():
    result = check(patient([(date(2022, 4, 1), 3.9), (date(2026, 5, 1), 6.4)]))
    assert result["low_count"] == 1 and result["low_in_period"] == 0 and result["latest_in_period_low"] is False


def test_missing_values_and_dates_are_skipped():
    facts = patient([(date(2026, 1, 1), 6.0)])
    facts.observations += [Observation(A1C_LOINC, None, 2.0), Observation(A1C_LOINC, date(2026, 2, 1), None)]
    assert check(facts) is None


def test_many_runs_are_summarized():
    values = []
    day = date(2019, 1, 1).toordinal()
    for i, (value, n) in enumerate([(2.4, 3), (2.5, 6), (2.6, 3), (2.7, 4)]):
        for _ in range(n):
            values.append((date.fromordinal(day), value))
            day += 7
    result = check(patient(values))
    runs = [n for n in result["notes"] if "times in a row" in n]
    assert len(runs) == 2 and "6 times" in runs[0] and "4 times" in runs[1]
    assert result["notes"][-1] == "2 more runs of repeated values" and result["repeat_runs"] == 4
