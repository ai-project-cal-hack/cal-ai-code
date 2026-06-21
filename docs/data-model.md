# Data Model

Codebreaker spreads its state across four stores. Understanding which data lives
where — and why — is the key to reading the control plane.

## Storage Layers

| Layer | Holds | Why here |
| --- | --- | --- |
| **Cloudflare D1** (SQLite) — binding `DB`, database `codebreaker-sessions` | Queryable metadata + timelines: sessions, benchmark runs/results, CVE follow-ups, audits | Relational queries at the edge; cheap, durable, survives worker restarts |
| **Durable Object SQLite** — `SessionAgent`, `AuditCoordinatorAgent`, `AuditInvestigatorAgent`, `AuditValidatorAgent` | Each agent's live, mutable state (conversation, artifact state) via the `agents` framework `setState(...)` | Strong per-object consistency for long-running, single-threaded agent work |
| **Git / GitHub** — `GIT_TREE_PROVIDER=github` (`GitHubGitTreeStore`) | Canonical code artifacts: per-run forks, repro/fix branches, PRs | Git is the natural store for code, and PRs drop into real workflows |
| **Repo filesystem** | Benchmark tasks (`benchmark/data/tasks/*.json`) and dataset-pipeline outputs (`benchmark/pipeline/output/*.jsonl`) | Static inputs; versioned in the repo, not runtime state |

D1 only stores **pointers** to the other layers (repo remotes, branch names,
commit SHAs, PR URLs, evidence paths). No single transaction spans the layers;
consistency is reconciled (see [Consistency model](#consistency-model)).

## D1 Schema

Tables are defined with Drizzle in `packages/control-plane/src/db/d1-schema.ts`
and applied via raw SQL migrations in `packages/control-plane/migrations/`
(`0000`–`0007`). `drizzle.config.ts` generates migrations (`driver: d1-http`).

Every domain follows the same convention:

> **parent row** + **`*_events` append-only timeline** + (where relevant) **child rows**.

### Sessions (agent runs)

- `sessions` — one row per agent session: `status`, model provider/id, token
  counts, turn count, repo + artifact metadata (`run_repo_remote`,
  `artifact_working_branch`, `artifact_latest_commit_sha`, evidence paths).
  Indexed by `(status, created_at)`, `benchmark_id`, `run_repo_name`.
- `processed_events` — idempotency ledger; unique on
  `(session_id, kind, event_id)` so streamed agent events are processed once.

### Benchmark runs

- `benchmark_runs` — one row per run: `task_id`, `difficulty`, model, `status`,
  `score`, `session_id`, `cleanup_policy`, timestamps.
- `benchmark_run_events` — run timeline (`created`, `checkout_started`,
  `agent_completed`, `result_parsed`, `failed`, …).
- `benchmark_run_results` — scored result: `agent_output`, `raw_output`,
  `score`, plus **denormalized queryable columns** (predicted/expected
  vulnerable + class, location scores) extracted from the agent output.

### CVE follow-ups (the Devin loop)

- `cve_followups` — one per follow-up: `run_id`, `ghsa_id`, `status`,
  `auto_fired`, `deepwiki_context`.
- `cve_followup_stages` — the pipeline stages `repro`, `fix`, `review_repro`,
  `review_fix`: `kind`, `status`, `devin_session_id`, `devin_url`, `branch`,
  `pr_url`, `attempts`, `validation_result_id`.
- `cve_followup_validations` — validation records: `exit_code`, `marker_seen`,
  `observational_fingerprint_matched`, `passed`, `tier`, stdout/stderr excerpts.
- `cve_followup_events` — per-follow-up timeline.

### Audits (the discovery pipeline)

- `audits` — audit run: `repo_url`, `ref`, model, candidate/validated counts,
  `coordinator_session_id`, `status`.
- `audit_shards` — work units; unique on `(audit_id, kind)`, each with an
  `investigator_session_id`.
- `audit_findings` — candidate vulns: `confidence`, `severity`, `cwe`,
  `vuln_class`, `locations_json`, `poc_sketch`, `references_json`,
  `validation_notes`, `validator_session_id`.
- `audit_events` — per-audit timeline.

## Conventions

- **Timestamps** are ISO-8601 **text** (lexicographically sortable, readable).
- **Booleans** are stored as **integers** (`0`/`1`) — SQLite has no boolean type
  (e.g. `auto_fired`, `passed`, `marker_seen`).
- **Semi-structured data** is stored as **JSON-in-text** (`agent_output`,
  `raw_output`, `locations_json`, `references_json`, `manifest_json`,
  `details`). These columns are **not** queryable with SQL `WHERE`; fields you
  need to filter/aggregate on are denormalized into real columns instead (see
  `benchmark_run_results`).
- **`d1://…` artifact paths** (e.g. `d1://benchmark-runs/<run>/results/<id>`)
  are a **logical identifier**, not a blob URL — the content lives in the
  corresponding D1 row's text columns.
- **`status` / `kind` columns are plain `text`.** Valid values are enforced at
  the application layer by Zod schemas
  (`packages/benchmark-runner/src/schemas.ts`), not by DB constraints.

## Consistency Model

Because state spans D1, Durable Objects, and GitHub with no cross-store
transaction, the system converges through **reconciliation + idempotency**
rather than ACID guarantees:

- A cron trigger (`*/2 * * * *`) drives reconcile loops that rebuild progress
  from durable D1 rows and advance work (e.g. CVE follow-up stages). In local
  dev, scheduled workers do not auto-fire — trigger manually with
  `curl http://localhost:8787/cdn-cgi/handler/scheduled`.
- `processed_events` and claim-based single-flight on stages
  (`claimStageForDispatch`) make event processing and Devin dispatch safe under
  retries and concurrent reconcilers.
- The append-only `*_events` tables provide recovery and observability.
