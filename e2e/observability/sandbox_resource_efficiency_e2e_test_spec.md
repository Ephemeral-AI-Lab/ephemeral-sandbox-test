# Sandbox Resource-Efficiency Live E2E Test Specification

Status: Draft; cases in this document are not yet implemented

Date: 2026-07-18

Required root:
`/Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test/e2e/observability`

Product contract:
[`sandbox_resource_efficiency_spec.md`](sandbox_resource_efficiency_spec.md)

## 1. Purpose

This suite provides packaged-daemon, real-Docker proof for the permanent fixes
to stale workspace holders, retained workspace resources, oversized daemon
runtimes, manager/daemon polling coupling, topology request cost, missing
resource limits, and insufficient incident evidence.

The suite must catch the motivating failure shape:

- a namespace holder exits unexpectedly;
- the holder becomes or remains a zombie;
- the workspace is still reported active;
- namespace descriptors or layer leases remain held;
- daemon anonymous memory remains materially above its settled baseline; and
- polling repeatedly wakes the daemon after useful work has ended.

The suite does not merely assert that a sandbox remains reachable. It proves
cleanup, memory cooldown, file-descriptor and lease balance, daemon quiescence,
thread budgets, admission behavior, resource containment, and bounded evidence.

## 2. Ownership and overlap

This is a new focused family. It composes with, rather than duplicates:

- [`resource_isolation/`](resource_isolation/), which owns long observability
  memory/disk and workload-GC qualification;
- [`cgroup/`](cgroup/), which owns normal process-topology placement and churn;
- [`test_spec.md`](test_spec.md), which owns resource-isolation release gates;
  and
- [`cgroup/test_spec.md`](cgroup/test_spec.md), which owns the schema-v2
  topology contract.

Existing cases remain authoritative for their contracts. For example, this
family proves that an unexpectedly dead holder is cleaned; it does not replace
the existing normal process-exit and normal workspace-destroy cases.

Exact internal invocation counts and Console timer behavior are not live E2E
assertions. Counting-fake manager integration tests and fake-timer Console
tests own those exact claims. Live cases prove their external consequences:
daemon CPU, I/O, memory, event storage, and response availability.

## 3. Required implementation layout

Implement new code below:

```text
e2e/observability/resource_efficiency/
  __init__.py
  conftest.py
  helpers.py
  test_smoke.py
  test_holder_lifecycle.py
  test_workspace_reclaim.py
  test_manager_routing.py
  test_runtime_budget.py
  test_resource_profiles.py
  test_diagnostics.py
  test_soak.py
```

Reuse bounded measurement and artifact primitives from
`observability.resource_isolation.helpers` and public topology/workspace
helpers from `observability.cgroup.helpers`. Do not copy the proc sampler,
streaming statistics, artifact cap, workspace tracker, or public CLI wrappers
into a second implementation.

If a shared helper needs an additive field such as thread count or FD count,
extend it without weakening existing callers and add a non-Docker helper unit
test.

## 4. Live boundary

Every live case must:

- create and destroy sandboxes through the shared manager lifecycle fixtures;
- run the packaged Linux daemon installed by the normal Docker sandbox flow;
- invoke product behavior through public `sandbox-manager-cli`,
  `sandbox-runtime-cli`, and `sandbox-observability-cli` routes;
- use exact sandbox and workspace IDs created by the current case;
- declare a stable `@e2e_test` ID, features, validation checkpoints,
  execution surface, marker, and realistic timeout;
- register each sandbox, workspace, and command immediately after creation;
- preserve bounded evidence on pass and failure;
- use monotonic deadlines and state predicates instead of correctness sleeps;
  and
- remove only run-owned resources.

Docker and `/proc` are independent measurement and controlled fault channels.
They must not replace public behavior assertions. No case may scrape daemon or
gateway logs for a verdict, call a private daemon endpoint, alter checked-in
production config, run Docker prune, delete containers by prefix, or send a
signal to a PID that has not passed the exact identity checks in Section 6.

## 5. Entry prerequisites

The following product behavior must exist before the corresponding live case
can pass:

1. holder supervision and idempotent unexpected-exit teardown;
2. public workspace snapshot and daemon-self fields sufficient to observe
   holder, FD, lease, thread, and cleanup counts;
