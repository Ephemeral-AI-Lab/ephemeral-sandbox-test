export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export type CatalogCase = {
  test_id: string;
  case_id: string;
  title: string;
  purpose?: string;
  description?: string;
  domain_id?: string;
  family_id?: string;
  group_id?: string;
  scenario_id?: string;
  kind?: string;
  runnable?: boolean;
  owner_id?: string;
  source?: string;
  pytest_nodeid?: string;
  effective_features?: string[];
  direct_feature_ids?: string[];
  validations?: Array<{ id: string; required?: boolean; phase?: string; feature_id?: string }>;
  execution_surface?: string | null;
  execution_label_ids?: string[];
  compound?: {
    complexity_id: string;
    subject_domain_ids: string[];
    components: Array<{ id: string; role: string }>;
    shared_workspace: boolean;
    teardown_contract: string;
  };
  [key: string]: unknown;
};

export type CatalogPage = {
  catalog_revision: string;
  source_revision: string;
  items: CatalogCase[];
  total: number;
  page: { limit: number; cursor: string | null; next_cursor: string | null };
  facets: Record<string, Record<string, number>>;
};

export type Health = {
  catalog: Record<string, Json>;
  lane: { active_run_id: string | null };
  roots: Record<string, string>;
  nonce: string;
};

export type Preview = {
  schema_version: 1;
  preview_id: string;
  state: "checking" | "ready" | "blocked" | "stale" | "expired";
  created_at: string;
  expires_at: string;
  catalog_revision: string;
  source_revision: string;
  admission_token?: string;
  case_count: number;
  cases: CatalogCase[];
  ordered_cases: CatalogCase[];
  policies: Record<string, Json>;
  workspace_template: string;
  disk_estimate: number;
  controller_bundle_digest: string;
  runner_bundle_digest: string;
  product_builds: Record<string, Json>;
  preflight: Array<{
    id: "catalog" | "source" | "recovery" | "lane" | "disk";
    state: "ready" | "blocked";
    reason_code: string;
    message: string;
    observed_at: string;
    evidence_summary: Record<string, Json>;
  }>;
  blockers: Array<{ reason_code: string; message: string }>;
  warnings: Json[];
  parent_run_id: string | null;
  preview_digest: string;
};

export type EvidenceRecord = {
  type: "log.recorded" | "artifact.recorded" | "evidence.recorded";
  seq: number;
  evidence_id?: string;
  availability?: "available" | "partial" | "unavailable" | "unsupported" | "invalid";
  role?: "supporting" | "validation_bound";
  storage_ref?: string;
  sha256?: string;
  media_type?: string;
  [key: string]: Json | undefined;
};

export type EvidenceResponse = {
  kind: "record" | "content";
  runId: string;
  evidenceId: string;
  mediaType: string;
  record?: Record<string, Json>;
  text?: string;
  retainedBytes?: number;
  omittedBytes?: number;
  omittedLines?: number;
};

export type RunProjection = {
  schema_version?: 1;
  kind?: "run_projection";
  run_id: string;
  preview_id?: string;
  state: string;
  created_at?: string;
  catalog_revision?: string;
  source_revision?: string;
  parent_run_id?: string | null;
  policies?: Record<string, Json>;
  case_counts?: Record<string, number>;
  cases?: Array<{
    test_id: string;
    case_id: string;
    title?: string;
    state: string;
    phases?: Record<string, string>;
    validations?: Record<string, string>;
    cleanup?: Record<string, string>;
    surfaces?: Array<Record<string, Json>>;
    evidence?: EvidenceRecord[];
  }>;
  first_failure_id?: string | null;
  primary_failure_id?: string | null;
  failures?: Array<{ id: string; message: string; severity: string; seq?: number; test_id?: string | null; case_id?: string | null; entity_id?: string | null; caused_by_seq?: number | null }>;
  evidence_health?: string;
  retention?: { state?: string; [key: string]: Json | undefined };
  applied_through_seq?: number;
  last_event_at?: string | null;
  journal_health?: "complete" | "truncated";
  recovery?: { blocker?: Json; history?: Json[] };
  recovery_bundle_match?: string;
  [key: string]: unknown;
};

export type RunsPage = {
  items: Array<Pick<RunProjection, "run_id" | "state" | "created_at" | "catalog_revision" | "source_revision" | "parent_run_id" | "case_counts" | "evidence_health" | "retention">>;
  history_state: "complete" | "partial";
  corrupt_records: number;
  page: { next_cursor: string | null };
};

export type Workspaces = {
  template: Array<{ workspace_id?: string; state?: string; [key: string]: Json | undefined }>;
  active_attempts: Array<{ workspace_id: string; run_id?: string; role: string }>;
  quarantine: Array<{ workspace_id: string; run_id?: string; role: string }>;
  recent_purges: Array<{ workspace_id: string; run_id?: string; state: string }>;
};
