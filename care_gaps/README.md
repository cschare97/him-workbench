# care_gaps: diabetes quality-measure gaps on synthetic patients

Finds which diabetic patients fail three public diabetes quality measures, says why, and sorts each gap into a type. The gap type separates care that didn't happen from care that probably happened but isn't on file as structured data. Uses Synthea synthetic FHIR data only. No real patient data.

## Measures (2026 eCQM versions, from ecqi.healthit.gov)

| Measure | What "met" means |
|---|---|
| CMS122v14 Glycemic Status Assessment > 9% | **Failing.** This is an inverse measure: the latest A1c in the period is above 9%, or there's no A1c at all |
| CMS131v14 Diabetes: Eye Exam | Retinal exam in the period, or the prior year if the patient has no retinopathy |
| CMS951v4 Kidney Health Evaluation | Both an eGFR and a urine albumin/creatinine ratio in the period |

These are simplified teaching versions, not certified measure logic. `report.py` prints the full list of simplifications every run.

## Gap types

These labels are this project's own triage, not part of any eCQM.

| Type | Meaning | What to do |
|---|---|---|
| Care gap | The test or exam didn't happen, or the result is out of range | Patient outreach |
| Documentation gap | Nothing structured is on file, but a note in the period mentions it | Chart review: find the result and enter it as structured data |
| Open order | It was ordered in the period, but no result came back | Close the loop with the lab or the patient |

A documentation gap comes from a keyword match in a note, so it means "worth a chart review", not proof the care happened.

## Run it

```bash
# 1. Generate the cohort (in your Synthea folder)
./run_synthea -s 2026 -p 1500 -a 18-75 -r 20261120 --exporter.baseDirectory=./output_caregap/ California

# 2. Add the test scenarios (writes a new folder; the source is never changed)
cd care_gaps
python3 inject_gaps.py ~/synthea/output_caregap/fhir ~/synthea/output_caregap_injected --log injected_gaps.json

# 3. Build the snapshot the /care-gaps page reads
python3 report.py ~/synthea/output_caregap_injected --json ../data/care_gaps.json --scenarios injected_gaps.json

# Tests
python3 -m pytest tests -q
```

To see the unmodified baseline, run `report.py` on `~/synthea/output_caregap/fhir` without `--scenarios`.

## Why inject gaps

Synthea's simulated care is close to perfect. On its own, no patient fails CMS122 for a high A1c, and nothing is ever documented in the wrong place. Real clinics have both. `inject_gaps.py` copies the diabetic patients and changes 16 of them on purpose, using a fixed seed so every run gives the same result:

| Scenario | Patients | Change | Expected gap |
|---|---|---|---|
| `a1c_in_note` | 4 | Structured 2026 A1c removed; a note says an outside lab drew it | Documentation |
| `outside_eye_exam` | 4 | Retinal exam records removed; a note says it was done at an outside office | Documentation |
| `open_order` | 3 | Structured 2026 A1c removed; an A1c order with no result added | Open order |
| `uncontrolled` | 5 | Most recent A1c raised to between 9.3% and 11.8% | Care |

Every change is listed in `injected_gaps.json`, and every added note or order is tagged `[Test scenario added by inject_gaps.py]`. Output bundles keep only the resource types the measures read, so they are about a quarter of the size.

## Results (seed 2026)

| Measure | Baseline gaps | With test scenarios |
|---|---|---|
| CMS122 A1c > 9% | 5 (all care) | 17 (10 care, 4 documentation, 3 open order) |
| CMS131 Eye exam | 32 (all care) | 36 (32 care, 4 documentation) |
| CMS951 Kidney health | 6 (all care) | 6 (all care) |

On the unmodified baseline, the note check flags zero documentation gaps, so it isn't firing on Synthea's normal visit notes.

The five baseline patients with no A1c are also missing both kidney labs: one root cause (no lab work drawn this year), not three separate problems.

## Data-quality note

Some Synthea A1c values are clinically implausible (below 4%). A real HIM review would flag these before trusting the measure.