3. a manager-only single-sandbox resource operation;
4. a manager-only fleet current-usage operation;
5. explicit topology access retained through the public observability CLI;
6. generated config support for worker, blocking, connection, command, trigger,
   and resource-profile settings; and
7. run-scoped diagnostic identity exposed in a bounded public response.

The suite must fail clearly for a missing required surface. It must not silently
fall back to the combined polling path and then claim resource isolation.

## 6. Deterministic holder fault injection

Unexpected holder exit needs a real process fault. Implement one shared helper,
`kill_workspace_holder`, with this exact safety protocol:

1. Read topology through the public CLI and select the workspace by exact ID.
2. Capture the returned holder PID.
3. Inside that case's exact container, read bounded `/proc/<pid>/stat`,
   `/proc/<pid>/status`, `/proc/<pid>/exe`, and `/proc/<pid>/cmdline` evidence.
4. Verify the process is the namespace-holder mode of the packaged daemon,
   record its parent PID and start-time field, and discard or redact raw
   command-line text after verification.
5. Immediately before signaling, re-read the start time and executable. Abort
   without sending a signal if either identity changed or the process vanished.
6. Send exactly one `SIGKILL` to that PID inside that container.
7. Record the monotonic signal time, identity digest, and result in
   `holder-fault.json`.
8. Observe recovery through public routes. `/proc` is used only to prove the
   exact child was reaped and never became a persistent zombie.

This is a deterministic, exact-target fault helper. A timed best-effort kill,
process-name kill, host PID guess, namespace-wide signal, or broad container
restart is forbidden.

For the destroy race, two threads are released from a barrier: one invokes the
public destroy operation and one executes the already-validated exact fault.
The test records which side won. It never retries a signal against the old PID.

## 7. Measurement contract

### 7.1 Daemon sample

Extend the existing one-second resource-isolation sample with:

- `/proc/<daemon-pid>/status`: `Threads`, `FDSize`, and voluntary/nonvoluntary
  context-switch counters;
- actual open FD count from `/proc/<daemon-pid>/fd`;
- direct-child process counts by state, including `Z`;
- daemon-self public fields for live holders, exited-unreaped holders,
  workspaces, namespace/control FDs, active layer leases, async tasks, blocking
  tasks, and configured pool limits;
- last holder-exit and cleanup counters; and
- resource-profile/cgroup paths and declared limits.

Retain the existing fields for `smaps_rollup`, daemon CPU ticks and I/O,
`memory.current`, `memory.stat`, event-store files, and manager resource rings.
Unavailable required fields are explicit and fail nightly/release cases on a
supported Linux runner.

### 7.2 Workspace-cycle record

Write one compact JSON line per cycle containing:

- cycle and repetition number;
- sandbox and workspace IDs;
- holder PID and identity digest;
- create, first-command, destroy, and settled monotonic timestamps;
- terminal public lifecycle state;
- holder, zombie, FD, lease, command, scratch, and persisted-handle deltas;
- daemon anonymous memory, RSS, threads, and CPU ticks after cooldown; and
- cleanup error or bounded response digest.

The test process may retain only online counters, extrema, fixed histograms,
and a deterministic bounded reservoir. It must never retain all cycle records.

### 7.3 Route traffic record

For resource and fleet campaigns, record only:

- route name and request count;
- success/error counts;
- fixed latency histogram;
- response-size extrema;
- stable response digest samples; and
- target/control daemon counter deltas.

Do not write 10,000 full response bodies.

### 7.4 Required artifacts

Each case writes below
`.e2e-state/observability/<run-id>/<stable-case-id>/`:

```text
environment.json
samples.jsonl
summary.json
cleanup.json
```

Cases add only their relevant bounded artifacts:

```text
holder-fault.json
workspace-cycles.jsonl
route-traffic.json
profile.json
diagnostic-fingerprint.json
```

The uncompressed case artifact cap is 32 MiB. The writer reserves space for
`summary.json` and `cleanup.json`, stops optional sampling before the cap, and
never loses the final verdict or cleanup evidence.

## 8. Stable case catalog

