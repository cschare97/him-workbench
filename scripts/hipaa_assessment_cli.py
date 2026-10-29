"""HIPAA Security Rule self-assessment of this project, verified against the code (synthetic data only).

  python scripts/hipaa_assessment_cli.py                          # summary and biggest gaps
  python scripts/hipaa_assessment_cli.py --write docs/hipaa-self-assessment.md

A self-assessment, not an audit or legal advice. Exit code 1 if any row's checks no longer hold (RE-CHECK).
"""

import sys
from pathlib import Path

from him_workbench.hipaa_assessment import RECHECK, markdown_report, run_checks, text_report


def main() -> int:
    args = sys.argv[1:]
    controls = run_checks()
    if "--write" in args:
        target = Path(args[args.index("--write") + 1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown_report(controls), encoding="utf-8")
        print(f"Wrote {target}")
    print(text_report(controls))
    return 1 if any(c.status == RECHECK for c in controls) else 0


if __name__ == "__main__":
    sys.exit(main())
