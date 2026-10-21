#!/usr/bin/env bash
# Scans the repo for things that look like API keys, tokens, or private keys.
set -uo pipefail
cd "$(dirname "$0")/.."
if grep -rnEIi -f scripts/secret_patterns.txt \
    --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ \
    --exclude=secret_patterns.txt --exclude=secret_check.sh --exclude=.scrub-terms .; then
  echo "SECRET CHECK FAILED: possible keys found above." >&2
  exit 1
fi
echo "Secret check passed."
