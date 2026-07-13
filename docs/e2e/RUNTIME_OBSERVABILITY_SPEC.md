# Runtime observability for E2E test runs

Status: Proposed  
Audience: E2E harness, sandbox observability, and Control Room UI maintainers  
Scope: Design only; this document does not authorize or implement code changes

## 1. Decision summary

Each executed E2E case will publish one case-scoped `runtime_observability`
artifact record. When the case owns one or more sandboxes, the artifact contains
a bounded NDJSON time series for sandbox CPU, memory, and I/O, workspace CPU,
memory, disk, and file counts, operation markers, and explicit sampling gaps.

The first release is durable and post-run:

- one child-local collector samples all sandboxes owned by the current case;
- raw samples are stored under the run's existing `evidence/` directory;
- one small `artifact.recorded` payload carries summary values and coverage;
- the existing evidence API serves the raw artifact;
- the selected-case UI renders summary cards and a native SVG timeline;
- collection failure changes evidence health, never the functional test verdict.

True midflight UI updates are a second release. They require changing the parent
runner from a blocking `subprocess.run` call to a `Popen` ingest loop. Raw
one-second samples will still remain outside the run journal.

No product-side metrics service, time-series database, Prometheus dependency,
new chart library, or new API endpoint is required for the first release.

## 2. Problem

The current run page can show verdicts, validations, phases, cleanup, execution
surface proof, and published evidence. It cannot answer these debugging
questions:

1. Did the sandbox become CPU-bound during the failure?
2. Did memory climb, peak, or approach its cgroup limit?
3. How much container I/O occurred?
4. Did a workspace's logical or allocated disk use grow unexpectedly?
5. Which operation coincided with a resource spike?
6. Are missing points a real idle period or an observability gap?
7. Was observability complete, partial, unavailable, unsupported, or not
   applicable to this case?

The existing **Log records** panel correctly says that logs are unavailable
when no `log.recorded` events were published. Runtime resource data is a
different evidence type and must not be presented as logs.

## 3. Goals

- Attach resource evidence to the exact run, test case, attempt, sandbox, and
  workspace that produced it.
- Retain enough time-series detail to debug a completed failure.
- Show CPU, memory, I/O, disk, file-count, timing, and coverage summaries without
  loading raw evidence for every case.
- Correlate resource samples with pytest phases, product operations, and the
  first failure.
- Make gaps and unsupported metrics explicit.
- Preserve current test semantics and cleanup behavior.
- Bound CPU overhead, subprocess count, disk use, journal size, and API output.
- Recover useful partial evidence after a timeout, crash, or interrupted write.
- Keep secrets, request arguments, command output, and workspace contents out of
  the resource artifact.

## 4. Non-goals

- A general-purpose production monitoring system.
- Host-wide CPU, memory, or disk monitoring.
- Per-process profiling, flame graphs, eBPF, or syscall traces.
- Replacing daemon traces, domain events, pytest output, or `log.recorded`.
- Enforcing performance SLOs in the first release.
- Reconstructing resource history that the product never sampled.
- Persisting unlimited history or exposing evidence after run evidence is purged.
- Live charts in the first release.

## 5. Current constraints

The design relies on the following current behavior.

### 5.1 Product metrics

- A sandbox-scope cgroup query asks the manager runtime for a fresh Docker
  reading and records one manager-side sample.
- The sandbox reading can contain `cpu_usec`, `mem_cur`, `mem_max`,
  `io_rbytes`, and `io_wbytes`, with `metrics_source: docker_engine`.
- Manager resource history is bounded to 600,000 ms. Querying with
  `window_ms=0` returns the newly recorded current sample without replaying prior
  samples.
- Because that one-sample response has no preceding sample, the harness must
  compute counter deltas from successive absolute readings. It must not depend
  on response `deltas` for sandbox polling.
