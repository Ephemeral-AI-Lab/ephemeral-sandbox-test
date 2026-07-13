import type { CatalogCase, CatalogPage, Health, Preview, RunProjection, RunsPage, Workspaces } from "../src/types";
import { asyncStateCopy } from "../src/state-copy";

const familyCases = (domain_id: string, kind: string, familyIds: string[]): CatalogCase[] => familyIds.map((family_id) => ({
  test_id: `${domain_id}.${family_id}`,
  case_id: "default",
  title: `${domain_id} ${family_id}`,
  purpose: `A generated ${domain_id} fixture for ${family_id}.`,
  domain_id,
  family_id,
  group_id: "generated",
  kind,
  runnable: true,
  effective_features: kind === "harness" ? [] : [`${domain_id}.${family_id}`],
  validations: [{ id: "contract", required: true }],
  execution_surface: kind === "harness" ? null : "runtime_cli",
  ...(domain_id === "compound" ? {
    compound: {
      complexity_id: family_id,
      subject_domain_ids: ["manager", "runtime"],
      components: [
        { id: "manager.management", role: "subject" },
        { id: "runtime.command", role: "subject" },
      ],
      shared_workspace: true,
      teardown_contract: "pytest fixture teardown",
    },
  } : {}),
}));

export const catalogCases: CatalogCase[] = [
  ...familyCases("manager", "product", ["lifecycle"]),
  ...familyCases("runtime", "product", ["command", "file", "daemon_http", "network_isolation", "reserved_paths", "shell_security", "workspace_session"]),
  ...familyCases("observability", "product", ["snapshot", "trace", "events", "cgroup", "layerstack"]),
  ...familyCases("compound", "compound", ["cross_boundary"]),
  ...familyCases("harness", "harness", ["catalog", "runner", "reducer", "storage", "api", "ui"]),
  {
    test_id: "translation.long-path",
    case_id: "future",
    title: "A deliberately long translated-like generated taxonomy record that must wrap safely",
    purpose: "Proves an additive domain, family, owner, validation, and unknown display hint need no UI change.",
    domain_id: "translation",
    family_id: "long_path",
    group_id: "future_group",
    scenario_id: "future_scenario",
    kind: "product",
    runnable: false,
    owner_id: "future-owner",
    validations: [{ id: "future-validation", required: true }],
    execution_surface: "http",
    display_hint: "future-value",
  },
];

const facets = (field: "domain_id" | "family_id" | "kind") => Object.fromEntries(catalogCases.reduce<Map<string, number>>((values, item) => {
  const value = String(item[field] ?? "unknown");
  values.set(value, (values.get(value) ?? 0) + 1);
  return values;
}, new Map()));

export const catalog: CatalogPage = {
  catalog_revision: "sha256:fixture-catalog-v1",
  source_revision: "sha256:fixture-source-v1",
  items: catalogCases,
  total: catalogCases.length,
  page: { limit: 50, cursor: null, next_cursor: null },
  facets: { domain_id: facets("domain_id"), family_id: facets("family_id"), kind: facets("kind") },
};

export const health: Health = {
  catalog: { state: "ready", current_revision: catalog.catalog_revision },
  lane: { active_run_id: null },
  roots: { test_repository_root: "/fixture/test", product_root: "/fixture/product", e2e_state_root: "/fixture/state" },
  nonce: "fixture-nonce",
};

export const preview: Preview = {
  schema_version: 1,
  preview_id: "preview-fixture",
  state: "ready",
  created_at: "2026-07-13T00:00:00Z",
  expires_at: "2099-07-13T01:00:00Z",
  admission_token: "fixture-token",
  catalog_revision: catalog.catalog_revision,
  source_revision: catalog.source_revision,
  case_count: 1,
  cases: [catalogCases[0]],
  ordered_cases: [catalogCases[0]],
  policies: { fail_fast: false },
  workspace_template: "template-default",
  disk_estimate: 65536,
  controller_bundle_digest: "sha256:fixture-controller",
  runner_bundle_digest: "sha256:fixture-runner",
  product_builds: {},
  preflight: [{ id: "catalog", state: "ready", reason_code: "catalog_current", message: "The selected revision is current.", observed_at: "2026-07-13T00:00:01Z", evidence_summary: {} }],
  blockers: [],
  warnings: [],
  parent_run_id: null,
  preview_digest: "sha256:fixture-preview",
};

export const run: RunProjection = {
  schema_version: 1,
  kind: "run_projection",
  run_id: "run-fixture",
  preview_id: preview.preview_id,
  state: "failed",
  created_at: "2026-07-13T00:00:00Z",
  catalog_revision: catalog.catalog_revision,
  source_revision: catalog.source_revision,
  policies: { fail_fast: true },
  case_counts: { passed: 1, failed: 1, not_run: 3 },
  first_failure_id: "first",
  primary_failure_id: "cleanup",
  failures: [{ id: "first", message: "First assertion failed", severity: "error", test_id: "runtime.file", case_id: "default", seq: 20 }, { id: "cleanup", message: "Cleanup also failed", severity: "fatal", test_id: "runtime.file", case_id: "default", seq: 39 }],
  evidence_health: "degraded",
  applied_through_seq: 42,
  last_event_at: "2026-07-13T00:01:00Z",
  journal_health: "complete",
  retention: { state: "retained" },
  cases: [
    { test_id: "runtime.command", case_id: "default", title: "Runtime command", state: "passed", phases: { execute: "passed" }, validations: { contract: "passed" }, cleanup: { workspace: "passed" }, surfaces: [], evidence: [] },
    { test_id: "runtime.file", case_id: "default", title: "Runtime file", state: "failed", phases: { execute: "failed" }, validations: { contract: "failed" }, cleanup: { workspace: "error" }, surfaces: [{ seq: 18, boundary: "runtime_cli" }], evidence: [{ type: "log.recorded", seq: 40, evidence_id: "log-runtime-file", availability: "partial", storage_ref: "runs/run-fixture/logs/runtime-file.txt", media_type: "text/plain", sha256: "sha256:fixture-log" }] },
    ...["File edit", "File blame", "File list"].map((title) => ({ test_id: title, case_id: "fail-fast", title, state: "not_run", phases: {}, validations: {}, cleanup: {}, surfaces: [], evidence: [] })),
  ],
};

export const runs: RunsPage = { items: [run], history_state: "complete", corrupt_records: 0, page: { next_cursor: null } };
export const workspaces: Workspaces = { template: [], active_attempts: [], quarantine: [], recent_purges: [] };

export const asyncStateFixture = Object.keys(asyncStateCopy) as Array<keyof typeof asyncStateCopy>;

export const envelope = <T,>(data: T) => ({ schema_version: 1 as const, data });
