# care_gaps: diabetes quality-measure gaps on synthetic patients

Code: `src/him_workbench/care_gaps/` (part of the `him_workbench` package, so CI's Bandit scan covers it). Tests: `tests/test_care_gaps_*.py`. Commands below run from the repo root.

Finds which diabetic patients fail three public diabetes quality measures, says why, and sorts each gap into a type. The gap type separates care that didn't happen from care that probably happened but isn't on file as structured data. Uses Synthea synthetic FHIR data only. No real patient data.

## Measures (2026 eCQM versions, from ecqi.healthit.gov)

| Measure | What "met" means |
|---|---|
| CMS122v14 Glycemic Status Assessment > 9% | **Failing.** This is an inverse measure: the latest A1c in the period is above 9%, or there's no A1c at all |
| CMS131v14 Diabetes: Eye Exam | Retinal exam in the period, or the prior year if the patient has no retinopathy |
| CMS951v4 Kidney Health Evaluation | Both an eGFR and a urine albumin/creatinine ratio in the period. Ages 18-85, diabetes diagnosed before the period starts, and ESRD or CKD stage 5 excluded |

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
# (run these from the repo root)
python3 -m him_workbench.care_gaps.inject_gaps ~/synthea/output_caregap/fhir ~/synthea/output_caregap_injected --log data/injected_gaps.json

# 3. Build the snapshot the /care-gaps page reads
python3 -m him_workbench.care_gaps.report ~/synthea/output_caregap_injected --json data/care_gaps.json --scenarios data/injected_gaps.json

# 4. Data-quality notes (a separate file; never touches care_gaps.json)
python3 -m him_workbench.care_gaps.data_quality ~/synthea/output_caregap_injected --json data/data_quality.json

# Tests
python3 -m pytest tests -q -k care_gaps
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
| CMS951 Kidney health | 6 (all care), denominator 94 | 6 (all care), denominator 94 |

On the unmodified baseline, the note check flags zero documentation gaps, so it isn't firing on Synthea's normal visit notes.

The five baseline patients with no A1c are also missing both kidney labs: one root cause (no lab work drawn this year), not three separate problems.

CMS951's denominator is 94, not 99: five patients were diagnosed with diabetes during 2026, and CMS951 only counts diabetes present at the start of the period.

## Evidence behind each result

Every result in the snapshot carries the facts behind it, numbered `E1`, `E2`, and so on: age and the measure's age range, diabetes diagnosis date, latest outpatient visit, lab results in the period (newest first, up to five), the last result before the period when there are none, matching note excerpts, open orders, and exclusions. The evidence is gathered separately from the pass/fail logic, so it can never change a result. It lets the page explain a gap without the Synthea files, which matters for a hosted demo.

Example: one baseline patient fails CMS122 with no A1c in 2026, but her evidence shows an A1c on 2025-12-17. She isn't uncontrolled. Her test landed two weeks before the period started.

## Explaining a gap

`explain.py` answers "why is this patient failing this measure?":

```bash
python3 -m him_workbench.care_gaps.explain --patient "Carlos Wolf" --measure CMS122                     # no model
python3 -m him_workbench.care_gaps.explain --patient "Carlos Wolf" --measure CMS122 --model qwen2.5:7b  # local Ollama
```

1. It picks the spec sections that apply by rule (a missing A1c points at CMS122's Numerator), not by search. There are only a few dozen sections, and the right one is known from the result.
2. A local model writes 3 to 5 sentences with three kinds of source tags: `[E#]` for the patient's evidence, `[R#]` for this project's own rules and next steps, and `[S#]` only for what the steward's published spec text says. The `[R#]` tags were added after testing showed the model citing spec sections for statements that came from this project, because those statements had no tag of their own.
3. A grounding check runs before anything is shown. Every citation must exist, and every number in the answer must appear in the evidence or the cited sections, so a made-up or calculated number gets the answer rejected.
4. If the check fails, or no model is running, it shows the built-in explanation from the measure logic, and says why. It never shows an unchecked answer as if it were checked.

The model only rewords facts the code already found. The pass/fail decision always comes from the measure logic, never from the model.

**What the check can't catch.** It catches made-up numbers, calculated numbers, fake sources and labels that aren't sources. It can't tell whether a real source actually supports a sentence, or whether a conclusion built from real facts is wrong. Testing with `qwen2.5:7b` showed exactly this: an early version of the prompt said the measure status was "met" (which means failing on the inverse CMS122), and the model wrote that the patient's measure "is met" because an A1c in a note was below 9%. Every number in that answer was real, and the conclusion was backwards. Two fixes came from that test: the prompt now states the outcome in plain words and explains why a value in a note doesn't count, and the check rejects "is met" for a patient with a gap. Because no check like this is complete, the measure logic's own explanation and the evidence are always shown next to any model summary.

### On the page

Each gap on `/care-gaps` has an **Explain** link. The page shows the measure logic's explanation first, then an optional summary from any local model you pick, then the project rules, the evidence and the spec sections with their notices. Set `HIM_PUBLIC=1` to apply the public-site rules (no NCQA text shown or sent to a model).

### Summaries for a hosted demo

A hosted copy has no local model, so summaries are written ahead of time on your own machine:

```bash
python3 -m him_workbench.care_gaps.precompute_summaries --model qwen2.5:7b   # after report.py, with Ollama running
```

It asks for each gap's summary in public mode (no NCQA spec text in the prompt) and stores only answers that pass the grounding check, with the model name and date. Rejected answers are counted and left out. The Explain page re-runs the check on a stored summary before showing it, so an edited or outdated summary is not displayed. Run it again after `report.py`, which rebuilds the snapshot without summaries.

Result with `qwen2.5:7b` (seed 2026): **57 of 59 gaps got a summary that passed the check**, 2 were rejected and 0 calls failed. Both rejections cited `[S1]` in public mode, where NCQA's text isn't provided, so there was no S1 to cite; the same answers pass in local mode, where S1 exists. A hand spot-check of one stored summary per gap type (missing care, out of range, documentation, open order) found the right conclusion and next step in all four, but citations sometimes pointed at a neighboring rule (for example `[R1]` where `[R2]` was meant). The check can't catch that, which is why the page always shows the measure logic and the evidence next to a summary.

The hosted demo (`him_workbench.public`) shows these stored summaries and never calls a model. See `docs/deploy-render.md`.

## Official spec text

`spec_loader.py` downloads the 2026 specifications from the eCQI Resource Center and splits them into sections (Initial Population, Denominator Exclusions, Numerator, Guidance, and so on):

```bash
python3 -m him_workbench.care_gaps.spec_loader fetch            # run from the repo root
python3 -m him_workbench.care_gaps.spec_loader show CMS122 Numerator
```

The specs are copyrighted by their measure stewards, and their terms differ:

- **NCQA (CMS122, CMS131).** The 2026 notice allows use for internal, noncommercial purposes without approval. All other uses must be approved by NCQA.
- **National Kidney Foundation (CMS951).** The measures may be reproduced and distributed, without modification, for noncommercial purposes.

So:

- Spec text is never committed to this repo. It is saved under `data/cache/` (gitignored), and each person downloads it from eCQI for their own use.
- When spec text is shown, it is shown exactly as published, never paraphrased or edited, with that measure's copyright notice and a link to the source page.
- A public deployment does not show NCQA's text for CMS122 and CMS131. It links to the eCQI page instead (`PUBLIC_TEXT_OK` in `spec_loader.py`).
- The measure logic in this folder is original code written from the public specifications. It does not copy their text.

Stewards update their terms between versions, so re-read each notice after a new fetch. This is a good-faith reading of the published terms, not legal advice.

The tests use a made-up page with the same layout, not real spec text.

Checking the measure code against the spec text found a real bug: CMS951 had been using CMS122's age range (18-75) and had no ESRD or CKD stage 5 exclusion. Both are fixed and covered by tests.

## Data-quality notes

`data_quality.py` checks every diabetic patient's A1c history for two things that look like source-data
problems rather than care problems, and writes them to `data/data_quality.json`. It never changes a
measure result and never rebuilds `care_gaps.json`, so stored summaries are safe.

- **Under 4%.** Possible in real life (for example over-treatment) but rare enough that a reviewer would
  ask. The check calls out when the most recent A1c in the period is under 4%, because that is the value
  CMS122 grades. Result (seed 2026): 19 of 123 diabetic patients have a value under 4%; for 10 it is the
  most recent in the period, and **9 of them pass CMS122 on it** (the 10th is excluded).
- **Same value 3+ times in a row.** Real lab results almost never repeat exactly. Synthea carries values
  forward: 96 of 123 patients have at least one run, some over 90 results long. Because it is nearly
  universal, the page shows it as one note, not a tag on each patient.

The care-gaps page shows a summary and the list of patients with values under 4% (and tags those rows);
each Explain page shows that patient's notes. A real HIM review would confirm these values before
trusting the measure.