- Daemon workspace samples can contain `cpu_usec`, `mem_cur`, `mem_max`,
  `mem_max_unlimited`, `disk_bytes`, `disk_allocated_bytes`, `files`, and
  `disk_truncated`.
- Daemon resource samples are operation-driven. A snapshot refreshes live
  resource samples before it responds.
- Workspace history can be queried by a remembered workspace ID even after the
  workspace has been destroyed, while the sandbox daemon and retained sample log
  still exist.

### 5.2 Harness and storage

- The pytest child currently publishes one case result only after teardown.
- The parent currently waits for the entire child through `subprocess.run` and
  reduces case results afterward; therefore the UI cannot receive real case
  progress from the current architecture.
- The parent is the sole run-journal writer.
- A journal event is capped at 64 KiB, so a time series must not be embedded in
  an event.
- `artifact.recorded` is already supported and projected as case evidence.
- Each run already owns an `evidence/` directory.
- The evidence endpoint verifies the recorded SHA-256, rejects unsafe paths and
  symlinks, redacts content, and returns at most 5 MiB.
- Run purge already removes the run's `evidence/` directory while preserving
  verdict lineage.

## 6. User experience

Runtime resources appear inside the selected case. They do not replace the
run-level log panel.

```text
+-- Selected case -----------------------------------------------------------+
| Runtime: command timeout                         FAILED                    |
| runtime.command.timeout:default                                            |
+---------------------------------------------------------------------------+
| Phases       | Validations        | Cleanup        | Surfaces              |
| setup  pass  | command-exits fail | teardown pass  | cli  3 calls          |
+---------------------------------------------------------------------------+
| Runtime resources                                      [PARTIAL]           |
| Scope [All v]  Sandbox [eos-a1 v]  Interval 1.0 s  Source docker + daemon  |
| Coverage 98 / 100 samples  |  1 gap (2.1 s)  |  View raw samples          |
|                                                                           |
| CPU peak       Memory peak       Container I/O       Workspace disk        |
| 1.42 cores     742 / 1024 MiB    R 38 MiB           214 MiB logical        |
| 6.82 CPU-s     72.5% of limit    W 11 MiB           301 MiB allocated      |
|                                                                           |
| setup                 call                                  teardown       |
| |---------------------|-------------------------------------|---------|     |
| CPU  __/^^\___________/^^^^^^\_______!_____________________/\_______      |
| MEM  ___/^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\________________________      |
| DISK _________/^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^______________________      |
|                 create_ws   exec_command      assertion failed             |
|                                                                           |
| ! Missing samples from 12:03:14.100 to 12:03:16.220: query timeout         |
+---------------------------------------------------------------------------+
| Evidence                                                                  |
| [archive] Runtime observability  runtime-2d80...  Partial                  |
+---------------------------------------------------------------------------+
| Bounded output                                                            |
| Log records                                             [UNAVAILABLE]      |
| No log records were published for any case.                                |
+---------------------------------------------------------------------------+
```

### 6.1 Panel states

| Resource status | UI behavior |
| --- | --- |
| `available` | Render cards, timeline, coverage, and raw-artifact action. |
| `partial` | Render retained data plus a prominent gap/error banner. |
| `unavailable` | Show the attempted sources, bounded error reason, and no chart. |
| `unsupported` | Explain which platform or metric source is unsupported. |
| `not_applicable` | Explain that the case never owned a sandbox; do not degrade evidence health. |
| `purged` | Keep summary identity and verdict lineage; raw data action returns the existing purged state. |
| `invalid` | Do not render derived values from corrupt or digest-mismatched data. |

### 6.2 Scope controls

The scope selector offers:

- **All**: sandbox series plus workspace disk overlays;
- **Sandbox**: one selected sandbox's container totals;
- **Workspace**: one selected workspace's CPU, memory, disk, and files.

If a case creates multiple sandboxes or workspaces, selectors list stable IDs.
Summary cards clearly label whether a value is an aggregate, maximum, or total.
The UI must never add memory peaks from different scopes together.

