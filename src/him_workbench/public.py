"""WSGI entry point for the read-only hosted demo (for example, on Render).

    gunicorn him_workbench.public:app

Public mode serves only the care-gaps pages, About and Data sources, GET only. It never calls a
model (summaries are written ahead of time by care_gaps/precompute_summaries.py) and never shows
NCQA spec text. Set HIM_ALLOWED_HOSTS to the site's hostname, or every request gets a 403.
"""
import os

os.environ["HIM_PUBLIC"] = "1"  # this entry point is always public, whatever the environment says

from .webapp import create_app  # noqa: E402  (must come after HIM_PUBLIC is set)

app = create_app()
