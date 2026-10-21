#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
cat > .git/hooks/pre-commit <<'HOOK'
#!/usr/bin/env bash
cd "$(git rev-parse --show-toplevel)"
./scripts/scrub_check.sh && ./scripts/secret_check.sh
HOOK
chmod +x .git/hooks/pre-commit
echo "Pre-commit hook installed."