### 6.3 Timeline markers

The timeline includes:

- setup, call, and teardown boundaries;
- operation start and finish, surface, duration, and return code;
- first case failure or failed validation;
- explicit sample gaps;
- sandbox create/final-sample boundaries;
- workspace session create/destroy boundaries when observed.

The resource artifact does not contain request arguments, stdout, stderr, raw
assertion values, file paths, file contents, environment variables, or auth
tokens.

## 7. Architecture

```text
pytest child
  |
  +-- reporter binds current test + pytest phase
  |
  +-- cleanup.track(sandbox_id) -----------+
  |                                         |
  +-- CLI / HTTP / direct daemon ----------+--> child-local resource collector
  |      operation markers + workspace IDs |      - one scheduler thread
  |                                         |      - immediate + 1 Hz samples
  +-- cleanup.untrack(sandbox_id) ----------+      - final workspace harvest
                                                |
                                                +--> .ndjson.part
                                                +--> atomic .ndjson
                                                +--> case result artifact summary
                                                            |
parent runner reads pytest-report.jsonl after child exit      |
  |                                                          |
  +-- artifact.recorded event <------------------------------+
  +-- existing reducer -> run.json
  +-- existing SSE/API -> React run page
  +-- existing evidence endpoint -> raw NDJSON on demand
```

### 7.1 Collector ownership

The pytest child owns one collector instance and one scheduler thread. The
thread is idle when no sandbox is registered and polls all active sandbox
scopes for the current serial case. A single thread is sufficient because the
runner is serial and avoids one thread per case or sandbox.

Collector state is keyed by manifest node ID and sandbox ID. It uses a separate
lock from the reporter so product operations and pytest reporting do not block
on file writes or metric subprocesses.

### 7.2 Sandbox lifecycle

1. A successful create helper calls `cleanup.track(sandbox_id)`.
2. `track` registers the sandbox against the current case and schedules an
   immediate sandbox-scope sample.
3. The scheduler samples each active sandbox every 1,000 ms.
4. `cleanup.untrack(sandbox_id)` requests a bounded final harvest before the
   existing destroy operation begins.
5. The final harvest records a sandbox sample, requests an observability
   snapshot to refresh live workspace data, and queries maximum retained
   history for every remembered workspace ID.
6. At pytest teardown, the reporter finalizes the case artifact. Any sandbox
   still tracked by a failed test receives one last best-effort sample, but
   remains registered for the existing session cleanup safety net.

The collector must not broaden cleanup ownership or destroy resources itself.

### 7.3 Sampling transport

Sandbox polling invokes the preserved public observability CLI equivalent of:

```text
sandbox-observability-cli cgroup \
  --sandbox-id <id> \
  --scope sandbox \
  --window-ms 0
```

The collector uses a private low-level subprocess helper, not the instrumented
test `cli()` wrapper. Otherwise its own sampling calls would pollute execution
surface proof and operation timing statistics or recursively instrument
themselves.

Each polling subprocess has a short timeout. A timeout or malformed response
produces a gap record and never raises into the test body.

### 7.4 Workspace discovery and harvest

Workspace IDs are remembered from trusted structured request/response
boundaries:

- direct-daemon workspace-session create/destroy;
- runtime CLI operations with a `workspace_session_id`;
- structured runtime or observability responses that identify a workspace;
- allowlisted daemon HTTP helpers;
- the specialized export and squash raw helpers that bypass the shared CLI.

At final harvest, the collector requests an observability snapshot, then queries
each remembered workspace scope with the product's maximum 600,000 ms window.
This captures samples for workspaces that have already been destroyed but still
exist in the daemon's retained sample log.

Automatic sessions that start and disappear entirely between observable
boundaries may not yield workspace-level history. The sandbox-level Docker
series remains the authoritative total for those commands, and the artifact
records the workspace limitation rather than inventing data.

## 8. Data sources and displayed metrics

