// Generates apps/docs/lib/tasks-data.json from benchmark/data/tasks.
// Run via `pnpm --dir apps/docs generate:tasks-data` (also runs on prebuild).
import { readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(__dirname, "../../..");
const tasksDir = join(repoRoot, "benchmark/data/tasks");
const outFile = join(__dirname, "../lib/tasks-data.json");

const files = (await readdir(tasksDir))
  .filter((f) => f.endsWith(".json"))
  .sort((a, b) => a.localeCompare(b));

const entries = [];
for (const file of files) {
  const task = JSON.parse(await readFile(join(tasksDir, file), "utf8"));
  const { codebase = {}, hints = {}, ground_truth = {} } = task;
  entries.push({
    task_id: task.task_id,
    ghsa_id: task.ghsa_id,
    language: codebase.language,
    ecosystem: codebase.ecosystem,
    repo: codebase.repo,
    vuln_class: ground_truth.vuln_class,
    cvss: ground_truth.cvss ?? null,
    reason: ground_truth.reason,
    locations: ground_truth.locations ?? [],
    hint_l1: hints.L1?.area ?? null,
    hint_l2: hints.L2?.description ?? null,
  });
}

await writeFile(outFile, `${JSON.stringify(entries, null, 2)}\n`);
process.stdout.write(`Wrote ${entries.length} task(s) to ${outFile}\n`);
