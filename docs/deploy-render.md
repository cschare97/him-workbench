# Hosted demo on Render

The hosted copy is a read-only slice of the app: the care-gaps pages, About and Data sources. It
runs `him_workbench.public`, which turns on public mode (`HIM_PUBLIC=1`):

- Only `/care-gaps`, `/care-gaps/explain`, `/about`, `/sources` and `/static/` are served, GET only.
  Everything else is a 404, every write is refused, and `/` goes to `/care-gaps`.
- Requests are accepted only for localhost and the hostnames in `HIM_ALLOWED_HOSTS`.
- No model is ever called. The Explain page shows summaries written ahead of time by
  `care_gaps/precompute_summaries.py` and re-checks each one before showing it.
- NCQA spec text (CMS122, CMS131) is never downloaded or shown. The build fetches the CMS951 spec
  only, which NKF's terms allow showing with its notice.
- Data is synthetic (Synthea). Nothing is written to disk at runtime.

## Before deploying

1. On your own machine, rebuild the snapshot and its summaries, then commit `data/care_gaps.json`:

   ```bash
   cd care_gaps && python3 report.py ~/synthea/output_caregap_injected --json ../data/care_gaps.json --scenarios injected_gaps.json && cd ..
   python3 care_gaps/precompute_summaries.py --model qwen2.5:7b
   ```

2. Try public mode locally:

   ```bash
   HIM_PUBLIC=1 python -c "from him_workbench.webapp import create_app; create_app().run(port=5051)"
   ```

   Open http://127.0.0.1:5051. Only Care gaps, Data sources and About should appear.

## First deploy

1. In Render: **New > Blueprint**, pick this repository. Render reads `render.yaml`.
2. When it asks for `HIM_ALLOWED_HOSTS`, enter the hostname it will use:
   `him-workbench-care-gaps.onrender.com` (the service name plus `.onrender.com`; if Render
   shows a different URL after creating the service, update the variable to match).
3. Deploys are manual (`autoDeployTrigger: off`). Use **Manual Deploy** after merging to main.

If every page returns 403, `HIM_ALLOWED_HOSTS` doesn't match the hostname in the browser's address bar.

## Free plan notes

The service is on Render's free plan: it sleeps after 15 minutes without traffic and takes about a
minute to wake on the next visit. Python is pinned to 3.12 by `.python-version` (CI tests 3.11 and 3.12).