| Scope | Source | Raw fields | UI values |
| --- | --- | --- | --- |
| Sandbox | Manager runtime / Docker engine | `cpu_usec`, `mem_cur`, `mem_max`, `io_rbytes`, `io_wbytes` | CPU cores, CPU time, memory peak/limit, read/write bytes |
| Workspace | Sandbox daemon cgroup | `cpu_usec`, `mem_cur`, `mem_max`, `mem_max_unlimited`, availability/error fields | CPU cores, CPU time, memory peak/limit |
| Workspace | Sandbox daemon upperdir walk | `disk_bytes`, `disk_allocated_bytes`, `files`, `disk_truncated` | logical bytes, allocated bytes, growth, file peak, truncation warning |

Host-wide utilization is intentionally excluded because it cannot be attributed
to the selected test case.

### 8.1 Derived values

For two valid samples from the same source and scope:

```text
delta_wall_ms = current.ts - previous.ts
delta_cpu_usec = current.cpu_usec - previous.cpu_usec
cpu_cores = delta_cpu_usec / (delta_wall_ms * 1000)
memory_ratio = mem_cur / mem_max
```

CPU is labeled in **cores**, not ambiguous percent. `1.42 cores` means 142% of
one logical core. If a percentage is shown secondarily, its denominator must be
stated.

Rules:

- calculate deltas only when timestamps increase and counters do not decrease;
- a counter decrease starts a new segment and emits `counter_reset` as a gap;
- sum valid positive counter deltas for CPU time and I/O totals;
- calculate memory ratio only for a positive bounded `mem_max`;
- show `Unlimited` when `mem_max_unlimited` is true;
- do not interpolate across a gap longer than 2.5 sampling intervals;
- do not sum workspace memory peaks or disk peaks across time;
- show disk walk truncation beside the affected value.

## 9. Raw artifact contract

### 9.1 Location and identity

One finalized raw file is stored per applicable case attempt:

```text
runs/<run-id>/evidence/runtime/<case-key>.ndjson
```

`storage_ref` is relative to the run's `evidence/` directory:

```text
runtime/<case-key>.ndjson
```

`case-key` is the lowercase hex SHA-256 of
`<test_id>\0<case_id>\0<attempt_id>`. The opaque evidence ID is
`runtime-<first-32-hex>`. IDs never contain source paths, pytest selectors, or
user-controlled separators.

### 9.2 Common record envelope

Every NDJSON line is one complete JSON object:

```json
{
  "schema_version": 1,
  "kind": "sample",
  "offset_ms": 1524.7
}
```

`offset_ms` is measured from case start using the child's monotonic clock. It is
the ordering authority inside the case. Wall-clock timestamps are included for
human correlation but are not used to calculate durations.

### 9.3 Metadata record

The first line is:

```json
{
  "schema_version": 1,
  "kind": "metadata",
  "offset_ms": 0,
  "run_id": "run-...",
  "test_id": "runtime.command.timeout",
  "case_id": "default",
  "attempt_id": "attempt-1",
  "started_at": "2026-07-14T12:03:01.000Z",
  "sample_interval_ms": 1000
}
```

### 9.4 Sample record

```json
{
  "schema_version": 1,
  "kind": "sample",
  "offset_ms": 1524.7,
  "observed_at": "2026-07-14T12:03:02.525Z",
  "source_ts_ms": 1784020982522,
  "phase": "call",
  "sandbox_id": "eos-a1",
  "scope": {"kind": "sandbox", "id": "sandbox"},
  "source": "docker_engine",
  "metrics": {
    "cpu_usec": 4812200,
    "mem_cur": 778043392,
    "mem_max": 1073741824,
    "io_rbytes": 39845888,
    "io_wbytes": 11534336
  },
  "delta": {
    "sample_ms": 1004,
    "cpu_usec": 1425680,
    "io_rbytes": 4194304,
    "io_wbytes": 1048576
  },
  "derived": {"cpu_cores": 1.420}
}
```