| Case | `@e2e_test` ID | Tier/marker | Timeout | Required validation checkpoints |
| --- | --- | --- | ---: | --- |
| RE-00 | `observability.resource-efficiency.smoke` | `smoke` | 10 min | `workspace-overhead-coarse`, `resource-route-quiescent`, `cleanup-coarse`, `artifact-bounded` |
| RE-01 | `observability.resource-efficiency.holder-exit` | `nightly` | 20 min | `holder-fault-detected`, `holder-reaped`, `workspace-cleaned`, `peer-survives`, `fault-artifact-bounded` |
| RE-02 | `observability.resource-efficiency.holder-destroy-race` | `nightly` | 30 min | `exit-destroy-idempotent`, `single-cleanup-result`, `resource-counts-balanced`, `race-artifact-bounded` |
| RE-03 | `observability.resource-efficiency.workspace-cycle-reclaim` | `nightly` | 3 h | `lifecycle-memory-plateau`, `fd-thread-plateau`, `lease-session-zero`, `cycle-artifact-bounded` |
| RE-04 | `observability.resource-efficiency.manager-resource-quiescence` | `nightly` | 3 h | `resource-series-available`, `daemon-quiescent`, `store-read-pure`, `post-poll-cooldown` |
| RE-05 | `observability.resource-efficiency.topology-cost` | `nightly` | 60 min | `empty-topology-bounded`, `idle-topology-bounded`, `topology-correct`, `topology-cooldown` |
| RE-06 | `observability.resource-efficiency.runtime-thread-budget` | `nightly observability_config` | 90 min | `idle-thread-envelope`, `pressure-thread-envelope`, `concurrency-functional`, `cooldown-reclaimed`, `config-restored` |
| RE-07 | `observability.resource-efficiency.admission-pressure` | `release observability_config` | 90 min | `admission-bounded`, `structured-overload`, `control-plane-responsive`, `post-pressure-clean`, `config-restored` |
| RE-08 | `observability.resource-efficiency.fleet-scaling` | `release` | 4 h | `fleet-batch-complete`, `all-daemons-quiescent`, `manager-scaling-bounded`, `fleet-cleanup-complete` |
| RE-09 | `observability.resource-efficiency.resource-profile` | `release observability_config` | 2 h | `profile-applied`, `workload-contained`, `daemon-control-survives`, `profile-cleanup-complete`, `config-restored` |
| RE-10 | `observability.resource-efficiency.triggered-diagnostic` | `release observability_config` | 45 min | `trigger-fires-once`, `bundle-bounded`, `bundle-attributable`, `cooldown-no-repeat`, `config-restored` |
| RE-11 | `observability.resource-efficiency.lifecycle-soak` | `release` | 8 h | `soak-no-memory-trend`, `soak-no-resource-leaks`, `polling-remains-quiescent`, `soak-cleanup-complete` |

All cases use `execution_surface="cli"`. Configuration-owning cases are serial,
own the gateway through the existing generated-config fixture, destroy their
sandboxes before restoration, and restore the baseline gateway even after
failure.

## 9. Case definitions

### RE-00 — focused smoke

Create one standard-profile sandbox and verify the packaged daemon identity.
Warm it for one minute, establish a settled daemon sample, then:

1. create one workspace and leave it idle for one minute;
2. issue only manager-owned resource reads at one request per second for two
   minutes;
3. destroy the workspace through the public runtime CLI; and
4. wait for a two-minute cooldown.

Pass conditions:

- an idle workspace adds no workload process and no unbounded thread count;
- resource responses remain structured and contain manager-owned series;
- daemon event-store fingerprints do not change during resource reads;
- no zombie direct child is observed;
- holder, workspace, namespace/control FD, and lease counts return to baseline;
- daemon `Anonymous` is no more than 1 MiB above the pre-workspace settled
  sample after cooldown;
- `AnonHugePages` and cgroup `anon_thp` remain zero; and
- all artifacts remain within the case cap.

This is a gross regression gate, not release memory qualification.

### RE-01 — unexpected holder exit

Run three nightly repetitions. In each repetition:

1. create workspace A and peer workspace B;
2. start one stable public command in each and prove normal topology placement;
3. record baseline self counts and the exact holder identity for A;
4. inject one exact `SIGKILL` into A's holder using Section 6;
5. continuously sample child state while polling public workspace state;
6. attempt a new command against A after the exit is detected;
7. prove B's command and namespace identity remain healthy; and
8. destroy or join cleanup only through the public lifecycle.

