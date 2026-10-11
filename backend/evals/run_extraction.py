# Fact-extraction eval: runs memory_writer.extract_facts over hand-written exchanges and scores the
# output against the expected facts, so a prompt or model change gives a number, not a guess.
#
# Run from backend/:
#   ../.venv/bin/python evals/run_extraction.py                      # current prompt and MEMORY_MODEL
#   ../.venv/bin/python evals/run_extraction.py --model openai/gpt-oss-120b
#   ../.venv/bin/python evals/run_extraction.py --prompt my_prompt.txt --label "stricter rules"
#   ../.venv/bin/python evals/run_extraction.py --fake               # check the scorer, no API calls
#
# Matching: an extracted fact matches an expected one when every keyword group appears in it as a
# whole word (case-insensitive; "a|b" means either). Each extracted fact matches at most one
# expected fact. Optional facts don't count as misses when absent or as extras when present.
#   precision = matched / extracted (not counting optional matches)
#   recall    = matched / expected (required facts only)
# Every run is appended to evals/results.csv so runs can be compared over time.
import argparse
import asyncio
import csv
import hashlib
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import backend modules

import llm  # noqa: E402
import memory_writer  # noqa: E402
from groq import RateLimitError  # noqa: E402

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results.csv"
FIELDS = ["timestamp", "label", "model", "prompt", "cases", "precision", "recall", "f1",
          "matched", "extra", "missed", "tokens"]


def matches(text: str, expected: dict) -> bool:
    return all(re.search(rf"\b(?:{group})\b", text, re.IGNORECASE) for group in expected["keywords"])


def score(extracted: list[str], expected: list[dict]) -> tuple[list, list, list]:
    """Returns (matched pairs, extra extracted facts, missed required facts)."""
    remaining, matched, extra = list(expected), [], []
    for text in extracted:
        hit = next((e for e in remaining if matches(text, e)), None)
        if hit:
            remaining.remove(hit)
            matched.append((text, hit))
        else:
            extra.append(text)
    missed = [e for e in remaining if not e.get("optional")]
    return matched, extra, missed


async def extract(case: dict, fake: bool) -> list[str]:
    if fake:  # a perfect extractor, to check the scorer itself
        return [e["fact"] for e in case["expected"] if not e.get("optional")]
    for attempt in range(4):
        try:
            facts = await memory_writer.extract_facts(case["user"], case["assistant"])
            return [f.text for f in facts]
        except RateLimitError as e:
            wait = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(e))
            seconds = (int(wait.group(1) or 0) * 60 + float(wait.group(2))) if wait else 30
            if seconds > 120 or attempt == 3:
                sys.exit(f"\nRate limit won't clear soon ({e}). Try again later.")
            print(f"  (rate-limited, waiting {seconds:.0f}s)")
            await asyncio.sleep(seconds + 1)
    return []


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=memory_writer.MEMORY_MODEL, help="model to evaluate (default: MEMORY_MODEL)")
    parser.add_argument("--prompt", type=Path, help="file with an extraction prompt to try instead of EXTRACT_PROMPT")
    parser.add_argument("--cases", type=Path, default=HERE / "extraction_cases.json")
    parser.add_argument("--label", default="", help="note saved with this run, e.g. 'shorter prompt'")
    parser.add_argument("--delay", type=float, default=3.0, help="seconds between calls, to stay under 8K tokens/min")
    parser.add_argument("--fake", action="store_true", help="use a perfect fake extractor (no API calls)")
    parser.add_argument("--no-save", action="store_true", help="don't append this run to results.csv")
    args = parser.parse_args()

    logging.getLogger("assistant").setLevel(logging.WARNING)
    memory_writer.MEMORY_MODEL = args.model
    if args.prompt:
        memory_writer.EXTRACT_PROMPT = args.prompt.read_text()
    prompt_id = hashlib.sha1(memory_writer.EXTRACT_PROMPT.encode()).hexdigest()[:8]

    tokens = 0
    real_log_usage = llm.log_usage
    def counting_log_usage(purpose, model, usage):
        nonlocal tokens
        used = real_log_usage(purpose, model, usage)
        tokens += used
        return used
    llm.log_usage = counting_log_usage

    cases = json.loads(args.cases.read_text())
    print(f"model={'fake' if args.fake else args.model}  prompt={prompt_id}  cases={len(cases)}\n")
    total_matched = total_extra = total_missed = optional_matched = 0
    for i, case in enumerate(cases):
        if i and not args.fake:
            time.sleep(args.delay)
        extracted = await extract(case, args.fake)
        matched, extra, missed = score(extracted, case["expected"])
        opt = sum(1 for _, e in matched if e.get("optional"))
        total_matched += len(matched) - opt
        optional_matched += opt
        total_extra += len(extra)
        total_missed += len(missed)
        status = "ok  " if not extra and not missed else "FAIL"
        print(f"{status} {case['id']:<20} {len(matched)} matched, {len(extra)} extra, {len(missed)} missed")
        for text in extra:
            print(f"       extra:  {text}")
        for e in missed:
            print(f"       missed: {e['fact']}")

    extracted_count = total_matched + total_extra
    expected_count = total_matched + total_missed
    precision = total_matched / extracted_count if extracted_count else 1.0
    recall = total_matched / expected_count if expected_count else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    print(f"\nprecision {precision:.2f}  ({total_matched}/{extracted_count} extracted facts were expected)")
    print(f"recall    {recall:.2f}  ({total_matched}/{expected_count} expected facts were found)")
    print(f"f1        {f1:.2f}")
    if optional_matched:
        print(f"(+{optional_matched} optional facts found, not counted)")
    if not args.fake:
        print(f"tokens    {tokens}")

    if args.fake or args.no_save:
        return
    row = {"timestamp": datetime.now().isoformat(timespec="seconds"), "label": args.label, "model": args.model,
           "prompt": prompt_id, "cases": len(cases), "precision": f"{precision:.3f}", "recall": f"{recall:.3f}",
           "f1": f"{f1:.3f}", "matched": total_matched, "extra": total_extra, "missed": total_missed, "tokens": tokens}
    new_file = not RESULTS.exists()
    with RESULTS.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)
    with RESULTS.open() as f:
        history = list(csv.DictReader(f))
    print("\nrecent runs (evals/results.csv):")
    for r in history[-5:]:
        print(f"  {r['timestamp']}  P {r['precision']}  R {r['recall']}  F1 {r['f1']}  "
              f"{r['model']}  prompt {r['prompt']}  {r['label']}")


if __name__ == "__main__":
    asyncio.run(main())