Workspace samples use `scope.kind: workspace`, the workspace ID in `scope.id`,
and `source: sandbox_daemon`. Fields absent from the source remain absent; they
are never written as zero.

### 9.5 Operation record

```json
{
  "schema_version": 1,
  "kind": "operation",
  "offset_ms": 2110.4,
  "phase": "call",
  "edge": "finish",
  "surface": "cli",
  "operation": "runtime.exec_command",
  "duration_ms": 843.2,
  "returncode": 0,
  "sandbox_id": "eos-a1",
  "workspace_id": "ws-7"
}
```

Allowed operation fields are identifiers, timing, phase, surface, and outcome.
Arguments and response bodies are prohibited.

### 9.6 Gap record

```json
{
  "schema_version": 1,
  "kind": "gap",
  "offset_ms": 5220.1,
  "from_offset_ms": 3112.8,
  "to_offset_ms": 5220.1,
  "sandbox_id": "eos-a1",
  "scope": {"kind": "sandbox", "id": "sandbox"},
  "reason_code": "query_timeout",
  "message": "Sandbox resource sample timed out."
}
```

Messages are allowlisted, bounded to 512 characters, and must not contain raw
subprocess output.

## 10. Artifact summary contract

The case result publishes exactly one `artifacts` entry with
`kind: runtime_observability`, including a metadata-only `not_applicable` entry
when the case never owned a sandbox.

An applicable example:

```json
{
  "evidence_id": "runtime-2d80b7c8c9d64686a973a0ebaf19d3cc",
  "kind": "runtime_observability",
  "role": "supporting",
  "availability": "partial",
  "status": "partial",
  "media_type": "application/x-ndjson",
  "storage_ref": "runtime/2d80b7c8c9d64686a973a0ebaf19d3cc.ndjson",
  "sha256": "sha256:...",
  "sample_count": 98,
  "operation_count": 12,
  "gap_count": 1,
  "summary": {
    "cpu_peak_cores": 1.42,
    "cpu_time_seconds": 6.82,
    "memory_peak_bytes": 778043392,
    "memory_limit_bytes": 1073741824,
    "io_read_bytes": 39845888,
    "io_write_bytes": 11534336,
    "workspace_disk_peak_bytes": 224395264,
    "workspace_disk_allocated_peak_bytes": 315621376,
    "workspace_file_peak": 1842
  },
  "coverage": {
    "started_at": "2026-07-14T12:03:01.000Z",
    "ended_at": "2026-07-14T12:04:41.040Z",
    "sample_interval_ms": 1000,
    "expected_ticks": 100,
    "observed_ticks": 98,
    "missed_ticks": 2,
    "sandbox_count": 1,
    "workspace_count": 1
  },
  "errors": [
    {"reason_code": "query_timeout", "count": 1, "message": "Sandbox resource sample timed out."}
  ]
}
```

The summary payload must remain well below the existing 64 KiB journal-event
cap. Lists are bounded as follows:

- at most 20 scope summaries;
- at most 10 aggregated error entries;
- no raw samples;
- no raw operation arguments or output.

A non-applicable example has no file reference:

```json
{
  "evidence_id": "runtime-2d80b7c8c9d64686a973a0ebaf19d3cc",
  "kind": "runtime_observability",
  "role": "supporting",
  "availability": "available",
  "status": "not_applicable",
  "reason_code": "no_sandbox_observed",
  "message": "This case did not own a sandbox."
}
```

`availability: available` means the explicit metadata record is present;
`status: not_applicable` means no resource series was expected. The reducer
must not degrade evidence health for this state.

## 11. Availability and verdict semantics

Resource collection is supporting evidence, not a product assertion.

| Condition | Artifact status | Evidence health effect | Case verdict effect |
| --- | --- | --- | --- |
| All expected samples and finalization succeed | `available` | complete | none |
| Some samples, scopes, or finalization fail | `partial` | degraded | none |
| Applicable case yields no trustworthy samples | `unavailable` | unavailable | none |
| Required platform/source cannot provide metrics | `unsupported` | degraded | none |
| Case never owns a sandbox | `not_applicable` | none | none |
| Artifact is malformed or digest verification fails | `invalid` | invalid | none |

