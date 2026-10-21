#!/usr/bin/env bash
# Scans files git would commit for things that look like API keys, tokens, or private keys.
set -uo pipefail
cd "$(dirname "$0")/.."
if git ls-files -z --cached --others --exclude-standard \
   | grep -zvE '^scripts/(secret_patterns\.txt|secret_check\.sh)$' \
   | xargs -0 grep -nEIi -f scripts/secret_patterns.txt --; then
  echo "SECRET CHECK FAILED: possible keys found above." >&2
  exit 1
fi
echo "Secret check passed."
