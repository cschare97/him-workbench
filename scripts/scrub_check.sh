#!/usr/bin/env bash
# Scans the repo for terms listed in .scrub-terms (one per line, case-insensitive).
# .scrub-terms is gitignored. Create it yourself with the names/terms that must never appear.
# Run before every commit and before publishing.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f .scrub-terms ]; then
  echo "No .scrub-terms file. Create one (one term per line) first." >&2
  exit 2
fi
if grep -rniF -f .scrub-terms --exclude-dir=.git --exclude-dir=.venv --exclude=.scrub-terms .; then
  echo "SCRUB CHECK FAILED: terms found above." >&2
  exit 1
fi
echo "Scrub check passed."
