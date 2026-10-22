# HIM Workbench

Local-first tools for Health Information Management (HIM) work: find possible duplicate patient records, look up ICD-10-CM codes, and ask about both in plain English through a model running on your own machine.

**Synthetic data only. Not for clinical use, billing, or coding decisions.** This is a learning and portfolio project. Nothing here has been validated for real patient data.

## What it does

| Piece | What it does |
|---|---|
| Duplicate matcher | Scores pairs of patient records and labels each `match`, `review`, or `non-match`. Says why when it holds a pair back for a person to look at. |
| ICD-10-CM lookup | Looks up a code, or searches by plain-English text ("type 2 diabetes kidney"). Reads the public CDC/NCHS code file; the file is not bundled. |
| Assistant | A local Ollama model that answers by calling the two tools above. Every tool result is printed raw, so you can check the model's retelling against the source. |
| Data-quality report | Counts missing values and format problems (impossible dates, malformed zips, odd names) and exact same-name-and-birth-date groups. Counts only, never patient values. |
| Audit log | Every tool call is appended to `logs/audit.jsonl`: tool name and counts only, never patient data or question text. |

## Principles

- **Synthetic data only.** Synthea-generated patients. No real patient data, ever.
- **Local by default.** Models run through Ollama on your own machine. Nothing is sent to a cloud service.
- **Tool results are the source of truth.** The model may only answer code and duplicate questions through tools. A code in an answer that no tool returned gets flagged.
- **Say what you did.** Tools report only what they actually did. A search that dropped a word says which one.
- **Public sources only.** Built from published methods and public code sets.

## Setup

