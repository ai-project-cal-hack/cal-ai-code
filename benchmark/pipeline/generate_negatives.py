"""Generate paired post-patch negative (non-vulnerable) benchmark tasks.

For each selected positive task in ``benchmark/data/tasks``, this builds a
negative counterpart pinned to the patched revision: the same repository at
the ``post_patch_commit`` recorded in the task's internal metadata, where the
vulnerability no longer exists. Negatives test an agent's false-positive rate
(can it correctly say "not vulnerable?").

Design choices (see docs/curation.md):
    * vulnerable=false, vuln_class=null, locations=[] — there is nothing to find.
    * Hints are area-only (L0/L1). L2/L3 are omitted because their descriptions
      describe a vulnerability that no longer exists in the patched code.
    * The negative reuses the positive's ghsa_id and existing metadata record,
      so no new metadata file is created (one metadata record per GHSA).

Selection is a deterministic round-robin across vulnerability classes so a
small pilot stays diverse.

Usage:

    uv run python -m pipeline.generate_negatives --count 10
    uv run python -m pipeline.generate_negatives --count 10 --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent
TASKS_DIR = BENCHMARK_ROOT / "data" / "tasks"
METADATA_DIR = BENCHMARK_ROOT / "internal" / "metadata"
DEFAULT_OUTPUT_DIR = BENCHMARK_ROOT / "data" / "negatives"

NEGATIVE_SUFFIX = "-patched"


def _load_json(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def _area_hint(task: dict[str, Any]) -> str | None:
    """Return the L1 'area' hint, falling back to the L3 area."""
    hints = task.get("hints") or {}
    for level in ("L1", "L3"):
        node = hints.get(level) or {}
        area = node.get("area")
        if area:
            return area
    return None


def build_negative(
    task: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    """Construct the negative task record from a positive + its metadata."""
    cb = task["codebase"]
    gt = task["ground_truth"]
    ghsa_id = task["ghsa_id"]
    post_patch = metadata["post_patch_commit"]
    vuln_class = gt.get("vuln_class")

    area = _area_hint(task)
    hints: dict[str, Any] = {"L0": None}
    if area:
        hints["L1"] = {"area": area}

    reason = (
        f"Patched revision of {ghsa_id}: the {vuln_class} vulnerability was "
        f"remediated in commit {post_patch}. The previously vulnerable code "
        f"paths now apply proper mitigations, so no exploitable flaw remains."
    )

    return {
        "task_id": f"{task['task_id']}{NEGATIVE_SUFFIX}",
        "ghsa_id": ghsa_id,
        "codebase": {
            "repo": cb["repo"],
            "language": cb["language"],
            "ecosystem": cb["ecosystem"],
            "commit": post_patch,
        },
        "hints": hints,
        "ground_truth": {
            "vulnerable": False,
            "vuln_class": None,
            "cvss": None,
            "reason": reason,
            "locations": [],
        },
    }


def select_tasks(
    tasks: list[dict[str, Any]], count: int
) -> list[dict[str, Any]]:
    """Deterministic round-robin across vuln classes for a diverse sample."""
    by_class: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        by_class[task["ground_truth"].get("vuln_class") or "unknown"].append(task)

    for bucket in by_class.values():
        bucket.sort(key=lambda t: t["task_id"])

    selected: list[dict[str, Any]] = []
    classes = sorted(by_class)
    index = 0
    while len(selected) < count and any(by_class[c] for c in classes):
        bucket = by_class[classes[index % len(classes)]]
        if bucket:
            selected.append(bucket.pop(0))
        index += 1
    return selected[:count]


def run(*, count: int, output_dir: Path, dry_run: bool) -> int:
    if not TASKS_DIR.is_dir():
        print(f"error: tasks dir not found: {TASKS_DIR}", file=sys.stderr)
        return 1

    tasks = [_load_json(p) for p in sorted(TASKS_DIR.glob("*.json"))]
    metadata_by_ghsa = {
        m["ghsa_id"]: m
        for m in (_load_json(p) for p in METADATA_DIR.glob("*.json"))
    }

    eligible = [t for t in tasks if t["ghsa_id"] in metadata_by_ghsa]
    skipped = len(tasks) - len(eligible)
    if skipped:
        print(f"note: {skipped} task(s) skipped (no metadata / post_patch_commit)")

    chosen = select_tasks(eligible, count)
    print(f"{'[dry-run] ' if dry_run else ''}Generating {len(chosen)} negative(s)\n")

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for task in chosen:
        metadata = metadata_by_ghsa[task["ghsa_id"]]
        negative = build_negative(task, metadata)
        vuln_class = task["ground_truth"].get("vuln_class")
        out_path = output_dir / f"{negative['task_id']}.json"
        print(f"  {negative['task_id']:48s} <- {vuln_class} @ {negative['codebase']['commit'][:10]}")
        if not dry_run:
            with out_path.open("w") as f:
                json.dump(negative, f, indent=2)
                f.write("\n")
            written += 1

    if dry_run:
        print(f"\n[dry-run] Would write {len(chosen)} file(s) to {output_dir}")
    else:
        print(f"\nWrote {written} negative task(s) to {output_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate paired post-patch negative benchmark tasks."
    )
    parser.add_argument(
        "--count", type=int, default=10,
        help="Number of negatives to generate (default: 10).",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be generated without writing files.",
    )
    args = parser.parse_args(argv)

    return run(count=args.count, output_dir=args.output_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
