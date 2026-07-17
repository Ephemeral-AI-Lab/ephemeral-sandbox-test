# Workspace Process Topology Live E2E Specification

Status: Draft  
Test family: `e2e/observability/cgroup`  
Product spec: [`sandbox-observability-workspace-process-topology-spec.md`](../../../../ephemeral-sandbox/docs/sandbox-observability-workspace-process-topology-spec.md)

## Purpose

Prove against a real Docker sandbox that the public observability backend reports workspace process placement from Linux `/proc` namespace identity. The suite must catch the current regression where the manager returns `topology.available: false` merely because no delegated child cgroups exist.

These are backend live tests. They do not automate the browser; product-repository browser tests own rendering behavior. They use the same public JSON contract consumed by the console.

This family becomes the single live-test owner for cgroup/process-topology behavior. The permissive topology smoke cases currently under `e2e/observability/snapshot` must be removed or narrowed to snapshot-only coverage when this family is implemented; do not retain duplicate tests that accept unavailable topology as a valid branch.

## Required invariant

For every sandbox that can create an EOS workspace and whose procfs is readable:

```text
topology.available == true
topology.source == "proc_namespaces"
```

This invariant is independent of:

- whether `/sys/fs/cgroup` is writable;
- whether the daemon owns a delegated child cgroup;
- whether all processes have membership `0::/`;
- whether the Docker host uses cgroup v1 or v2;
- which API client started the command.

The tests must fail, not skip or degrade, if topology is reported unavailable solely due to cgroup delegation.

## Live boundary

Behavior under test must use packaged public CLIs:

- `sandbox-runtime-cli` for workspace lifecycle and command execution;
- `sandbox-observability-cli` with `--operation cgroup --scope sandbox --output json` for topology;
- existing E2E provider/manager helpers for sandbox lifecycle.

Do not call daemon HTTP endpoints directly. Do not import daemon, runtime, query, telemetry, or console implementation modules into the test process.

Docker inspection or `docker exec` may be used only as an independent measurement oracle after behavior has been exercised through the public CLI. It must not mutate daemon state or replace a public assertion.

## Proposed files

```text
e2e/observability/cgroup/
├── __init__.py
├── helpers.py
├── test_contract.py
├── test_workspace_placement.py
└── test_lifecycle.py
```

Keep feature-specific polling and payload assertions in `helpers.py`; reuse shared provider, sandbox, workspace tracker, and `exec_in` fixtures from the existing live harness.

## Shared helpers

### `read_topology`

Run the packaged observability CLI with the sandbox-scoped `cgroup` operation and parse its JSON output. Assert the stable envelope before returning `payload["topology"]`:

```text
view == "cgroup"
scope == "sandbox"
series is a list
topology is an object
```

### `assert_proc_topology_available`

Assert:

- `schema_version == 2`;
- `available is True`;
- `source == "proc_namespaces"`;
- `error is None`;
- `workspaces` and `warnings` are lists;
- workspace IDs are unique and sorted;
- process PIDs within each workspace are positive, unique, and sorted.

Do not accept the legacy delegated-cgroup `groups` contract as a fallback.

### `wait_for_topology`

Poll the public CLI until a supplied predicate succeeds or a bounded deadline expires. On timeout, include:

- the last complete topology payload;
- sandbox ID and workspace IDs;
- recent command-session state already exposed by the public CLI.

Use monotonic deadlines and short bounded polling. Do not use fixed sleeps as correctness synchronization.

### `workspace_by_id`

Select a workspace by exact returned ID and fail with all observed workspace IDs if it is missing. Never rely on list position.

### `workload_processes`

Return rows whose `kind` is `process`, leaving the namespace init out of workload-count assertions.

### `measure_namespace_identity`

For a returned workspace and process PID, use non-mutating Docker inspection to stat:

- `/proc/<holder_pid>/ns/pid_for_children`;
- `/proc/<holder_pid>/ns/mnt`;
- `/proc/<process_pid>/ns/pid`;
- `/proc/<process_pid>/ns/mnt`.

Compare device and inode pairs. This is the independent oracle for the most important placement case. Use returned `holder_pid`; do not read internal daemon state files to discover the mapping.

If the process exits before measurement, poll for the still-running marker process and retry within the test deadline. A missing proc entry is not a product failure until the public topology itself violates the lifecycle expectation.

## Test isolation and cleanup

- Use the suite's shared sandbox lifecycle rather than creating an ad hoc Docker harness.
- Give each test a unique workspace prefix through `workspace_tracker`.
- Register every created workspace before starting commands.
- Track command sessions and terminate/cancel them through public APIs during teardown when the shared fixture does not already do so.
- Destroy only workspaces created by the current test.
- Do not use broad Docker cleanup commands, name-prefix sweeps, or removal of unrelated containers.
- Keep commands bounded by fixture/test timeouts even when the happy path uses a long-running process.

## Test cases