Each repetition passes only when:

- holder death is reflected in public state or a structured operation error
  within one second;
- the holder is waited and no zombie remains after one second;
- A accepts no new work after death;
- A leaves the active workspace snapshot after cleanup;
- holder, exited-unreaped, namespace/control FD, active-layer-lease, command,
  and persisted-handle counts return to their pre-A baselines;
- exactly one holder-exit counter increment and one terminal cleanup result are
  observable;
- B's command remains running and its holder identity is unchanged;
- the daemon and sandbox remain ready; and
- after cooldown, daemon anonymous memory is within 128 KiB of its settled
  pre-A median.

If cleanup cannot safely finalize a publish-required workspace, the expected
result is a structured `finalization_failed` recovery state and bounded
recovery artifact, not silent success. Use `no_op` for the default RE-01 arm
and add one publish-required arm once that public policy is available.

### RE-02 — holder exit racing explicit destroy

Run 20 bounded race iterations in one sandbox, alternating barrier launch order
to avoid always favoring the same side.

Allowed terminal outcomes are:

| Winner | Fault helper result | Public destroy result | Required final state |
| --- | --- | --- | --- |
| Exit | exact signal sent | success or already closing/closed | one cleanup, workspace absent |
| Destroy | target already exited after validated identity | success | one cleanup, workspace absent |
| Concurrent | exact signal sent | success or cleanup in progress | one cleanup, workspace absent |

No outcome may contain a generic timeout, double-release error, lease
underflow, unrelated PID signal, peer-workspace change, or retained zombie.

After every iteration, public self counts must return to the iteration baseline
before the next workspace is created. The final daemon FD, thread, lease,
holder, command, and persisted-handle counts must equal the initial baseline.

Deterministic internal interleavings still belong in product unit tests. This
live case proves the packaged process and public response tolerate natural
scheduler interleavings.

### RE-03 — repeated workspace lifecycle reclaim

Run at least 1,000 sequential cycles against one packaged daemon:

1. create a workspace;
2. run one bounded command that allocates and frees a small fixed buffer;
3. await command completion;
4. destroy the workspace;
5. wait for public holder/workspace/lease counts to return to baseline; and
6. sample after every tenth cycle and throughout a ten-minute final cooldown.

Every hundredth cycle also starts and stops a long-running command so command
session cleanup is covered. Fault injection is not used here.

Pass conditions:

- all 1,000 cycles complete with no untracked session;
- no direct-child zombie is observed;
- final holder, workspace, command, lease, persisted-handle, and FD counts
  equal baseline;
- idle thread count returns to its declared envelope;
- final-ten-minute daemon `Anonymous` median is within 128 KiB of the settled
  pre-cycle median;
- post-warmup `Anonymous` Theil-Sen slope is at most 4 KiB/hour;
- no anonymous huge pages appear; and
- `workspace-cycles.jsonl` remains bounded and parseable.

The test fails on the first correctness leak but retains the cycle record and a
bounded final sample window.

### RE-04 — manager resource traffic leaves daemons quiescent

Use paired target and untouched control sandboxes for three repetitions. Warm
both equally. Against the target, issue 10,000 public manager-only
single-sandbox resource reads over thirty minutes. Do not request topology,
snapshot, events, or traces during the measurement phase.

For each repetition:

- every resource response contains bounded host series even if the daemon is
  deliberately made unreachable in a separate short subcase;
- target and control daemon event-store fingerprints are unchanged;
- target-minus-control daemon CPU is below one scheduler tick per minute;
- target daemon storage I/O does not advance because of manager resource reads;
- target-minus-control anonymous-memory growth is at most 64 KiB;
- after ten-minute cooldown, target `Anonymous` is within 128 KiB of its
  pre-poll median; and
- the manager resource ring remains fixed and at most 64 KiB.

The product manager integration test separately proves exactly zero daemon
client invocations. This case must not infer an exact call count from noisy CPU
ticks.

### RE-05 — explicit topology has bounded cost

Measure three phases in one sandbox:

1. no open workspaces;
2. one valid idle workspace; and
3. one active bounded command followed by idle cooldown.

Call explicit topology at the production visible-page cadence for ten minutes
per phase. Apply the full schema and namespace-placement assertions from the
existing cgroup helpers.

