#!/usr/bin/env bash
# Generates synthetic patients with Synthea into data/synthea (gitignored).
# Usage: ./scripts/generate_synthea.sh [population] [seed]
# FHIR_EXPORT=true ./scripts/generate_synthea.sh   also writes FHIR bundles to data/synthea/fhir/
set -euo pipefail
cd "$(dirname "$0")/.."
JAR="${SYNTHEA_JAR:-$HOME/Documents/Synthea/synthea-with-dependencies.jar}"
POP="${1:-500}"
SEED="${2:-42}"
if [ ! -f "$JAR" ]; then
  echo "Synthea jar not found at $JAR (set SYNTHEA_JAR)." >&2
  exit 1
fi
mkdir -p data/synthea
java -jar "$JAR" -p "$POP" -s "$SEED" \
  --exporter.baseDirectory="$(pwd)/data/synthea" \
  --exporter.csv.export=true \
  --exporter.fhir.export="${FHIR_EXPORT:-false}" \
  --generate.append_numbers_to_person_names=false \
  California
echo "Done. Patients file: data/synthea/csv/patients.csv"
