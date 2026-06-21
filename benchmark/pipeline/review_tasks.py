"""Browse and review benchmark tasks from the CLI.

A convenience wrapper for systematically going through the dataset: filter by
vulnerability class, language, ecosystem, or a free-text search, then list them
one-per-line, show full detail (including hints), or print summary counts.

Reads the source task JSON files in ``benchmark/data/tasks`` so the full
ground truth and progressive hints are available.

Usage:

    # one-line index of everything
    uv run python -m pipeline.review_tasks | less

    # walk a single class with full detail (hints, locations, reason)
    uv run python -m pipeline.review_tasks --class sql-injection --detail | less

    # filter combinations
    uv run python -m pipeline.review_tasks --language rust --class buffer-overflow
    uv run python -m pipeline.review_tasks --search jwt --detail

    # distribution counts
    uv run python -m pipeline.review_tasks --counts

    # JSON output for piping into jq
    uv run python -m pipeline.review_tasks --class xss --json | jq '.[].repo'
    uv run python -m pipeline.review_tasks --counts --json

    # include the post-patch negatives too
    uv run python -m pipeline.review_tasks --include-negatives --counts
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = BENCHMARK_ROOT / "data" / "tasks"
NEGATIVES_DIR = BENCHMARK_ROOT / "data" / "negatives"


def load_tasks(*, include_negatives: bool) -> list[dict[str, Any]]:
    dirs = [TASKS_DIR]
    if include_negatives and NEGATIVES_DIR.is_dir():
        dirs.append(NEGATIVES_DIR)

    tasks: list[dict[str, Any]] = []
    for directory in dirs:
        for path in sorted(directory.glob("*.json")):
            tasks.append(json.load(path.open()))
    return tasks


def matches(task: dict[str, Any], args: argparse.Namespace) -> bool:
    cb = task.get("codebase", {})
    gt = task.get("ground_truth", {})

    if args.vuln_class and gt.get("vuln_class") not in args.vuln_class:
        return False
    if args.language and cb.get("language") not in args.language:
        return False
    if args.ecosystem and cb.get("ecosystem") not in args.ecosystem:
        return False
    if args.search:
        needle = args.search.lower()
        haystack = " ".join(
            str(x)
            for x in (
                task.get("task_id"),
                task.get("ghsa_id"),
                cb.get("repo"),
                gt.get("reason"),
            )
        ).lower()
        if needle not in haystack:
            return False
    return True


def print_oneline(tasks: list[dict[str, Any]]) -> None:
    for t in tasks:
        cb = t.get("codebase", {})
        gt = t.get("ground_truth", {})
        cls = gt.get("vuln_class") or ("not-vulnerable" if gt.get("vulnerable") is False else "?")
        print(f"{t['task_id']:48} {str(cls):24} {str(cb.get('language')):11} {cb.get('repo')}")


def print_detail(tasks: list[dict[str, Any]]) -> None:
    for t in tasks:
        cb = t.get("codebase", {})
        gt = t.get("ground_truth", {})
        hints = t.get("hints", {})
        print(f"== {t['task_id']}  ({t.get('ghsa_id')}) ==")
        print(f"   class:   {gt.get('vuln_class')}   vulnerable={gt.get('vulnerable')}   cvss={gt.get('cvss')}")
        print(f"   repo:    {cb.get('repo')}  [{cb.get('language')}/{cb.get('ecosystem')}]")
        print(f"   commit:  {cb.get('commit')}")
        locs = gt.get("locations") or []
        if locs:
            print("   locations:")
            for loc in locs:
                fn = f" -> {loc['function']}" if loc.get("function") else ""
                print(f"     {loc.get('file')}{fn}")
        else:
            print("   locations: (none)")
        print(f"   reason:  {gt.get('reason')}")
        for level in ("L1", "L2", "L3"):
            node = hints.get(level)
            if not node:
                continue
            parts = [v for v in (node.get("area"), node.get("description")) if v]
            print(f"   {level}: {' | '.join(parts)}")
        print()


COUNT_FIELDS = (
    ("vuln_class", lambda t: t["ground_truth"].get("vuln_class")),
    ("language", lambda t: t["codebase"].get("language")),
    ("ecosystem", lambda t: t["codebase"].get("ecosystem")),
)


def compute_counts(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"total": len(tasks)}
    for field, getter in COUNT_FIELDS:
        counts = Counter(getter(t) for t in tasks)
        result[field] = dict(counts.most_common())
    return result


def print_counts(tasks: list[dict[str, Any]]) -> None:
    summary = compute_counts(tasks)
    print(f"TOTAL: {summary['total']}\n")
    for field, _ in COUNT_FIELDS:
        print(f"By {field}:")
        for key, count in summary[field].items():
            print(f"  {str(key):26} {count:>4}")
        print()


def run(args: argparse.Namespace) -> int:
    tasks = load_tasks(include_negatives=args.include_negatives)
    filtered = [t for t in tasks if matches(t, args)]

    if args.limit is not None:
        filtered = filtered[: args.limit]

    if args.json:
        payload = compute_counts(filtered) if args.counts else filtered
        print(json.dumps(payload, indent=2))
    elif args.counts:
        print_counts(filtered)
    elif args.detail:
        print_detail(filtered)
    else:
        print_oneline(filtered)
        print(f"\n{len(filtered)} of {len(tasks)} task(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Browse and review benchmark tasks from the CLI."
    )
    parser.add_argument(
        "--class", dest="vuln_class", action="append", metavar="CLASS",
        help="Filter by vulnerability class (repeatable).",
    )
    parser.add_argument(
        "--language", action="append", metavar="LANG",
        help="Filter by language (repeatable).",
    )
    parser.add_argument(
        "--ecosystem", action="append", metavar="ECO",
        help="Filter by ecosystem (repeatable).",
    )
    parser.add_argument(
        "--search", metavar="TEXT",
        help="Case-insensitive substring match over task_id, ghsa, repo, reason.",
    )
    parser.add_argument(
        "--detail", action="store_true",
        help="Show full detail (locations, reason, hints) instead of one line each.",
    )
    parser.add_argument(
        "--counts", action="store_true",
        help="Show distribution counts for the (filtered) set instead of a listing.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON (the filtered task records, or counts with --counts) for piping into jq etc.",
    )
    parser.add_argument(
        "--include-negatives", action="store_true",
        help="Also include tasks from data/negatives.",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Only show the first N matches.",
    )
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
