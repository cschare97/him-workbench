# HIM Workbench

Local-first tools for Health Information Management workflows, built on public standards (FHIR, HL7, ICD-10-CM, CMS documentation).

## Status

Early scaffold. Nothing here is production software and nothing here is validated for clinical use.

## Principles

- **Synthetic data only.** Uses Synthea and OpenEMR demo data. No real patient data, ever.
- **Local by default.** Models run through Ollama on your own machine.
- **Audit everything.** Every tool call is logged.
- **No claimed actions.** The tool reports only what a function actually did.
- **Public sources only.** Built from published standards, not from any employer's internal material.

## Planned modules

| Module | Purpose |
|---|---|
| `matching` | Duplicate patient record detection |
| `codes` | ICD-10-CM / CPT lookup |
| `quality` | Data quality reports |
| `audit` | Append-only tool call log |

## Layout

```
src/him_workbench/   package code
tests/               tests, synthetic fixtures only
data/                local synthetic data (gitignored, see data/README.md)
scripts/             helper scripts
```

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## License

TBD. Pick one before publishing.