### 1. `observability.cgroup.proc-contract`

File: `test_contract.py`

Steps:

1. Start or reuse the family sandbox through the shared fixture.
2. Query the public cgroup operation with no family-owned workspace required.
3. Apply `assert_proc_topology_available`.
4. Record cgroup mount mode and daemon membership as diagnostic measurements when available.

Assertions:

- schema version 2 is returned;
- topology is available even when the workspace list is empty;
- source is proc namespaces;
- no response text claims that delegated child cgroups are required;
- `series` remains present and manager-owned.

This test is the minimal regression gate for the manager's previous synthetic unavailable response.

### 2. `observability.cgroup.workspace-idle`

File: `test_workspace_placement.py`

Steps:

1. Create two workspaces through `sandbox-runtime-cli`.
2. Do not start workload commands.
3. Poll topology until both exact workspace IDs appear.

Assertions for each workspace:

- state is `idle`;
- holder PID is positive;
- PID and mount namespace diagnostics are non-empty;
- there is no row with `kind: "process"`;
- a returned namespace-init row, if present, has namespace PID 1.

Cross-workspace assertions:

- holder PIDs are distinct;
- namespace identity pairs are distinct;
- an idle workspace is not represented as unavailable or omitted.

### 3. `observability.cgroup.workspace-process-placement`

File: `test_workspace_placement.py`

Steps:

1. Create workspace A and workspace B.
2. Start one long-running command session in each through the public runtime CLI.
3. Use commands available in the packaged test image and keep them alive long enough for polling; do not rely on Python or image-specific package installation.
4. Poll until each workspace reports at least one workload process.
5. Measure namespace identity for one stable workload PID from each workspace.

Assertions:

- both workspaces are `active`;
- their process PID sets are disjoint;
- each process's PID and mount namespace stat keys match its reported holder;
- each process's namespace key does not match the other workspace holder;
- `namespace_pid` is positive;
- `name` and `state` are bounded non-empty strings;
- `cgroup_memberships` is a list and may be empty, a v2 root line, or multiple v1 lines.

The test must not require a different cgroup membership for A and B.

### 4. `observability.cgroup.backend-originated-command`

File: `test_workspace_placement.py`

Purpose: cover commands started outside the browser, which previously appeared as terminal sessions the UI could not reconcile.

Steps:

1. Create one workspace.
2. Start a long-running command exclusively through the runtime CLI used by the E2E driver.
3. Query topology through the observability CLI.

Assertions:

- the workspace changes from `idle` to `active`;
- at least one non-init process appears without any browser interaction;
- returned placement passes the namespace identity oracle;
- repeated refreshes retain the process while the command is running.

### 5. `observability.cgroup.forked-descendants`

File: `test_workspace_placement.py`

Steps:

1. Create a workspace.
2. Start a shell command that keeps both a shell parent and a sleeping child alive using only POSIX shell facilities provided by the packaged image.
3. Poll until at least two workload processes are present simultaneously.

Assertions:

- both rows are assigned to the same workspace;
- both PID and mount namespace identities match the holder;
- at least one returned `parent_pid` refers to another returned process or the namespace init;
- no row appears under another open workspace.

This proves placement follows namespace membership rather than only the directly launched runner PID.

### 6. `observability.cgroup.process-exit`

File: `test_lifecycle.py`

Steps:

1. Create a workspace and start a bounded command that remains alive long enough to observe.
2. Capture its workload PID set from topology.
3. Let the command complete or cancel it through the public runtime CLI.
4. Poll topology until the captured workload PIDs disappear.

Assertions:

- topology stays available throughout;
- exited PIDs are not cached or returned as stale rows;
- the workspace eventually returns to `idle` when no other workload is active;
- proc race warnings, if any, remain bounded and do not change top-level availability.

### 7. `observability.cgroup.workspace-destroy`

File: `test_lifecycle.py`

Steps:

1. Create workspace A and workspace B.
2. Confirm both appear.
3. Destroy A through the public runtime CLI while leaving B open.
4. Poll topology.

Assertions:

- A disappears completely;
- B remains with the same holder PID and namespace diagnostics;
- no process formerly assigned to A is reassigned to B;
- topology remains available.

### 8. `observability.cgroup.concurrent-churn`

File: `test_lifecycle.py`

Steps:

1. Create two workspaces.
2. Run a bounded set of short commands in both while repeatedly querying topology.
3. Finish with one stable long-running command in each and wait for a settled snapshot.

Assertions for every successful snapshot:

- schema and ordering invariants hold;
- no PID appears in two workspaces;
- no unknown workspace ID appears;
- warnings remain within the response cap;
- `available` never becomes false because a process exited during enumeration.

Final assertions:

- both stable processes are correctly assigned and pass namespace measurement;
- no command/session or workspace leak remains after teardown.

Keep the churn count modest; this is a race detector, not a load benchmark.

### 9. `observability.cgroup.read-only-independent`

File: `test_contract.py`