The case timeout includes the ten-minute authenticated no-op baseline, all
three ten-minute topology phases, the five-minute cooldown, and fifteen minutes
of bounded setup, assertion, command-stop, workspace-destroy, and artifact
finalization headroom. Measurement and cooldown durations may not be shortened
to fit the timeout.

Pass conditions:

- empty topology remains available and complete;
- empty-topology daemon CPU above a matched authenticated no-op request
  baseline is at most one scheduler tick per minute;
- an idle valid workspace adds at most 0.5% of one core at the two-second
  cadence;
- the active command is assigned correctly and disappears after completion;
- no response retains stale PIDs or exceeds row/warning limits;
- event storage is unchanged;
- no anonymous-memory trend appears; and
- after the request phase, memory and thread counts return to baseline.

The exact claim that an empty reverse index performs zero numeric `/proc`
enumeration belongs to an instrumented product unit test. This live case proves
the packaged consequence without inspecting implementation calls.

### RE-06 — runtime thread budget

Own a generated gateway configuration with:

```yaml
daemon:
  server:
    worker_threads: 2
    max_blocking_threads: 8
    blocking_thread_keep_alive_s: 5
    max_concurrent_connections: 64
runtime:
  command:
    max_active: 32
```

Create one fresh sandbox after the config is active. Record five minutes idle,
then run 32 bounded public command sessions with a barrier so useful
concurrency overlaps. Complete all sessions and record ten minutes cooldown.

Pass conditions:

- public self config reports the exact configured values;
- settled idle daemon threads are at most `worker_threads + 4`;
- pressure threads never exceed
  `worker_threads + max_blocking_threads + 6`;
- all admitted commands complete correctly without deadlock;
- the daemon stays responsive to public snapshot, interrupt, and destroy;
- after keepalive plus cooldown, threads return to the idle envelope;
- post-cooldown `Anonymous` is within 128 KiB of pre-pressure median; and
- the baseline gateway is restored.

If platform support requires a different fixed infrastructure-thread
allowance, it must be exposed by the daemon build metadata and qualified once;
the test must not learn a larger allowance from the observed peak.

### RE-07 — admission pressure is bounded

Own a generated low-capacity configuration:

```yaml
daemon:
  server:
    worker_threads: 2
    max_blocking_threads: 4
    max_concurrent_connections: 8
runtime:
  command:
    max_active: 4
```

Release 12 command attempts concurrently. Pressure concurrency is fixed at 12;
there is no reduced pressure lane.

Pass conditions:

- no more than four command executions are active simultaneously;
- excess attempts receive the documented structured admission result rather
  than hanging or disconnecting;
- queued request and task counts remain within declared bounds;
- a public status request and interrupt for each admitted command succeeds
  during pressure;
- daemon threads stay within the configured envelope;
- all commands and workspaces are cleaned;
- memory returns within 128 KiB of baseline after cooldown; and
- baseline gateway config is restored.

The exact distribution of admitted callers is not asserted. Boundedness,
structured outcomes, responsiveness, and cleanup are asserted.

### RE-08 — fleet resource scaling

Create 20 standard-profile sandboxes and register each immediately. Warm all
of them, then issue one public fleet current-usage request every two seconds for
thirty minutes. The response must cover all 20 ready sandbox IDs.

Use round-robin out-of-band daemon sampling so observer processes do not run in
all containers simultaneously. Also measure the exact run-owned manager
process and resource-ring directory.

Pass conditions:

- the client issues one fleet request per cadence, independent of sandbox
  count;
- every response contains exactly one current record per ready run-owned
  sandbox and no unrelated record;
- every sandbox daemon's CPU, storage-I/O, event-store, and anonymous-memory
  deltas satisfy the untouched control bound;
- manager anonymous memory is bounded by fixed per-sandbox ring/index state and
  has no trend with poll count;
- response p99 and manager CPU are reported against sandbox count and pass the
  established release baseline;
- one ring per sandbox remains at most 64 KiB; and
- destroying the 20 sandboxes removes only their rings.

The existing 100-sandbox ring-lifecycle case remains the authoritative disk
scale proof. RE-08 owns request fanout and daemon quiescence.

### RE-09 — resource profile containment

