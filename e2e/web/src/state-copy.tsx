import { Paper, Stack, Text, Title } from "@mantine/core";

/**
 * The UI design's operator-facing async contract.  One generic presenter keeps
 * the copy testable without coupling a state to a product feature or case.
 */
export const asyncStateCopy = {
  catalog_loading: ["Loading test catalog…", "Reading the current published revision."],
  catalog_refreshing: ["Updating test catalog — inputs changed.", "Recollecting now; revision {R} remains the last verified catalog."],
  catalog_filtered_empty: ["No cases match these filters.", "Clear a filter or change the search."],
  catalog_empty_group: ["This group has no runnable cases.", "The group exists in revision {R}, but no runnable case is registered."],
  catalog_invalid_last_good: ["Catalog update failed — showing the last good revision.", "{diagnostic}. Admission is blocked until a valid refresh succeeds."],
  catalog_unavailable: ["Test catalog is unavailable.", "No valid published revision exists. {first diagnostic}."],
  catalog_incompatible: ["Catalog schema is not supported by this UI.", "This UI did not interpret the published data."],
  preview_checking: ["Checking {check name}…", "Start is unavailable while {reason for check} is verified."],
  preview_ready: ["Ready to start {N} exact cases.", "Scope is frozen at revision {R} until {expiry}; Start creates one run."],
  preview_blocked: ["Run blocked — {blocker label}.", "{affected scope}. {safe recovery instruction}."],
  preview_stale: ["Review is out of date.", "{catalog/source/preflight fact} changed; the old scope was not submitted."],
  preview_error: ["Readiness check failed.", "{typed reason}. Request {request ID}; nothing was submitted."],
  admission_pending: ["Starting one run…", "The reviewed {N} cases are being admitted; duplicate Start is disabled."],
  active_run_conflict: ["Another run owns the execution lane.", "Run {ID} is {state}; your reviewed scope was not started."],
  admission_nonce_expired: ["This tab is no longer authorized to start runs.", "Reload before trying again. Nothing was submitted."],
  run_queued: ["Run queued.", "Run {ID} is waiting for its controller-owned serial lane."],
  preparing: ["Preparing workspace for {case title}…", "Creating fresh attempt {ID}; product execution has not started."],
  case_running: ["Running {case title} — {phase label}.", "{validation or action}; elapsed {duration}; evidence current as of {time}."],
  failure_detected: ["Failure detected — finalization continues.", "First failure: {message}. Teardown, cleanup, and evidence may still change the primary verdict."],
  fail_fast: ["Fail-fast stopped new cases.", "Failure {ID} caused {N} remaining cases to become Not run; active cleanup continues."],
  cleaning_up: ["Cleaning up {resource or attempt}…", "Mandatory cleanup is still running; the run cannot finish yet."],
  evidence_finalizing: ["Finalizing evidence…", "Product work ended; integrity, redaction, caps, and availability are still being recorded."],
  cancelling: ["Cancelling — cleanup continues.", "Stopping product work at {stage}; mandatory cleanup continues until {deadline}."],
  reconnecting: ["Reconnecting to live updates…", "Showing verified state through sequence {N} at {time}; missing events will replay or trigger one snapshot refresh."],
  stream_stale: ["Live updates delayed.", "No event arrived for {duration}; showing state as of sequence {N} at {time}. Timers are paused."],
  disconnected: ["Disconnected — showing last verified state.", "Controller state is unknown after sequence {N} at {time}."],
  recovering: ["Recovering interrupted run.", "{action label}, {completed}/{total}; only scoped resources are being reconciled."],
  recovery_blocked: ["Recovery blocked — controller bundle changed.", "State is verified through sequence {N}. No process was signalled and no cleanup was attempted."],
  run_incompatible: ["Run data uses an unsupported schema.", "Unknown state was not mapped to success."],
  terminal_passed: ["Passed.", "Required checks and cleanup passed. Evidence health: {health}."],
  terminal_failed: ["Failed.", "First failure: {first}. Primary cause: {primary}. Cleanup: {state}. Evidence: {health}."],
  terminal_error: ["Error.", "{infrastructure, cleanup, contract, or recovery cause}. Product assertions are shown separately."],
  evidence_degraded: ["Product checks passed; evidence is degraded.", "{missing/truncated reason}; retained {amount}."],
  evidence_unavailable: ["Evidence unavailable.", "Verdict source remains; {availability reason}."],
  evidence_purged: ["Evidence was purged.", "Verdict, validations, lineage, and purge time remain."],
  history_empty: ["No runs yet.", "Start from Catalog to create the first run."],
  history_partial: ["History is partial.", "{N} run records could not be read; visible rows remain verified."],
  history_unavailable: ["Run history is unavailable.", "The store could not be read safely."],
  workspace_purge_partial: ["Purge incomplete.", "Removed {N}, retained {N}, failed {N}; lineage remains."],
  demo_fixture: ["Demo data — no runner connected.", "Actions here do not start or cancel a real run."],
} as const;

export type AsyncState = keyof typeof asyncStateCopy;

export function AsyncStateNotice({ state, detail }: { state: AsyncState; detail?: string }) {
  const [headline, explanation] = asyncStateCopy[state];
  return <Paper className="async-state" role="status" aria-live="polite" p="md"><Stack gap={4}><Title order={3}>{headline}</Title><Text>{detail ?? explanation}</Text></Stack></Paper>;
}
