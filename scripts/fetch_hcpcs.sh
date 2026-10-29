#!/usr/bin/env bash
# Downloads the public CMS HCPCS Level II alpha-numeric code file into data/hcpcs (gitignored).
#
# HCPCS Level II only (ambulance, DME, supplies, drugs given other than by mouth, etc.) — CMS-
# maintained and free to use. This deliberately does NOT fetch CPT / HCPCS Level I, which is
# AMA-licensed.
#
# CMS changes this URL with each quarterly release; check the current one at
# https://www.cms.gov/medicare/coding/hcpcsreleasecodesets/hcpcs-quarterly-update if this 404s.
set -euo pipefail
cd "$(dirname "$0")/.."
URL="${HCPCS_URL:-https://www.cms.gov/files/zip/october-2026-alpha-numeric-hcpcs-file.zip}"
mkdir -p data/hcpcs
curl -fL -o data/hcpcs/hcpcs-file.zip "$URL"
unzip -o data/hcpcs/hcpcs-file.zip -d data/hcpcs
echo
echo "Files:"
ls -la data/hcpcs
echo
echo "If hcpcs_cli.py or the web UI can't parse the extracted file, send me (Claude) a few header"
echo "lines from it — CMS has changed this file's internal column layout between releases before,"
echo "and hcpcs.py's column-detection may need a small update for this one."