The generic run projection should start with `evidence_health: not_published`,
not `complete`. It becomes:

- `complete` after applicable publishers report available evidence;
- `degraded` for partial or unsupported applicable evidence;
- `unavailable` when an applicable producer publishes no trustworthy content;
- `invalid` for contract, integrity, or corruption failure.

This prevents an empty run from claiming complete evidence while keeping
functional verdicts independent from optional diagnostic collection.

## 12. Durability, limits, and recovery

### 12.1 Write protocol

1. Create `runtime/<case-key>.ndjson.part` with mode `0600`.
2. Append only complete JSON lines.
3. Flush and `fsync` at most once per sampling tick, batching all scopes for
   that tick.
4. Stop before 4 MiB to remain below the evidence endpoint's 5 MiB response
   bound.
5. Append a final `cap_reached` gap if space permits.
6. Flush, `fsync`, calculate SHA-256, atomically rename to `.ndjson`, and
   `fsync` the parent directory.

Sampling never silently truncates a file.

### 12.2 Interrupted cases

If the pytest child times out, crashes, or exits before normal finalization, the
parent examines only run-owned `.ndjson.part` files:

- retain the longest prefix of complete valid lines;
- reject records with the wrong schema or case identity;
- finalize the recovered prefix as a `partial` artifact;
- include `child_interrupted` or `torn_final_line` in bounded errors;
- publish `unavailable` if no trustworthy sample remains.

Recovery must not scan unrelated runs or workspaces.

### 12.3 Sampling budget

- default interval: 1,000 ms;
- one immediate and one final sample per sandbox;
- one scheduler thread per pytest child;
- one sandbox query in flight at a time in the first release;
- per-query timeout: 750 ms;
- no retry inside the same tick;
- maximum raw artifact: 4 MiB per case attempt;
- maximum retained workspace query window: 600,000 ms;
- no resource assertion or SLO by default.

If the collector falls behind, it skips the late slot and records a gap instead
of building an unbounded subprocess queue.

## 13. UI data flow

The run projection contains the bounded artifact summary, so the selected-case
panel can render status, cards, coverage, and scope inventory immediately.

The UI fetches and parses raw NDJSON only when:

- the user expands the timeline;
- the user changes to a scope that needs raw points; or
- the user opens the evidence drawer.

Parsing is line-by-line with a maximum point count per rendered series. When a
series contains more points than the viewport needs, the client uses a simple
min/max bucket reduction that preserves spikes. The raw artifact remains
available unchanged.

Use native SVG and existing UI primitives. Do not add a chart dependency for
three aligned series and event markers.

### 13.1 Accessibility

- Every chart has a text summary containing peak, end value, sample count, and
  gaps.
- Color is not the only status indicator; use line style, marker, label, and
  text.
- Operation and failure markers are keyboard-focusable.
- Tooltips are also available through focus, not hover only.
- Units are present in labels and accessible names.
- `not_applicable`, `partial`, and `unavailable` have distinct wording.

## 14. Midflight extension

Midflight observability is deliberately separated from durable post-run
capture.

### 14.1 Runner change

Replace the blocking pytest `subprocess.run` with `Popen` and a parent loop that
simultaneously:

- drains stdout and stderr without deadlock;
- enforces the existing run timeout and cancellation process-group behavior;
- tails the child reporter stream;
- ingests bounded resource summaries;
- appends normalized events as the sole journal writer.

### 14.2 Live summary event

Add one journal event type, `resource.summary`, no more than once every five
seconds for the running case. The payload contains only:

- latest and peak CPU cores;
- latest and peak memory;
- cumulative I/O deltas;
- latest workspace disk and file count;
- observed/expected ticks and latest gap;
- scope IDs and observation timestamp.