This case is required on the release Linux cgroup-v2 runner. It may explicitly
skip on a developer Docker Desktop host only when the environment evidence
shows the missing delegation primitive.

Create a sandbox with a small test-owned profile. Verify its CPU, memory, and
PID settings through public manager metadata and independent Docker/cgroup
measurement. Run three bounded workload-leaf subcases through the public
runtime CLI:

1. CPU pressure above the configured quota;
2. a deterministic memory allocator above the workload leaf maximum; and
3. controlled process creation above `pids.max`.

The workload fixture must be bounded, copied into the test workspace as a
run-owned fixture when necessary, and incapable of escaping its cgroup. Do not
fill host memory, fork without a fixed maximum, or OOM the outer container.

Pass conditions:

- measured limits equal the selected profile;
- workload CPU is throttled at the declared quota;
- memory pressure terminates or rejects only the workload leaf according to
  contract;
- process creation stops at the PID limit with a structured workload result;
- the daemon remains alive and can report status, interrupt survivors, clean
  the workspace, and destroy the sandbox;
- no unrelated sandbox is affected; and
- all run-owned cgroups and artifacts disappear during cleanup.

Outer-container-only platforms run the non-destructive limit-verification arm
but do not claim workload/daemon isolation qualification.

### RE-10 — triggered diagnostic is bounded and attributable

Use generated config with a safe test threshold and short cooldown. Warm one
sandbox, then produce sustained daemon work through explicit public topology
requests against a known active workspace. Do not use a workload CPU spike as
the trigger because workload CPU is not daemon CPU.

Pass conditions:

- public daemon-self state reports exactly one trigger and a diagnostic ID;
- a bounded diagnostic fingerprint is available without parsing daemon logs;
- the diagnostic identifies topology/RPC work, active task/queue counts,
  thread counts, holder/workspace IDs, CPU interval, and memory counters;
- the bundle is at most 1 MiB and contains no workspace file content,
  environment variables, auth token, or full command line;
- sustained threshold crossing during the configured cooldown creates no
  second bundle;
- after work stops, the daemon returns below threshold and creates no idle
  bundle; and
- baseline config is restored.

The product unit test owns exact threshold-window arithmetic. This live case
owns packaged capture, redaction, size, attribution, and cooldown behavior.

### RE-11 — six-hour lifecycle and polling soak

Run one standard-profile sandbox for six hours. Two bounded drivers operate:

- a lifecycle driver repeatedly creates a workspace, runs one short command,
  destroys it, and waits for baseline holder/lease counts before continuing;
  and
- a resource driver issues manager-only resource reads every two seconds.

Issue explicit topology only while the current command is active and once to
confirm the workspace is gone. Sample the daemon every five seconds and stream
all evidence. Complete at least 1,000 lifecycle cycles.

Release gates:

- zero zombie observations;
- zero failed or retained workspace cleanups;
- holder, FD, lease, command, scratch, and persisted-handle counts show no
  upward trend and equal baseline at the end;
- daemon `Anonymous` slope is at most 4 KiB/hour;
- final-ten-minute `Anonymous` median is within 128 KiB of the initial settled
  median;
- idle threads always return to the declared envelope;
- manager-only polling causes no daemon store mutation or residual CPU work;
- no anonymous huge pages appear; and
- final public destroy and run-scoped cleanup succeed.

This case is never automatically rerun after failure. Investigate its bounded
artifacts and rerun only the failed focused case before another release soak.

## 10. Companion non-live coverage

The live cases are necessary but cannot efficiently force every interleaving
or count internal calls. The feature is incomplete without the following
product tests.

### 10.1 Workspace and process unit tests

- `Child` normal exit, signal exit, and wait failure;
- duplicate exit notifications reap once;
- explicit destroy before, during, and after exit notification;
- every teardown step fails once, then retries without double release;
- holder PID/start-time mismatch refuses to signal;
- no-op and publish-required finalization after unexpected exit;
- peer workspace isolation; and
- startup reconciliation of a persisted handle after daemon crash.

### 10.2 Manager integration tests

- 10,000 resource-only reads: zero daemon client calls;
- 10,000 fleet reads: zero daemon client calls;
- one topology read: exactly one daemon call;
- unavailable daemon: resource series still returned;
- one fleet call reads N manager rings without N manager RPCs; and
- combined legacy route remains compatible for the migration window.

