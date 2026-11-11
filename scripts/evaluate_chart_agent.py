"""Accuracy test for the "Patient chart" assistant on Synthea patients.

For each sampled patient, every question runs on a fresh assistant (clean state, like a SimEHR episode).
The expected answer is worked out from the raw FHIR bundle, not from the workbench's Chart object, so the test
checks the model's reading of the tool result and does not just agree with the tool.

Questions:
  weight    most recent body weight (value must appear in the answer)
  visit     date of the most recent visit (any common date format)
  problems  number of active problems (digit or word)
  meds      active medications now (a named active medication, or a clear "none" when there are none)
  a1c       "is the latest A1c normal?" The record has no reference ranges, so the answer must NOT judge it.

Usage:
  python scripts/evaluate_chart_agent.py data/synthea/fhir --model NAME [--n 10] [--seed 1] [--show]
  python scripts/evaluate_chart_agent.py data/synthea/fhir --dry-run     (prints expected answers, no model)

Needs Ollama running for a real run. Prints pass/fail counts. --show also prints each answer for review.
The a1c check is a phrase match, so read the failures before trusting it. Synthetic data only.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import date
from pathlib import Path

from him_workbench.assistant import Assistant, judges_against_a_range, list_models, ollama_chat
from him_workbench.chart import (PROBLEM_TAGS, _coded, _medication_name, _ref_key, chart_files_from, clean,
                                 semantic_tag)
from him_workbench.fhir import load_fhir_patients

GUARD_NOTE = "\n\n[!] This answer judges a value against a normal range."
_WORDS = ["zero", "no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
_JUDGE = re.compile(r"\b(is|was|are|were|within|in the|looks?|appears?|seems?)\s+(the\s+)?(a\s+)?"
                    r"(normal|abnormal|high|low|elevated|healthy|good|fine|concerning)\b", re.I)


def facts(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    res = [e["resource"] for e in data.get("entry") or [] if isinstance(e.get("resource"), dict)]
    index = {str(r["id"]): r for r in res if r.get("id")}
    out: dict = {}
    starts = [str((r.get("period") or {}).get("start") or "")[:10] for r in res if r.get("resourceType") == "Encounter"]
    starts = [s for s in starts if s]
    out["visit"] = max(starts) if starts else None
    out["problems"] = sum(
        1 for r in res if r.get("resourceType") == "Condition"
        and _coded(r.get("clinicalStatus")).code == "active"
        and semantic_tag(_coded(r.get("code")).display) in PROBLEM_TAGS)
    out["meds"] = sorted({clean(_medication_name(r, index).display).split()[0].lower() for r in res
                          if r.get("resourceType") == "MedicationRequest" and r.get("status") == "active"})
    weights = sorted(((str(r.get("effectiveDateTime") or ""), r["valueQuantity"]["value"]) for r in res
                      if r.get("resourceType") == "Observation"
                      and any(c.get("code") == "29463-7" for c in (r.get("code") or {}).get("coding") or [])
                      and isinstance(r.get("valueQuantity"), dict) and r["valueQuantity"].get("value") is not None))
    out["weight"] = f"{round(weights[-1][1], 2):g}" if weights else None
    a1cs = sorted(((str(r.get("effectiveDateTime") or ""), r["valueQuantity"]["value"]) for r in res
                   if r.get("resourceType") == "Observation"
                   and any(c.get("code") == "4548-4" for c in (r.get("code") or {}).get("coding") or [])
                   and isinstance(r.get("valueQuantity"), dict) and r["valueQuantity"].get("value") is not None))
    out["a1c"] = bool(a1cs)
    out["a1c_value"] = f"{round(a1cs[-1][1], 2):g}" if a1cs else None
    return out


def date_forms(iso: str) -> list[str]:
    d = date.fromisoformat(iso)
    return [iso, d.strftime("%B ") + f"{d.day}, {d.year}", d.strftime("%b ") + f"{d.day}, {d.year}",
            f"{d.month}/{d.day}/{d.year}", f"{d.month:02d}/{d.day:02d}/{d.year}",
            f"{d.day} " + d.strftime("%B %Y"), d.strftime("%B ") + f"{d.day} {d.year}"]


def number_in(text: str, n: int) -> bool:
    t = text.lower()
    if re.search(rf"(?<![\d.]){n}(?![\d.])", t):
        return True
    return n < len(_WORDS) and re.search(rf"\b{_WORDS[n]}\b", t) is not None


_DISCLAIM = re.compile(r"(no|not|cannot|can't|unable|without|doesn't|does not)[^.]{0,60}"
                       r"(reference|range|interpret|determine|judg|normal)", re.I)
_NO_A1C = re.compile(r"(no|not|n't|unable|couldn't|cannot)[^.]{0,80}(a1c|hba1c|glycated|glycosylated)", re.I)


def a1c_check(has_a1c: bool, value: str | None = None):
    """With an A1c on file: pass unless the answer judges it normal/abnormal (a disclaimer such as "cannot be
    determined if it is in the normal range" is fine). With no A1c on file: pass only if the answer says there is
    none, so a different test (plain hemoglobin) quietly standing in for it fails."""
    if has_a1c:
        return lambda a: (value is None or value in a) and not judges_against_a_range(a)
    return lambda a: _NO_A1C.search(a) is not None


def expect_text(key: str, f: dict) -> str:
    if key == "a1c":
        return f"the value {f['a1c_value']} and no normal/abnormal judgement" if f["a1c"] else "an answer that says there is no A1c on record"
    return str(f.get(key))


def questions(f: dict) -> list[tuple[str, str, object]]:
    qs = []
    if f["weight"]:
        qs.append(("weight", "What is this patient's most recent body weight?",
                   lambda a: f["weight"] in a))
    if f["visit"]:
        qs.append(("visit", "When was this patient's most recent visit?",
                   lambda a: any(s in a for s in date_forms(f["visit"]))))
    qs.append(("problems", "How many active problems does this patient have on the problem list?",
               lambda a: number_in(a, f["problems"])))
    if f["meds"]:
        qs.append(("meds", "What medications is this patient currently taking?",
                   lambda a: any(m in a.lower() for m in f["meds"])))
    else:
        qs.append(("meds", "What medications is this patient currently taking?",
                   lambda a: re.search(r"\b(no|none|zero|not on|not taking|not currently|currently not|isn't|is not|does not|doesn't)\b", a, re.I) is not None))
    qs.append(("a1c", "Is this patient's most recent hemoglobin A1c in the normal range?", a1c_check(f["a1c"], f["a1c_value"])))
    return qs


def pick_patients(pool, files, n, seed, balanced_a1c):
    """A random sample. With balanced_a1c, half the patients have an A1c on file and half do not, so both
    behaviours get tested (a plain random sample can contain almost none with an A1c)."""
    rng = random.Random(seed)
    if not balanced_a1c:
        return rng.sample(pool, min(n, len(pool)))
    order = pool[:]
    rng.shuffle(order)
    want = {True: (n + 1) // 2, False: n // 2}
    got: dict[bool, list] = {True: [], False: []}
    for p in order:
        has = facts(files[p.id])["a1c"]
        if len(got[has]) < want[has]:
            got[has].append(p)
        if all(len(got[k]) >= want[k] for k in want):
            break
    return got[True] + got[False]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("fhir", type=Path, help="folder of Synthea FHIR bundles")
    ap.add_argument("--model")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--only", help="comma-separated questions to run, e.g. a1c or weight,visit. With only a1c the "
                                   "sample is half patients with an A1c and half without.")
    ap.add_argument("--timeout", type=int, default=300, help="seconds to wait for one answer (default 300)")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.dry_run and not args.model:
        print("Pick a model with --model. Your Ollama models:", ", ".join(list_models()) or "(none found)")
        return 1
    only = {k.strip() for k in args.only.split(",")} if args.only else None
    patients, _ = load_fhir_patients(args.fhir)
    files = chart_files_from(args.fhir)
    pool = sorted((p for p in patients if p.id in files), key=lambda p: p.id)
    sample = pick_patients(pool, files, args.n, args.seed, only == {"a1c"})
    chat = None if args.dry_run else ollama_chat(args.model, timeout=args.timeout)
    tally: dict[str, list[int]] = {}
    guard_flags: dict[str, int] = {}
    errors: list[str] = []

    def report() -> None:
        print("\nresults (pass / fail):")
        for key, (a, b) in tally.items():
            print(f"  {key:<9}{a:>3} / {b}")
        print(f"  total    {sum(a for a, _ in tally.values()):>3} / {sum(b for _, b in tally.values())}")
        if errors:
            print(f"\n{len(errors)} question(s) got no answer (timeout or Ollama error), not counted above: "
                  + ", ".join(errors))
        print(f"\nThe app's reference-range check added its [!] note to {sum(guard_flags.values())} answer(s). "
              "Those are graded on the model's own words, so a flagged violation still counts as a fail; the note "
              "only warns the user.")
        print("Answers vary from run to run, so treat small counts as a rough picture, not a score.")

    try:
        for p in sample:
            f = facts(files[p.id])
            for key, q, check in questions(f):
                if only and key not in only:
                    continue
                if args.dry_run:
                    print(f"{p.id[:8]} {key:<8} expect {expect_text(key, f)} (A1c on file: {f['a1c']})")
                    continue
                print(f"  asking {p.id[:8]} {key} ...", end="", flush=True)
                t0 = time.time()
                bot = Assistant(chat, patients=patients, charts=files, profile="chart")
                try:
                    full = bot.ask(q, patient_id=p.id)
                except (OSError, ValueError) as e:      # timeout, connection reset, Ollama HTTP error
                    print(f" no answer after {time.time() - t0:.0f}s ({type(e).__name__})", flush=True)
                    errors.append(f"{p.id[:8]} {key}")
                    continue
                # Grade what the model wrote. The workbench appends a "[!] This answer judges..." note after it;
                # that note's own words must not count as the model's disclaimer, so it is cut off before grading.
                answer = full.split(GUARD_NOTE)[0].rstrip()
                flagged = GUARD_NOTE in full
                guard_flags[key] = guard_flags.get(key, 0) + flagged
                ok = bool(check(answer))
                print(f" {time.time() - t0:.0f}s", flush=True)
                tally.setdefault(key, [0, 0])[0 if ok else 1] += 1
                if args.show or not ok:
                    print(f"[{'pass' if ok else 'FAIL'}{' +flagged by app' if flagged else ''}] {p.id[:8]} {key}: "
                          f"{answer[:300]!r}" + ("" if ok else f"   expected: {expect_text(key, f)}"))
    except KeyboardInterrupt:
        print("\nStopped. Results so far:")
    if not args.dry_run:
        report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
