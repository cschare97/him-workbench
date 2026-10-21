#!/usr/bin/env bash
# Downloads the public ICD-10-CM code descriptions (CDC/NCHS, FY2027) into data/icd10cm (gitignored).
set -euo pipefail
cd "$(dirname "$0")/.."
URL="https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/2027/icd10cm-code-descriptions-2027.zip"
mkdir -p data/icd10cm
curl -fL -o data/icd10cm/code-descriptions.zip "$URL"
unzip -o data/icd10cm/code-descriptions.zip -d data/icd10cm
echo
echo "Files:"
ls -la data/icd10cm