### 10.3 Topology unit tests

- no workspaces: zero `read_dir(/proc)` calls;
- all holders invalid: zero numeric proc enumeration and all workspaces
  `partial`;
- one valid holder: one numeric proc enumeration;
- mixed valid/invalid holders: one enumeration and correct partial state;
- row, warning, and bounded-read caps unchanged; and
- PID disappearance races remain nonfatal.

### 10.4 Config/runtime tests

- standard and build profile defaults;
- deprecated `max_worker_threads` alias;
- both old and new name rejected together;
- worker, blocking, connection, and command lower/upper bounds;
- bounded queue in front of `spawn_blocking`; and
- structured overload responses for connection and command admission.

### 10.5 Console tests

- Resources calls only the manager resource operation;
- dashboard performs one fleet batch call, not N sandbox calls;
- Processes alone requests explicit topology;
- stable idle topology stops daemon requests;
- timestamp and resource-counter changes do not count as activity;
- manager activity revision and user interaction resume exactly one request;
- hidden tab stops polling; and
- focus performs a manager revision check before topology.

## 11. Requirement traceability

| Product requirement | Primary live proof | Required companion proof |
| --- | --- | --- |
| WL1–WL4 | RE-01 | holder supervisor unit tests |
| WL5 | RE-02 | deterministic teardown interleavings |
| WL6 | RE-01, RE-03, RE-11 | teardown step unit tests |
| WL7 | RE-01 fault safety | PID mismatch unit test |
| WL8 | RE-01 publish-required arm | finalize policy tests |
| WL9 | RE-01 peer workspace | operation service tests |
| WL10 | RE-01, RE-10 | event/counter serialization tests |
| DR1–DR3 | RE-06 | config and Tokio builder tests |
| DR4 | RE-07 | admission integration tests |
| DR5–DR6 | RE-03, RE-06, RE-11 | runtime ownership tests |
| OR1–OR3 | RE-04, RE-08 | counting-fake manager tests |
| OR4–OR6 | RE-05 | Console fake-timer tests |
| OR7–OR8 | RE-05 | instrumented topology tests |
| OR9–OR10 | RE-04 | manager compatibility tests |
| RP1–RP3 | RE-09 | profile/config/cgroup tests |
| RP4 | RE-00 through RE-11 | self-payload serialization tests |
| RP5–RP6 | RE-10 | trigger-window and redaction tests |
| Anonymous-memory isolation | RE-03, RE-04, RE-11 | existing RI-01 through RI-04 |
| GC isolation | Existing GC-01 | allocator/runtime benchmarks |

No requirement may be marked complete solely from a different layer's test.
For example, a counting fake proves zero calls but not packaged-daemon memory;
a live CPU trace proves quiescence but not exact invocation count.

## 12. Failure interpretation

Classify every failure before changing a threshold:

| Evidence | Classification |
| --- | --- |
| Holder remains `Z` after deadline | Product lifecycle correctness defect |
| Workspace active after holder death | Product session-state defect |
| Lease/FD count fails to return | Product teardown resource leak |
| Anonymous memory trends with stable counts | Retained allocation or allocator-residence defect; inspect self metrics |
| Resource-only poll advances daemon CPU/I/O | Manager routing defect |
| Fleet client emits N requests | Console/client batching defect |
| Empty topology shows material CPU but unit fast path passes | RPC/serialization/runtime overhead; profile before threshold change |
| Thread count exceeds configured envelope | Runtime builder or blocking-admission defect |
| `WouldBlock`/`EAGAIN` during host load | Recheck request timeout and exact failed case before product change |
| Foreign Docker creation during memory phase | Invalid measurement environment; do not call product pass/fail |
| Required cgroup delegation absent on release runner | Runner capability failure, not an allowed skip |

Never make a failing release soak pass by widening its memory, zombie, FD,
lease, or cleanup bounds.

## 13. Cleanup rules

- Register a sandbox immediately after create returns its ID.
- Register a workspace before starting its first command.
- On failure, attempt public command interrupt and workspace destroy first.
- If the holder already died, join the public cleanup result; do not send a
  second signal.
