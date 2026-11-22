# care_gaps: diabetes quality-measure gaps on synthetic patients

Finds which diabetic patients fail three public diabetes quality measures, and says why, using Synthea synthetic FHIR data. No real patient data is used.

## Measures (2026 eCQM versions, from ecqi.healthit.gov)

| Measure | What "met" means |
|---|---|
| CMS122v14 Glycemic Status Assessment > 9% | **Failing.** This is an inverse measure: the latest A1c in the period is above 9%, or there's no A1c at all |
| CMS131v14 Diabetes: Eye Exam | Retinal exam in the period, or the prior year if the patient has no retinopathy |
| CMS951v4 Kidney Health Evaluation | Both an eGFR and a urine albumin/creatinine ratio in the period |

These are simplified teaching versions, not certified measure logic. `report.py` prints the full list of simplifications every run.

## Run it

```bash
# Generate the cohort (in your Synthea folder)
./run_synthea -s 2026 -p 1500 -a 18-75 -r 20261120 --exporter.baseDirectory=./output_caregap/ California

# Run the measures
cd care_gaps
python3 report.py ~/synthea/output_caregap/fhir --csv care_gaps.csv

# Tests
python3 -m pytest care_gaps/tests -q
```

## Baseline results (seed 2026, before any injected gaps)

| Measure | Denominator | Result |
|---|---|---|
| CMS122 A1c > 9% | 99 | 5 failing (5.1%), all from a missing A1c |
| CMS131 Eye exam | 99 | 67 passing (67.7%) |
| CMS951 Kidney health | 99 | 93 passing (93.9%) |

Synthea's simulated care is close to perfect, so no patient fails CMS122 for a high A1c. A later step adds documented, reproducible gaps, such as an A1c that exists only in a scanned note instead of a structured result.

## Data-quality note

Some Synthea A1c values are clinically implausible (below 4%). A real HIM review would flag these before trusting the measure.
