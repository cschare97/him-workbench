# Hosted demo on Render

The hosted copy is a read-only slice of the app: the care-gaps pages, About and Data sources. It
runs `him_workbench.public`, which turns on public mode (`HIM_PUBLIC=1`):

- Only `/care-gaps`, `/care-gaps/explain`, `/about`, `/sources` and `/static/` are served, GET only.
  Everything else is a 404, every write is refused, and `/` goes to `/care-gaps`.
- Requests are accepted only for localhost and the hostnames in `HIM_ALLOWED_HOSTS`.
- No model is ever called. The Explain page shows summaries written ahead of time by
  `him_workbench.care_gaps.precompute_summaries` and re-checks each one before showing it.
- NCQA spec text (CMS122, CMS131) is never downloaded or shown. The build fetches the CMS951 spec
  only, which NKF's terms allow showing with its notice.
- Each client gets 120 requests a minute (static files not counted; `HIM_RATE_LIMIT` changes it), then a
  429 with `Retry-After`. The client is identified by `True-Client-IP` / `CF-Connecting-IP`, which
  Cloudflare (in front of Render) sets itself, never by `X-Forwarded-For`, which Render appends to
  without clearing, so a client could fake it. The count is in memory, so it resets on each deploy or
  wake-up, which is fine for one worker.
- Data is synthetic (Synthea). Nothing is written to disk at runtime.

## Before deploying

1. On your own machine, rebuild the snapshot and its summaries, then commit `data/care_gaps.json` and
   `data/data_quality.json`. `report` always rebuilds the snapshot without summaries, so run
   `precompute_summaries` right after it, every time:

   ```bash
   python3 -m him_workbench.care_gaps.report ~/synthea/output_caregap_injected --json data/care_gaps.json --scenarios data/injected_gaps.json
   python3 -m him_workbench.care_gaps.precompute_summaries --model qwen2.5:7b
   python3 -m him_workbench.care_gaps.data_quality ~/synthea/output_caregap_injected --json data/data_quality.json
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

## Deploys from Blueprint changes

A merge that changes `render.yaml` makes Render apply the Blueprint, which starts a deploy on its own
even with manual deploys. Check the deploy list before clicking Manual Deploy.

## Free plan notes

The service is on Render's free plan: it sleeps after 15 minutes without traffic and takes about a
minute to wake on the next visit. Python is pinned to 3.12 by `.python-version` (CI tests 3.11 and 3.12).