The reducer replaces `case.resources.latest` with the newest summary instead of
accumulating every summary. Existing SSE then updates the UI. Raw one-second
samples never enter `events.jsonl` or `run.json`.

The live chart may show summary cards and the latest short window. The durable
full timeline becomes authoritative only after artifact finalization.

## 15. Logs and resource lines

Resource integration does not make the **Log records** panel available. That
panel requires a separate `log.recorded` producer.

If bounded case logs are added, the useful lines are:

```text
12:03:01.000 phase.started       setup
12:03:01.420 sandbox.created     eos-a1
12:03:02.100 phase.started       call
12:03:02.111 operation.started   runtime.exec_command  cli
12:03:02.954 operation.finished  runtime.exec_command  exit=0  duration=843ms
12:03:03.010 resource.gap        eos-a1/sandbox  query_timeout
12:03:03.450 validation.failed   command-exits
12:03:03.520 phase.started       teardown
12:03:04.001 cleanup.finished    destroy_sandbox  passed
12:03:04.040 case.finished       failed
```

These lines should reference the resource artifact rather than repeat sample
values every second. Per-sample logging would be noisy, duplicate the NDJSON,
and quickly exhaust bounded output.

## 16. Integration points

The first release is expected to touch these boundaries.

| Area | Intended change |
| --- | --- |
| `e2e/harness/runner/resources.py` | New collector, NDJSON writer, delta/summary calculation, and recovery validation. |
| `e2e/harness/runner/cleanup.py` | Register/unregister sandbox lifecycle without changing cleanup ownership. |
| `e2e/harness/runner/reporter.py` | Bind case/phase context, finalize collector output, and attach one artifact entry to the case result. |
| `e2e/harness/runner/cli.py` | Record sanitized operation markers and structured workspace IDs; sampling bypasses this wrapper. |
| `e2e/harness/runner/direct_daemon.py` | Record sanitized operation markers and workspace-session IDs. |
| `e2e/harness/runner/daemon_http.py` | Record the same sanitized boundary metadata. |
| Export and squash raw helpers | Cover operations that intentionally bypass the shared CLI helper. |
| `e2e/harness/runner/runner.py` | Supply run/evidence context and recover interrupted `.part` files. |
| `e2e/harness/reducer/events.py` | Correct initial evidence-health semantics; no new event type for release 1. |
| `e2e/web/src/types.ts` | Type the resource artifact summary and status. |
| `e2e/web/src/App.tsx` | Add the selected-case resource panel and lazy raw-artifact loading. |
| `e2e/web/src/styles.css` | Resource cards, aligned timeline, gap, and responsive states. |
| `e2e/web/src/tests/App.test.tsx` | Verify status, scope, gap, purged, and accessibility behavior. |

No change is required in the product repository for the first release.

## 17. Delivery sequence

### Release 1A: durable collection

- add the child-local collector and sandbox lifecycle hooks;
- persist bounded raw NDJSON and summary metadata;
- publish one existing `artifact.recorded` event per executed case;
- recover partial artifacts after child interruption;
- leave the UI unchanged until the contract is proven.

### Release 1B: completed-run UI

- render resource status and summary cards;
- fetch raw data lazily;
- render scope-aware SVG timelines and operation/failure markers;
- keep Log records separate;
- fix empty evidence health from `complete` to `not_published`.

### Release 2: true midflight summaries

- move the parent to `Popen` ingestion;
- add bounded `resource.summary` journal projection;
- update live summary cards over existing SSE;
- retain the finalized artifact as the source of truth.

## 18. Verification plan

### 18.1 Collector unit tests

- parse sandbox and workspace cgroup responses;
- calculate CPU cores and counter totals;
- omit deltas on counter reset or non-increasing timestamp;
- distinguish missing from zero values;
- aggregate multiple sandbox and workspace scopes correctly;
- record query timeout, malformed response, unsupported cgroup, and disk
  truncation;
