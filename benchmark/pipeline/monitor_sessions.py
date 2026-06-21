"""Monitor dispatched Devin curation sessions and report progress.

Lists all sessions tagged with 'curation', rolls them up into high-level
buckets (working, blocked, finished, suspended, failed), and prints a
progress summary. Useful for watching a dispatch batch make its way to
completion before running ``cleanup_sessions.py``.

Usage:

    uv run python -m pipeline.monitor_sessions
    uv run python -m pipeline.monitor_sessions --tag XSS        # filter by extra tag
    uv run python -m pipeline.monitor_sessions --detail         # list individual sessions
    uv run python -m pipeline.monitor_sessions --json           # machine-readable output
    uv run python -m pipeline.monitor_sessions --watch 60       # refresh every 60s
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .lib.env import require_env

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DEVIN_API_KEY = require_env("DEVIN_API_KEY")
DEVIN_ORG_ID = require_env("DEVIN_ORG_ID")

BASE_URL = f"https://api.devin.ai/v3/organizations/{DEVIN_ORG_ID}/sessions"

MAX_RETRIES = 3
RETRY_BACKOFF = 5.0
TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)

# High-level buckets keyed in display order. Each entry maps to a predicate
# over (status, status_detail). The first matching bucket wins.
BLOCKED_DETAILS = {"waiting_for_user", "blocked", "waiting_for_confirmation"}
FAILED_DETAILS = {"error", "failed", "expired"}


def _client() -> httpx.Client:
    return httpx.Client(
        headers={
            "Authorization": f"Bearer {DEVIN_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=TIMEOUT,
    )


def _request_with_retry(
    method: str,
    client: httpx.Client,
    url: str,
    *,
    params: dict | None = None,
) -> httpx.Response:
    """Fire an HTTP request with retries on transient errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return client.request(method, url, params=params)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
            if attempt == MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF * attempt
            print(f"    retry {attempt}/{MAX_RETRIES} after {type(exc).__name__}, waiting {wait:.0f}s...")
            time.sleep(wait)
    raise RuntimeError("unreachable")


def list_curation_sessions(client: httpx.Client) -> list[dict]:
    """Paginate through all curation-tagged sessions (v3 cursor pagination)."""
    sessions: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict = {"tags": "curation", "first": 200}
        if cursor:
            params["after"] = cursor
        resp = _request_with_retry("GET", client, BASE_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        sessions.extend(items)
        if not data.get("has_next_page") or not items:
            break
        cursor = data.get("end_cursor")
        if not cursor:
            break
    return sessions


def classify(session: dict) -> str:
    """Roll a session up into a high-level bucket."""
    status = (session.get("status") or "").lower()
    detail = (session.get("status_detail") or "").lower()

    if detail == "finished":
        return "finished"
    if detail in FAILED_DETAILS:
        return "failed"
    if detail in BLOCKED_DETAILS:
        return "blocked"
    if status == "suspended":
        return "suspended"
    if status == "running":
        return "working"
    return "other"


def _matches_tags(session: dict, required: list[str]) -> bool:
    if not required:
        return True
    tags = {str(t).lower() for t in (session.get("tags") or [])}
    return all(t.lower() in tags for t in required)


BUCKET_ORDER = ["working", "blocked", "suspended", "finished", "failed", "other"]


def summarize(sessions: list[dict]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {name: [] for name in BUCKET_ORDER}
    for s in sessions:
        buckets[classify(s)].append(s)
    return buckets


def _print_human(buckets: dict[str, list[dict]], *, detail: bool) -> None:
    total = sum(len(v) for v in buckets.values())
    if total == 0:
        print("No curation sessions found.")
        return

    finished = len(buckets["finished"])
    done_pct = 100.0 * finished / total

    print(f"Curation progress: {finished}/{total} finished ({done_pct:.0f}%)\n")
    print("By bucket:")
    for name in BUCKET_ORDER:
        items = buckets[name]
        if items:
            print(f"  {name:12s} {len(items):>4d}")

    if detail:
        for name in BUCKET_ORDER:
            items = buckets[name]
            if not items:
                continue
            print(f"\n{name.upper()} ({len(items)}):")
            for s in items:
                sid = s.get("session_id", "?")
                title = s.get("title", "?")
                url = s.get("url", "")
                print(f"  {sid}  {title}")
                if url:
                    print(f"      {url}")


def _print_json(buckets: dict[str, list[dict]]) -> None:
    total = sum(len(v) for v in buckets.values())
    payload = {
        "total": total,
        "counts": {name: len(items) for name, items in buckets.items()},
        "sessions": {
            name: [
                {
                    "session_id": s.get("session_id"),
                    "title": s.get("title"),
                    "status": s.get("status"),
                    "status_detail": s.get("status_detail"),
                    "url": s.get("url"),
                }
                for s in items
            ]
            for name, items in buckets.items()
        },
    }
    print(json.dumps(payload, indent=2))


def run(*, tags: list[str], detail: bool, as_json: bool) -> int:
    with _client() as client:
        sessions = list_curation_sessions(client)

    if tags:
        sessions = [s for s in sessions if _matches_tags(s, tags)]

    buckets = summarize(sessions)

    if as_json:
        _print_json(buckets)
    else:
        _print_human(buckets, detail=detail)

    return 0


def watch(*, interval: int, tags: list[str], detail: bool) -> int:
    print(f"Watching curation sessions every {interval}s (Ctrl-C to stop)\n")
    while True:
        try:
            run(tags=tags, detail=detail, as_json=False)
            print(f"\nRefreshing in {interval}s...\n{'-' * 50}\n")
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Monitor Devin curation sessions and report progress."
    )
    parser.add_argument(
        "--tag", type=str, action="append", default=None, metavar="TAG",
        help="Only include sessions carrying this tag (AND-combined, repeatable). "
        "E.g. a vuln class or GHSA ID added at dispatch time.",
    )
    parser.add_argument(
        "--detail", action="store_true",
        help="List individual sessions (id, title, url) per bucket.",
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="Emit machine-readable JSON instead of a human summary.",
    )
    parser.add_argument(
        "--watch", type=int, nargs="?", const=60, default=None, metavar="SECS",
        help="Refresh in a loop every N seconds (default: 60).",
    )
    args = parser.parse_args(argv)

    tags = args.tag or []

    if args.watch is not None:
        if args.as_json:
            print("error: --watch cannot be combined with --json", file=sys.stderr)
            return 1
        return watch(interval=args.watch, tags=tags, detail=args.detail)

    return run(tags=tags, detail=args.detail, as_json=args.as_json)


if __name__ == "__main__":
    raise SystemExit(main())