- Destroy only sandboxes created by the current case.
- Remove only run-owned generated config, cgroups, fixtures, and artifacts.
- Preserve summary, fault, and cleanup evidence before final sandbox destroy.
- Never run broad Docker cleanup or delete the shared manager registry.
- A cleanup failure fails its required validation checkpoint even when the
  behavioral assertion passed.

## 14. Implementation sequence

1. Add bounded helper unit tests for thread/FD parsing, cycle streaming,
   histograms, fault identity validation, and artifact caps.
2. Implement RE-00 and establish a packaged-daemon baseline.
3. Implement the exact-target fault helper and RE-01.
4. Implement RE-02 only after deterministic product race tests pass.
5. Implement RE-03 and fix lifecycle leaks before moving to routing work.
6. Implement RE-04 and the manager/Console counting and batching tests.
7. Implement RE-05 and the instrumented topology fast-path tests.
8. Implement config-owned RE-06 and RE-07 one case at a time.
9. Implement RE-08, then the capability-gated RE-09.
10. Implement RE-10 after diagnostic redaction tests pass.
11. Run RE-11 only after all focused cases are green.
12. Run one final observability proof including existing cgroup and
    resource-isolation cases.

Do not rerun already passing long cases while investigating a later failure.
Rerun the exact failed case, preserve its artifacts, then perform one final
scheduled proof.

## 15. Live-Docker runbook

Before every test command, append an entry to
`.e2e-state/TEST-REPORT.md` with `Command`, `Good`, `Defect`, and `Fix` fields.
Fill the pending result after the command; never rewrite earlier entries.

Run from:

```sh
cd /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test/e2e
```

Compile/collect and run non-Docker helper tests first:

```sh
python3 -m pytest --collect-only observability/resource_efficiency \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

Run the smoke case:

```sh
python3 -m pytest observability/resource_efficiency/test_smoke.py -m smoke \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

Run one focused case while debugging:

```sh
python3 -m pytest \
  observability/resource_efficiency/test_holder_lifecycle.py::test_unexpected_holder_exit \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

Run config-owning files serially and separately from non-config parallel work:

```sh
python3 -m pytest observability/resource_efficiency/test_runtime_budget.py \
  -m observability_config \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

After focused cases pass, run this family, then the broader observability tree:

```sh
python3 -m pytest observability/resource_efficiency \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox

python3 -m pytest observability \
  --test-repository-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test \
  --product-root /Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox
```

Rebuild the gateway and packaged daemon when product runtime code or config
shape changed. A stale packaged daemon is not proof. Report test wall time and
any build-lock/startup wait separately.

## 16. CI tiers

| Tier | Frequency | Cases | Environment |
| --- | --- | --- | --- |
| Helper/unit | Every change | parsers, bounded writers, fault identity | no Docker where possible |
| Smoke | Every relevant PR | RE-00 | packaged daemon, one sandbox |
| Focused lifecycle | Every holder/teardown change | RE-01, RE-02 | packaged Linux daemon |
| Nightly | Nightly | RE-01 through RE-06 plus existing RI/cgroup nightly cases | isolated Docker worker |
| Release | Before release | RE-07 through RE-11 plus existing RI/DS/GC release cases | Linux cgroup v2 qualification worker |

Long memory and fleet phases must use the existing foreign-container creation
guard. Configuration-owning cases are serial. Release soak failures are never
automatically retried.

## 17. Exit gates

The E2E work is complete when:

- RE-00 through RE-11 are registered with their exact IDs and validation
  checkpoints;
- focused holder exit and race cases pass against the packaged daemon;
- 1,000 lifecycle cycles and the six-hour soak show no zombie, FD, lease,
  session, thread, or anonymous-memory trend;
- manager-only single and fleet reads leave daemon CPU, I/O, memory, and event
  storage quiescent;
- explicit topology remains correct and bounded;
- default and pressure thread envelopes pass;
- admission pressure at fixed concurrency 12 stays bounded and responsive;
- the release Linux worker proves workload/daemon cgroup containment;
- triggered diagnostics are attributable, redacted, and capped;
- all existing resource-isolation and cgroup cases remain green;
- every case preserves bounded verdict and cleanup artifacts;
- `.e2e-state/TEST-REPORT.md` contains append-only command/result records; and
- one final observability proof passes after focused defects are resolved.

No manual sandbox restart, one-off memory screenshot, or successful cleanup
after the test deadline can substitute for these gates.