Needs Python 3.11+ and, for the assistant, [Ollama](https://ollama.com) running locally.

```
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest
pytest
```

### Get data (not included in the repo)

```
./scripts/generate_synthea.sh        # synthetic patients (needs Java 17 and the Synthea jar)
./scripts/fetch_icd10.sh             # ICD-10-CM code descriptions from CDC/NCHS
```

Synthea's jar location defaults to `~/Documents/Synthea/synthea-with-dependencies.jar`; set `SYNTHEA_JAR` to change it. Both downloads land in `data/`, which is gitignored.

### Try it

```
python scripts/icd10_cli.py E11.9
python scripts/icd10_cli.py "type 2 diabetes kidney"
python scripts/evaluate_matching.py          # single-error duplicates
python scripts/evaluate_hard.py              # two-error duplicates and look-alike people
python scripts/quality_report.py             # missing values and format problems
python scripts/model_check_cli.py --models gemma4:e4b,qwen3.5:9b   # fixed questions, pass/fail per model
ollama pull gemma4:e4b
python scripts/assistant_cli.py --model gemma4:e4b --demo-dups
```

`--demo-dups` injects 15 known duplicates (ids start with `dup-`) so there is something to find. Set `HIM_MODEL` to skip the `--model` flag, or `OLLAMA_HOST` to use Ollama on another machine.

### Web UI (optional)

```
pip install -e ".[ui]"
python scripts/ui_cli.py --model gemma4:e4b --demo-dups     # http://127.0.0.1:5050
```

Tabs on the left (Patients, Duplicates, Data quality, Codes), a fixed bar on top, and the assistant prompt in the top right (press `/`). The answer panel shows the model's reply next to the raw tool results, so you can check it. It binds to 127.0.0.1 only, refuses other Host headers, accepts POSTs only as same-origin JSON, and loads nothing from the internet.

**Your own textures:** the page textures are generated with CSS. To use your own images, put `patients.jpg`, `duplicates.jpg`, `quality.jpg`, `codes.jpg` or `header.jpg` in `data/ui-textures/` (gitignored). Only use images you have the right to use.

## How the matcher decides

Blocking (only compare records that share a key such as same last-name sound and birth year), then weighted field comparison in the Fellegi-Sunter style: each field adds or subtracts log-likelihood points, names use Jaro-Winkler similarity, and a small nickname table treats Bob and Robert as the same first name. Score of 12 or more is `match`, 5 or more is `review`.

Two safety rules keep a high score from becoming an automatic match:

- First names must be identical after nickname normalization. Look-alike names (Jaden / Jayden) and typos go to `review`.
- Dates of birth must not clearly conflict. Same name and different birth year (parent and child) goes to `review`.

A pair held back this way carries a stated reason, for example `first names not identical (typo or look-alike name)`.

The agreement probabilities in `matching.py` are illustrative defaults, not tuned or validated.

## What the evaluation showed

Measured on 500 Synthea patients (Massachusetts, default seed) with duplicates injected by the scripts above. These are counts from one synthetic dataset, not a benchmark, and real records have messier errors than injected ones.

Single-error duplicates (34 injected into 500 originals; one change per copy):

| Change | Auto-matched | Review | Missed |
|---|---|---|---|
| Birth month and day swapped | 3 | 0 | 0 |
| Birth year off by one | 9 | 0 | 0 |
| First-name typo | 1 | 6 | 0 |
| Last-name typo | 5 | 0 | 0 |
| Sex and zip missing | 10 | 0 | 0 |
| **Total** | **28 (82%)** | **6 (18%)** | **0** |

No false matches or false review flags among the 500 different people. The nickname case did not appear in this run's sample, so it is covered by unit tests but was not measured here.

Two-error duplicates (95 cases, e.g. a typo plus a swapped birth month and day):

| Outcome | Count |
|---|---|
| Auto-matched | 55 |
| Sent to review | 33 |
| Missed | 7 |

Look-alike *different* people (twins, parent and child with the same name, siblings, same-name strangers; 60 each):

| Outcome | Count |
|---|---|
| Wrongly auto-matched | 0 |
| Held for review (twins, parent and child) | 120 |
| Correctly left alone (siblings, strangers) | 120 |

Run `evaluate_matching.py` and `evaluate_hard.py` yourself for current numbers.

## Known limits

- **Missed duplicates.** All 7 misses were records with two different errors in the birth date (year off by one plus month and day swapped). Blocking never pairs them. Widening the blocking keys flooded the review queue with same-name strangers, so it was left as is.
- **Typos always go to a person.** A misspelled first name is never auto-matched, by design. That means more review work.
- **Small lookup tables.** The nickname table and the plain-English synonym list (kidney/renal, heart/cardiac, and a few more) are short and hand-written.
- **Search relaxation.** If no code matches every word, the search retries without filler words like "complications" and words that match nothing, and says what it dropped. It never drops a real word to force an answer.
- **Search shows 10 codes at a time.** When more match, the result says "10 of N shown" and the assistant is told to say the list is partial. Ranking is by how directly the words match, with one hand-written boost: "high blood pressure" ranks essential (primary) hypertension first. Other lay phrases have no such boost.
- **Quality checks are about format, not truth.** A birth date can be a valid date and still be the wrong one. The report finds malformed and missing values; it cannot find wrong ones.
- **No CPT.** CPT codes are licensed by the AMA and are not included.
- **Models are imperfect.** Small local models can miscount, skip a tool, or reword your question. That is why raw tool results are printed and why scan results state their own totals. The code-grounding check can occasionally flag text that only looks like a code (for example "B12").
- **Code sets change.** ICD-10-CM is updated every October 1 (sometimes April 1). Results name the file they came from.

## Models tried

Informal checks of tool use on the duplicate scan and one code search, on one Mac. Not a benchmark. `scripts/model_check_cli.py` reruns a fixed set of nine questions on any model and checks each answer against what the tools returned (right tool called, counts match, partial lists labeled, no invented codes, honest when a tool errors). The checks are simple string tests; they catch the failures seen so far, not writing quality.

| Model | What happened |
|---|---|
| `gemma4:e4b` | Called the tools, repeated the totals correctly, listed the review reasons, and said which search word was ignored. Current default. |
| `qwen3.5:9b` | Called the tools and listed every code, but left the review reasons out of its table and reworded the question before searching. |
| `qwen2.5:7b` | Called the tools, got labels right, misread score columns as differences before totals were added to the tool output. |
| `ministral-3` | Did not call the tool on the scan request. |

## Layout

```
src/him_workbench/   package code (matching, icd10, quality, assistant, audit, web UI)
tests/               tests with invented fixtures only
scripts/             data download, evaluation, CLI, and repository checks
data/                local synthetic data (gitignored, see data/README.md)
logs/                audit log (gitignored)
```

`scripts/` also holds a pre-commit check that scans files going into git for secret-looking strings and for any terms you list in a local, gitignored `.scrub-terms` file. Install it with `scripts/install_hooks.sh`.

## Data sources

- [Synthea](https://github.com/synthetichealth/synthea) synthetic patients (Apache 2.0).
- ICD-10-CM code descriptions published by CDC/NCHS.

## License

MIT. See `LICENSE`.
