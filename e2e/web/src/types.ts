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
  compound?: { complexity_id?: string; subject_domain_ids?: string[]; component_roles?: string[] };
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
  preview_id: string;
  state: "checking" | "ready" | "blocked" | "stale" | "expired";
  admission_token?: string;
  catalog_revision: string;
  expires_at?: string;
  case_count: number;
  cases: CatalogCase[];
  blockers?: Array<{ reason_code?: string; message?: string }>;
  preflight?: Array<{ label?: string; state?: string; message?: string }>;
};

export type RunProjection = {
  run_id: string;
  state: string;
  created_at?: string;
  completed_at?: string;
  case_counts?: Record<string, number>;
  cases?: Array<{
    test_id: string;
    case_id: string;
    title?: string;
    state: string;
    validations?: Record<string, { state?: string; evidence?: Array<{ evidence_id?: string; availability?: string }> }>;
    cleanup?: Record<string, { state?: string }>;
    failures?: Array<{ id?: string; message?: string; severity?: string }>;
  }>;
  first_failure_id?: string | null;
  primary_failure_id?: string | null;
  failures?: Array<{ id?: string; message?: string; severity?: string }>;
  evidence_health?: string;
  evidence_summary?: {
    truncation?: { retained_bytes?: number; omitted_bytes?: number; omitted_lines?: number };
  };
  retention?: { state?: string; [key: string]: Json | undefined };
  applied_through_seq?: number;
  recovery?: { blocker?: Json; history?: Json[] };
  [key: string]: unknown;
};

export type RunsPage = {
  items: Array<Pick<RunProjection, "run_id" | "state" | "created_at" | "case_counts" | "evidence_health" | "retention">>;
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
