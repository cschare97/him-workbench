#!/usr/bin/env bash
# Scans files git would commit (tracked + untracked-but-not-ignored) for terms in .scrub-terms.
# .scrub-terms: one term per line, case-insensitive. Blank lines and lines starting with # are skipped.
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -f .scrub-terms ]; then
  echo "No .scrub-terms file. Create one (one term per line) first." >&2
  exit 2
fi
terms="$(mktemp)"; trap 'rm -f "$terms"' EXIT
grep -v -e '^[[:space:]]*$' -e '^#' .scrub-terms > "$terms" || true
if [ ! -s "$terms" ]; then
  echo ".scrub-terms has no usable terms." >&2
  exit 2
fi
awk 'length($0) < 4 { print "Warning: very short term may cause false hits: " $0 }' "$terms" >&2
if git ls-files -z --cached --others --exclude-standard | xargs -0 grep -nIiF -f "$terms" --; then
  echo "SCRUB CHECK FAILED: terms found above." >&2
  exit 1
fi
echo "Scrub check passed."
