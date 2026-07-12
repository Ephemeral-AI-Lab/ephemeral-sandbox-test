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
  preview_id: "preview-fixture",
  state: "ready",
  admission_token: "fixture-token",
  catalog_revision: catalog.catalog_revision,
  expires_at: "2026-07-13T01:00:00Z",
  case_count: 1,
  cases: [catalogCases[0]],
  preflight: [{ label: "Catalog revision", state: "ready", message: "The selected revision is current." }],
};

export const run: RunProjection = {
  run_id: "run-fixture",
  state: "failed",
  created_at: "2026-07-13T00:00:00Z",
  completed_at: "2026-07-13T00:01:00Z",
  case_counts: { passed: 1, failed: 1, not_run: 3 },
  first_failure_id: "first",
  primary_failure_id: "cleanup",
  failures: [{ id: "first", message: "First assertion failed", severity: "error" }, { id: "cleanup", message: "Cleanup also failed", severity: "error" }],
  evidence_health: "degraded",
  evidence_summary: { truncation: { retained_bytes: 1024, omitted_bytes: 2048, omitted_lines: 16 } },
  applied_through_seq: 42,
  retention: { state: "retained" },
  cases: [
    { test_id: "runtime.command", case_id: "default", title: "Runtime command", state: "passed", validations: { contract: { state: "passed" } }, cleanup: { workspace: { state: "passed" } } },
    { test_id: "runtime.file", case_id: "default", title: "Runtime file", state: "failed", validations: { contract: { state: "failed" } }, cleanup: { workspace: { state: "error" } } },
    ...["File edit", "File blame", "File list"].map((title) => ({ test_id: title, case_id: "fail-fast", title, state: "not_run", validations: {}, cleanup: {} })),
  ],
};

export const runs: RunsPage = { items: [run], history_state: "complete", corrupt_records: 0, page: { next_cursor: null } };
export const workspaces: Workspaces = { template: [], active_attempts: [], quarantine: [], recent_purges: [] };

export const asyncStateFixture = Object.keys(asyncStateCopy) as Array<keyof typeof asyncStateCopy>;

export const envelope = <T,>(data: T) => ({ schema_version: 1 as const, data });
