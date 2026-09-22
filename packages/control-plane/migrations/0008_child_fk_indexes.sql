-- Secondary indexes on child/foreign-key columns so "rows for parent X"
-- lookups (timelines, stages, findings) don't full-scan as data grows.
-- All additive and idempotent.

create index if not exists benchmark_run_events_run_id
  on benchmark_run_events (run_id);

create index if not exists benchmark_run_results_run_id
  on benchmark_run_results (run_id);

create index if not exists cve_followups_run_id
  on cve_followups (run_id);

create index if not exists cve_followup_stages_followup_id
  on cve_followup_stages (followup_id);

create index if not exists cve_followup_events_followup_id
  on cve_followup_events (followup_id);

create index if not exists cve_followup_validations_stage_id
  on cve_followup_validations (stage_id);

create index if not exists audit_findings_audit_id
  on audit_findings (audit_id);

create index if not exists audit_findings_shard_id
  on audit_findings (shard_id);

create index if not exists audit_events_audit_id
  on audit_events (audit_id);