This case validates behavior without assuming a particular host mount mode.

Steps:

1. Measure whether `/sys/fs/cgroup` is writable and capture the daemon's raw `/proc/self/cgroup` lines.
2. Create a workspace and start a stable workload.
3. Query topology and validate placement.

Assertions:

- topology is available in either mount mode;
- root or shared membership does not suppress the workspace or process;
- raw membership returned for the process, when readable, agrees with `/proc/<pid>/cgroup` measurement;
- the response does not claim that no delegated child cgroups means no topology.

On Docker Desktop and other read-only configurations this is a direct regression test. On writable Linux configurations it still proves the public contract without fabricating or mutating host cgroups.

### 10. `observability.cgroup.workspace-resource-estimates`

File: `test_workspace_placement.py`

Steps:

1. Create a workspace and start a stable CPU-active POSIX shell command through the public runtime CLI.
2. Poll until a workload row has non-null `resident_memory_bytes`, `cpu_time_us`, and `start_time_ticks`.
3. Poll a second public sample until CPU time increases for the same `(pid, start_time_ticks)`.
4. Read that process's `status` and `stat` through read-only Docker inspection.

Assertions:

- RSS is positive in both the public row and procfs measurement;
- cumulative CPU time increases while the workload runs;
- returned start time exactly matches procfs, preventing PID-reuse joins;
- returned CPU time is no greater than the later independent measurement;
- the resource fields do not change namespace placement or cgroup-independence assertions.

The live case validates collector inputs. Product console tests own refresh-interval percentage calculation, partial metrics, and the explicit estimate labels and limitations.

## Contract edge cases left to product tests

The following require deterministic fault injection and belong in the product repository rather than a privileged live harness:

- unreadable procfs root;
- holder disappearing between runtime snapshot and namespace stat;
- malformed or oversized proc files;
- cgroup v1 fixture parsing when the live host uses v2, and vice versa;
- exact 512-process truncation boundary;
- warning-cap enforcement;
- namespace stat collision/mismatch fixtures.

The live suite verifies the real happy path and natural process churn; it must not remount procfs/cgroupfs or grant additional privileges to simulate these cases.

## Environment matrix

At minimum, run the family on the existing default Docker live environment. CI coverage should include, when runners are available:

| Environment | Required result |
| --- | --- |
| Linux Docker Engine, cgroup v2 | Full suite passes |
| Docker Desktop Linux containers | Full suite passes |
| Linux Docker Engine, cgroup v1 | Full suite passes; membership may have multiple lines |
| Read-only cgroup mount | Full suite passes without delegation |
| Native Windows containers | Out of scope |

The implementation must not add a privileged-container or `SYS_ADMIN` requirement solely to make these tests pass.

## Timeouts and polling

- Use the shared suite timeout constants where available.
- Workspace/topology convergence polls should normally complete within 10 seconds and have a hard deadline no greater than 30 seconds.
- Long-running command bodies may request a longer duration, but test teardown must cancel them promptly.
- Each CLI invocation needs its own timeout so one stuck command cannot consume the whole test deadline.
- Timeout messages must include the last payload rather than a generic predicate failure.

## Artifacts and failure reporting

Follow the repository live-E2E rules:

- append the planned run entry to `e2e/test-report.md` before executing live tests;
- store full logs under `e2e/.artifacts/<timestamp>-observability-cgroup/`;
- keep the report entry concise and bounded;
- inspect only failing case output plus a small context window;
- record exact case IDs, pass/fail state, duration, and artifact directory.

For a placement failure, the bounded diagnostic bundle should include:

- last public topology JSON;
- public workspace/session state;
- stat/readlink measurement for the relevant holder and process namespace handles;
- raw measured `/proc/<pid>/cgroup` lines;
- no environment variables or command arguments.

## Implementation order

Implement and run the family feature by feature:

1. helpers plus `proc-contract`;
2. `workspace-idle`;
3. `workspace-process-placement` and its independent oracle;
4. backend-originated and forked-descendant cases;
5. process-exit and workspace-destroy lifecycle cases;
6. read-only-independent and bounded churn cases;
7. workspace resource estimate inputs.

After each feature, run only the smallest relevant test selection, inspect its artifacts, fix failures, and then continue. Run the complete `e2e/observability/cgroup` family once all individual cases pass.

## Admission criteria

The backend/UI feature is ready to merge when:

- every required case above is implemented with the named stable case ID;
- no assertion conditionally accepts `available: false` due to cgroup delegation;
- at least one live placement is independently verified by PID and mount namespace stat identity;
- command execution is exercised through the packaged public runtime CLI;
- topology is exercised through the packaged public observability CLI;
- stable live processes expose positive RSS, advancing CPU time, and a procfs-verified start identity;
- cleanup is run-scoped and leaves no family workspace or command-session leak;
- the complete family passes in the default live Docker environment;
- the live report links the bounded artifact directory for the final run.