- stop at the artifact cap without a partial JSON line;
- recover a valid prefix from a torn `.part` file;
- redact or reject prohibited fields;
- generate deterministic IDs and storage paths.

### 18.2 Harness contract tests

- an immediate and final sample are attempted for every tracked sandbox;
- collector calls do not appear in execution surface counts or operation timing;
- destroyed workspace history remains attached to the owning case;
- multiple sandboxes remain isolated to the current case;
- sampling failure does not alter pass/fail/error/cancelled verdicts;
- each executed case publishes exactly one explicit resource artifact record;
- non-applicable cases do not degrade evidence health;
- summary events remain below 64 KiB;
- files remain below 4 MiB and the evidence API verifies their digest;
- child timeout produces partial or unavailable evidence, never a false
  `available` state;
- purge removes raw resources while preserving artifact identity and verdict.

### 18.3 UI tests

- available, partial, unavailable, unsupported, not-applicable, invalid, and
  purged states;
- one and multiple sandbox/workspace scopes;
- CPU/memory/I/O/disk cards use correct units and labels;
- gaps split chart lines and show reasons;
- unlimited memory and truncated disk are explicit;
- the timeline remains usable without color and with keyboard navigation;
- raw evidence is fetched lazily, not for every case on page load;
- absence of `log.recorded` still shows the current Log records message.

### 18.4 Focused live proof

Use the repository's feature-by-feature E2E workflow:

1. Run collector and reporter contract tests without Docker.
2. Run one focused runtime command case that creates a sandbox.
3. Run one focused workspace-session case and verify workspace history.
4. Run one focused failure/timeout case and verify partial evidence.
5. Reap only the focused run IDs.
6. After focused defects are resolved, run one scheduled final proof with the
   repository defaults: `max_parallel=5`, `container_weight_cap=10`, and fixed
   pressure concurrency 12.

All test commands and outcomes follow the append-only test-report rules.

## 19. Acceptance criteria

The first release is complete when:

1. Every executed case publishes one unambiguous resource status.
2. Every sandbox-owning case attempts immediate, periodic, and final sampling.
3. Completed run pages show CPU, memory, I/O, workspace disk, file count,
   coverage, phases, operation markers, and gaps when the sources provide them.
4. Raw samples are case-scoped, digest-verified, bounded, redacted, purgeable,
   and recoverable after interruption.
5. A resource collector failure never changes the functional test verdict.
6. Empty or absent evidence never appears as `complete`.
7. Missing fields, unsupported cgroups, counter resets, disk truncation, and
   sampling gaps never render as zero or success.
8. Collector sampling is excluded from product execution-surface proof and
   operation performance statistics.
9. The completed-run UI adds no chart dependency and loads raw evidence only on
   demand.
10. Existing case execution, serial scheduling, cleanup ownership, evidence
    serving, redaction, retention, and purge behavior continue to pass.

The second release is complete when a running case's bounded resource summary
updates over existing SSE without placing raw one-second samples in the journal.

## 20. Resolved design choices

- **Artifact, not log:** resource series are structured evidence; logs remain a
  separate producer.
- **Case-scoped, not only run-scoped:** debugging needs exact ownership and
  phase/operation correlation.
- **One child-local scheduler:** serial execution does not justify a collector
  thread per case or sandbox.
- **Public product reads:** the collector uses supported observability commands,
  not direct cgroup filesystem access from the harness.
- **Harness-computed sandbox deltas:** `window_ms=0` avoids repeated history but
  returns no prior sample for product-side delta calculation.
- **Final workspace harvest:** daemon history is most useful immediately before
  sandbox destruction.
- **Summary in journal, series in file:** this respects the 64 KiB event cap and
  keeps run projection/SSE small.
- **Native SVG:** the required visualization is small and does not justify a new
  chart dependency.
- **Post-run first:** current runner architecture cannot honestly claim live
  midflight case observability.
