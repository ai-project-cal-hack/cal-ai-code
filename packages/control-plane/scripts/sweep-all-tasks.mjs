#!/usr/bin/env node
// Resumable driver that fans out every benchmark task through the pipeline:
// create run -> start run. Completed+vulnerable runs auto-fire CVE follow-ups,
// which the worker's reconcile loop then advances (Devin repro/fix -> PR).
//
// Designed to run unattended for a long sweep. It throttles to a fixed number
// of concurrently-running benchmark runs so the local worker + Modal are not
// overwhelmed, and it records dispatched task ids to a progress file so it can
// be stopped and restarted without re-dispatching.
//
// Env (all optional, read from packages/control-plane/.dev.vars if not set):
//   CODEBREAKER_API_URL   default http://127.0.0.1:8787
//   CODEBREAKER_TOKEN     bearer JWT (required)
//   SWEEP_CONCURRENCY     max simultaneously running benchmark runs (default 4)
//   SWEEP_DIFFICULTY      L0|L1|L2|L3 (default L1)
//   SWEEP_MODEL           provider/model-id (default anthropic/claude-sonnet-4-6)
//   SWEEP_PROGRESS        progress file path (default /tmp/codebreaker-sweep.json)

import { readFileSync, existsSync, writeFileSync, readdirSync } from "node:fs";
import { dirname, resolve, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const controlPlane = resolve(here, "..");
const repoRoot = resolve(controlPlane, "../..");
const NEWLINE_RE = /\r?\n/;

const parseDevVars = (raw) => {
  const env = {};
  for (const line of raw.split(NEWLINE_RE)) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const eq = t.indexOf("=");
    if (eq === -1) continue;
    let v = t.slice(eq + 1).trim();
    if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
      v = v.slice(1, -1);
    }
    env[t.slice(0, eq).trim()] = v;
  }
  return env;
};

const devVarsPath = join(controlPlane, ".dev.vars");
const fileEnv = existsSync(devVarsPath) ? parseDevVars(readFileSync(devVarsPath, "utf8")) : {};
const cfg = (k, d) => process.env[k] ?? fileEnv[k] ?? d;

const API = cfg("CODEBREAKER_API_URL", "http://127.0.0.1:8787");
const TOKEN = cfg("CODEBREAKER_TOKEN");
const CONCURRENCY = Number.parseInt(cfg("SWEEP_CONCURRENCY", "4"), 10);
const DIFFICULTY = cfg("SWEEP_DIFFICULTY", "L1");
const MODEL = cfg("SWEEP_MODEL", "anthropic/claude-sonnet-4-6");
const PROGRESS = cfg("SWEEP_PROGRESS", "/tmp/codebreaker-sweep.json");

if (!TOKEN) {
  console.error("CODEBREAKER_TOKEN is required (set it or add to .dev.vars)");
  process.exit(1);
}

const [provider, ...idParts] = MODEL.split("/");
const model = { provider, id: idParts.join("/") };
if (!(model.provider && model.id)) {
  console.error("SWEEP_MODEL must be provider/model-id");
  process.exit(1);
}

const taskIds = readdirSync(join(repoRoot, "benchmark/data/tasks"))
  .filter((f) => f.endsWith(".json"))
  .map((f) => f.slice(0, -".json".length))
  .sort();

const progress = existsSync(PROGRESS)
  ? JSON.parse(readFileSync(PROGRESS, "utf8"))
  : { dispatched: {} };
const saveProgress = () => writeFileSync(PROGRESS, JSON.stringify(progress, null, 2));

const api = async (method, path, body) => {
  const res = await fetch(`${API}${path}`, {
    method,
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const text = await res.text();
  let json;
  try { json = text ? JSON.parse(text) : undefined; } catch { json = undefined; }
  return { ok: res.ok, status: res.status, json, text };
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const TERMINAL = new Set(["completed", "failed", "scored", "errored", "cancelled"]);
const inFlight = new Map(); // runId -> taskId

const reapInFlight = async () => {
  for (const [runId, taskId] of [...inFlight.entries()]) {
    const r = await api("GET", `/benchmark-runs/${runId}`);
    const status = r.json?.run?.status;
    if (!status || TERMINAL.has(status)) {
      inFlight.delete(runId);
      if (status) {
        progress.dispatched[taskId] = { runId, status, finishedAt: new Date().toISOString() };
        saveProgress();
        console.log(`[done] ${taskId} -> ${status} (score=${r.json?.run?.score})`);
      }
    }
  }
};

const main = async () => {
  console.log(`Sweep: ${taskIds.length} tasks | concurrency=${CONCURRENCY} | model=${MODEL} | difficulty=${DIFFICULTY}`);
  let dispatched = 0;
  let skipped = 0;
  for (const taskId of taskIds) {
    if (progress.dispatched[taskId]?.runId) { skipped += 1; continue; }
    // Throttle: wait until a slot frees up.
    while (inFlight.size >= CONCURRENCY) {
      await reapInFlight();
      if (inFlight.size >= CONCURRENCY) await sleep(5000);
    }
    const create = await api("POST", "/benchmark-runs", {
      taskId, difficulty: DIFFICULTY, model, cleanupPolicy: "retain",
    });
    const runId = create.json?.run?.id;
    if (!(create.ok && runId)) {
      console.error(`[skip] ${taskId} create failed: HTTP ${create.status} ${create.text?.slice(0, 160)}`);
      continue;
    }
    const start = await api("POST", `/benchmark-runs/${runId}/start`);
    if (!start.ok) {
      console.error(`[warn] ${taskId} start failed: HTTP ${start.status} ${start.text?.slice(0, 160)}`);
    }
    inFlight.set(runId, taskId);
    progress.dispatched[taskId] = { runId, status: "running", startedAt: new Date().toISOString() };
    saveProgress();
    dispatched += 1;
    console.log(`[start ${dispatched}/${taskIds.length}] ${taskId} -> ${runId}`);
    await sleep(750); // gentle pacing on create/start
  }
  // Drain remaining in-flight so progress reflects final statuses.
  while (inFlight.size > 0) {
    await reapInFlight();
    if (inFlight.size > 0) await sleep(5000);
  }
  console.log(`Sweep dispatch complete. dispatched=${dispatched} skipped(existing)=${skipped}. Follow-ups auto-fire on vulnerable runs; the reconcile loop advances them.`);
};

main().catch((e) => { console.error(e); process.exit(1); });
