# Sandbox Workspace and Daemon Resource-Efficiency Specification

Status: Draft
Date: 2026-07-18
Applies to: `ephemeral-sandbox`, `ephemeral-sandbox-console`, and the external
live E2E suite in this repository

## 1. Purpose

This specification defines the permanent correction for excessive CPU,
anonymous memory, process, and polling overhead observed in an otherwise idle
sandbox after a workspace session had been opened.

The intended outcome is not merely that a restart makes memory fall. The
system must make an unexpectedly dead workspace holder a bounded lifecycle
event, keep the per-sandbox daemon small by default, keep host-owned resource
reads out of the daemon, and prove those properties continuously.

The companion live-test design is
[`sandbox_resource_efficiency_e2e_test_spec.md`](sandbox_resource_efficiency_e2e_test_spec.md).

## 2. Relationship to existing contracts

This document is additive. It does not weaken or replace:

- the product
  [resource-isolation specification](../../../ephemeral-sandbox/docs/sandbox-observability-resource-isolation-spec.md),
  which owns disk-only history, idle-memory-neutral observability, and the
  manager/daemon polling boundary;
- the product
  [workspace process-topology specification](../../../ephemeral-sandbox/docs/sandbox-observability-workspace-process-topology-spec.md),
  which owns namespace-based process placement and bounded `/proc` collection;
- the external
  [resource-isolation live E2E specification](test_spec.md), which owns the
  long memory, disk, and workload-GC qualification campaigns; and
- the external
  [process-topology live E2E specification](cgroup/test_spec.md), which owns
  process placement, exit, destroy, and churn behavior.

Where the current combined `cgroup` operation serves both host resource series
and daemon process topology, this specification separates the two use cases.
The existing combined public response remains available during migration, but
ordinary resource and fleet reads must use a manager-only path.

## 3. Motivating incident and established evidence

The motivating sandbox was
`eos-c2006847-3cee-462f-89ff-43c2e6c8b9a4`. The incident snapshot showed:

| Signal | Affected sandbox | Comparable idle sandbox |
| --- | ---: | ---: |
| Daemon RSS | about 38.8 MiB | about 8.5 MiB |
| Daemon anonymous memory | about 35.2 MiB | about 4.9 MiB |
| Sandbox cgroup memory | about 39 MiB | about 12.3 MiB |
| Daemon threads | 34 | 35 |
| Workspace workload processes | 0 | 0 |

The affected workspace was still reported as active even though its namespace
holder, PID 65, was a zombie. The session retained three namespace/control
file descriptors and one layer lease. The workspace's mounted source was not
resident as file-backed memory; the excess was daemon anonymous memory.

Two independent amplification paths were also present:

1. production config requested 32 Tokio worker threads for every daemon; and
2. the Console used the combined `cgroup` operation for the Resources page and
   for one request per ready sandbox on the fleet dashboard, causing host
   resource reads to invoke daemon topology collection.

Repeated Resources polling accounted for measurable work but did not explain
the earlier 11–15% CPU interval. That interval ended before a stack profile was
captured. This specification therefore treats the exact historical hot
function as unproven and requires triggered diagnostics for any recurrence.

## 4. Root causes and contributing defects

### 4.1 Workspace holder ownership is incomplete

The daemon stores each namespace-holder `Child` in a process-global map after
startup. The child is polled and waited only when an explicit close or runtime
error enters the teardown path. There is no continuously active owner that
observes an unexpected exit and asks the workspace session service to clean up.

Consequences include:

- an unreaped zombie;
- a workspace that remains externally active but can no longer execute safely;
- namespace/control descriptors and layer leases retained until manual close
  or daemon restart; and
- workspace-associated allocations remaining reachable or allocator pages
  remaining resident after the useful work is gone.

### 4.2 `max_worker_threads` is an exact worker count

The production value is 32, and the Tokio builder receives it through
`worker_threads`. It is not a dynamic ceiling. A fleet of seven idle sandboxes
therefore starts roughly 224 Tokio worker threads before workload commands.

The blocking pool is not explicitly bounded or given a short idle keepalive.
Connection and command admission defaults are both 256, allowing a much larger
burst than an ordinary sandbox should accept.

### 4.3 Host resource reads and daemon topology share one operation

The manager always forwards a sandbox `cgroup` request to the daemon to obtain
topology after it reads Docker-derived resource series. The Resources page
polls this operation every two seconds. The fleet dashboard independently
issues the same operation once per ready sandbox every two seconds.

This violates the ownership rule that resource sampling is manager-owned and
must not wake a sandbox daemon. It also gives fleet polling `O(sandbox count)`
HTTP requests and daemon wakeups instead of one manager batch request.

### 4.4 The topology collector misses an empty-index fast path

When every workspace holder identity is unreadable, the namespace reverse map
is empty. The collector nevertheless enumerates numeric `/proc` entries and
stats namespaces for visible processes even though no process can match.

The collector must retain its no-PID-cache and no-background-task properties.
An early return when the reverse map is empty satisfies those rules while
removing useless work.

### 4.5 Resource use is not protected by explicit budgets

The inspected sandbox had no Docker CPU, memory, or PID ceiling. A correctness
fix must not rely on limits, but production needs limits as a final containment
layer. A runaway workload should not consume unbounded host resources or make
the daemon unresponsive.

### 4.6 Self-observability is insufficient for attribution

The public daemon view could not report allocator residence, runtime queue
depth, blocking-task count, holder exits, or cleanup latency. Once the CPU
interval ended, there was no evidence capable of naming the hot task.

## 5. Goals

1. Reap every namespace holder exactly once, including unexpected exits.
2. Remove a dead holder's workspace from active service within a bounded time.
3. Release all run-owned descriptors, leases, mounts, network state, scratch
   state, child sessions, and persisted active handles during cleanup.
4. Make explicit destroy and unexpected exit safe when they race.
5. Reduce default per-sandbox daemon threads and admission limits without
   reducing tested supported throughput.
6. Make Resources and fleet usage reads manager-only.
7. Make process topology explicit, visible-page scoped, and bounded.
8. Bound sandbox CPU, memory, and PID consumption through selectable profiles.
9. Expose enough self-metrics to attribute a future CPU or memory regression.
10. Turn all resource expectations into automated unit, integration, live,
    nightly, and release gates.

## 6. Non-goals

- Removing workspace namespaces or the namespace-holder architecture.
- Loading, indexing, or scanning workspace source files to estimate memory.
- Caching process PIDs between topology requests.
- Adding a background process-topology collector.
- Treating periodic daemon restart, forced allocator trimming, or larger host
  capacity as the primary correction.
- Requiring one cgroup per workspace for process-topology correctness.
- Claiming that the ended 11–15% CPU interval has been exactly attributed
  without a captured profile.

## 7. Required invariants

### 7.1 Workspace lifecycle

| ID | Requirement |
| --- | --- |
| WL1 | Every spawned namespace holder has exactly one live supervisor and one reap owner. |
| WL2 | An unexpected holder exit is detected within one second under normal scheduler operation. |
| WL3 | A holder is waited exactly once; no zombie remains after the detection deadline. |
| WL4 | The workspace stops accepting new work immediately after holder death. |
| WL5 | Explicit destroy and unexpected exit converge on one idempotent teardown transaction. |
| WL6 | Completed teardown leaves zero holder/control namespace FDs, zero active layer leases, no live child commands, and no active persisted handle for that workspace. |
| WL7 | PID reuse cannot make cleanup signal, inspect, or reap an unrelated process. |
| WL8 | Unexpected exit never silently publishes a workspace and never silently discards a publish-required recovery artifact. |
| WL9 | One failed workspace does not terminate the daemon or disturb peer workspaces. |
| WL10 | Every holder exit and cleanup result is represented by a bounded structured event and counter. |

### 7.2 Daemon runtime

| ID | Requirement |
| --- | --- |
| DR1 | The standard production profile starts with two Tokio worker threads. |
| DR2 | Worker count is an exact, clearly named setting; the legacy name is accepted only as a compatibility alias. |
| DR3 | The blocking pool has an explicit maximum and idle keepalive. |
| DR4 | Connection and active-command admission are bounded and return structured overload errors. |
| DR5 | Thread count returns to the declared idle bound after burst cooldown. |
| DR6 | Runtime sizing is per daemon profile, not multiplied by open workspace count. |

### 7.3 Observability routing and collection

| ID | Requirement |
| --- | --- |
| OR1 | A sandbox resource-series read is fulfilled entirely by the manager's Docker/cgroup ring. |
| OR2 | A fleet current-usage read uses one manager batch request, not one request per sandbox. |
| OR3 | Resource and fleet reads make zero sandbox-daemon invocations. |
| OR4 | Process topology is requested only by an explicit topology consumer. |
| OR5 | A stable idle Processes page stops daemon topology calls after state is resolved and resumes from a manager activity revision or direct user action. |
| OR6 | A hidden page performs no polling; focus performs one manager revision check before any daemon request. |
| OR7 | An empty valid-identity index returns without enumerating numeric `/proc` entries. |
| OR8 | A nonempty topology request scans numeric `/proc` entries at most once and retains existing row, warning, and read-size caps. |
| OR9 | Resource-only responses remain useful when the daemon is stopped or unreachable. |
| OR10 | The combined `cgroup` route remains backward compatible for one migration window and is not used by ordinary Resources or fleet UI paths. |

### 7.4 Resource protection and evidence

| ID | Requirement |
| --- | --- |
| RP1 | Every production sandbox selects an explicit CPU, memory, and PID profile. |
| RP2 | Limits contain runaway workloads without preventing control-plane cleanup. |
| RP3 | Daemon and workload consumption are independently measurable; where cgroup v2 delegation is available, workload pressure cannot OOM the daemon first. |
| RP4 | Daemon self-metrics expose RSS, PSS, anonymous/private memory, allocator active/resident bytes when supported, thread counts, task/queue counts, holder counts, cleanup latency, FDs, and leases. |
| RP5 | A triggered diagnostic artifact is captured when sustained daemon CPU or memory exceeds configured thresholds. |
| RP6 | Metric and diagnostic collection is bounded and does not introduce a new idle loop. |

## 8. Detailed design

### 8.1 Namespace-holder supervision

Replace the passive process-global `PID -> Child` map with an owned holder
record that includes:

- workspace session ID;
- `Child` ownership;
- holder PID and a PID-reuse-safe identity;
- Linux `pidfd` when available;
- captured process start time as a diagnostic fallback;
- readiness and control descriptor ownership;
- creation generation; and
- a one-shot teardown token.

On Linux, the preferred watcher is `pidfd` integrated with the daemon runtime.
A dedicated `SIGCHLD`/`waitid` reaper is an acceptable fallback where `pidfd`
is unavailable. Periodically calling `kill(pid, 0)` is not sufficient because
it neither reaps a child nor prevents PID reuse.

The watcher sends a typed holder-exit event to the workspace session owner. It
must not perform mount, layer, or persistence cleanup while holding the child
registry lock. The session owner atomically marks the workspace unavailable to
new operations, then enters the existing teardown transaction.

The teardown transaction must be idempotent and record each resource as
released before it can be retried. A simultaneous explicit destroy joins that
transaction and receives the same terminal result. Cleanup failures are
reported, retried with a bounded policy, and left visible to reconciliation;
they are not converted to a successful close.

Finalization rules are:

- `no_op`: never publish; clean disposable scratch after child and mount
  teardown;
- publish/finalize requested: never claim successful publication after holder
  death unless the normal finalize transaction completes; otherwise preserve
  a bounded recovery artifact and surface `finalization_failed`; and
- all policies: release the active layer lease after the finalization decision
  has been durably recorded.

Daemon startup reconciliation remains necessary for a daemon crash, but it is
not a substitute for live supervision.

### 8.2 Runtime sizing and admission

Introduce explicit settings:

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

`max_worker_threads` is accepted as a deprecated alias for one compatibility
release. Supplying both names is a validation error. Configuration validation
rejects zero and values above a documented safety maximum.

The first release candidates are:

| Profile | Workers | Blocking max | Connections | Active commands |
| --- | ---: | ---: | ---: | ---: |
| Standard | 2 | 8 | 64 | 32 |
| Build-heavy | 4 | 16 | 128 | 64 |

These values must pass the concurrency and workload suite before becoming
defaults. They may be adjusted from benchmark evidence, but the production
default must not derive worker count directly from all host CPUs visible to a
container.

Blocking work uses existing admission semaphores before `spawn_blocking`.
Creating an unbounded queue in front of a bounded blocking pool is forbidden.

### 8.3 Manager-only resources and explicit topology

The durable API model has two operations:

1. a manager-owned resource operation returning the host resource series for
   one sandbox or a batch of current values for the fleet; and
2. an explicit process-topology operation that contacts one sandbox daemon.

During migration, the combined `cgroup` operation may continue to merge both
payloads for CLI compatibility. The Console must stop using it for Resources
and fleet current usage as soon as the manager-only operations exist.

The fleet operation reads the manager's existing resource rings in one request
and returns records keyed by sandbox ID. It must not fan out to daemon or
per-sandbox HTTP operations.

The Processes view follows this scheduling state machine:

```text
visible + unresolved -> request topology
topology active      -> poll at active cadence
topology idle        -> stop daemon polling
manager revision     -> request topology once
user interaction     -> request topology once
tab hidden           -> stop all polling
focus                -> manager revision check only
```

Resource counters and sample timestamps are never treated as workspace
activity. A full JSON serialization of a resource response is therefore not a
valid activity fingerprint.

### 8.4 Topology empty-index fast path

After building workspace entries and the namespace reverse index:

- if no workspaces exist, return an available empty topology immediately;
- if workspaces exist but none has a readable PID and mount namespace pair,
  return those workspaces as `partial` with bounded warnings immediately; and
- otherwise enumerate numeric `/proc` entries once and retain the existing
  matching and response caps.

This change does not cache PIDs, add persistent state, or weaken proc-race
handling.

### 8.5 Resource profiles

The manager exposes named sandbox profiles rather than scattered raw Docker
flags. At minimum a profile declares:

- CPU quota or equivalent;
- memory high and hard maximum where supported;
- PID maximum;
- daemon runtime profile; and
- whether a separately bounded workload cgroup is available.

Initial profile values must be established by real workloads. A suggested
developer starting point is 1 CPU, 512 MiB, and 256 PIDs for a standard
sandbox, with a larger explicit build profile. These numbers are guardrails,
not proof of low idle consumption.

On cgroup v2 hosts that support delegation, workload commands run below a
workload cgroup with the daemon outside that leaf. The daemon must retain
enough headroom to interrupt commands, finalize or discard a workspace, and
serve destroy after workload pressure. Unsupported hosts report the lack of
delegation explicitly and still apply the outer container profile.

### 8.6 Self-metrics and triggered diagnostics

Add a bounded `topology.daemon` or equivalent daemon-self payload containing:

- process RSS, PSS, `Anonymous`, `Private_Dirty`, and `AnonHugePages`;
- thread count and Tokio worker configuration;
- active async tasks, blocking tasks, queue depth, and admission usage where
  supported;
- allocator allocated, active, mapped, and resident bytes when the selected
  allocator exposes them;
- open workspace count, live holder count, exited-unreaped holder count;
- namespace/control FD and active layer-lease counts;
- last holder-exit reason and cleanup duration as bounded summaries; and
- daemon CPU and I/O counters.

Sampling on public read is preferred to a daemon background sampler. Sustained
threshold capture may use one bounded supervisor already responsible for
process health. It records a capped diagnostic bundle and applies a cooldown;
it must not continuously profile an idle daemon.

Suggested initial triggers are daemon CPU above 2% of one core for 30 seconds,
anonymous memory above the profile's warm budget for 60 seconds, or any
exited-unreaped holder count above zero. Thresholds are configurable.

## 9. Memory-reclamation policy

Correct ownership and dropping of workspace state come before allocator
tuning. The implementation must prove that holder/session objects, command
registries, response buffers, and teardown reports become unreachable after
cleanup.

If anonymous memory still fails cooldown gates after those fixes:

1. add allocator active/resident evidence;
2. compare the current allocator with bounded arena settings and at least one
   production-suitable alternative under the same workload;
3. select from measured steady-state memory and contention; and
4. keep the live cooldown gate regardless of allocator choice.

Periodic restart, unconditional `malloc_trim`, or a timer-driven purge is not a
correctness mechanism and cannot make a failing lifecycle test pass.

## 10. Public behavior and compatibility

- Existing workspace create, execute, finalize, and destroy success responses
  remain compatible.
- An operation racing holder death returns a structured workspace-terminated
  or cleanup-in-progress error, never a generic transport timeout.
- Repeated destroy is idempotent according to the existing public lifecycle
  contract.
- Resource-only responses are available when the target daemon is unavailable;
  the response may state that process topology was not requested.
- The combined `cgroup` response retains `view`, `scope`, `series`, and
  `topology` during its deprecation window.
- The Console may render older daemons through the combined operation, but it
  must use a reduced cadence and display that resource-isolation guarantees
  require the new manager-only route.

## 11. Implementation touch points

| Area | Current touch point | Required direction |
| --- | --- | --- |
| Holder ownership | `crates/sandbox-runtime/workspace/src/namespace/holder.rs` | Owned supervisor, PID-safe wait, typed exit event |
| Workspace teardown | `crates/sandbox-runtime/workspace/src/lifecycle/destroy.rs` | Idempotent joinable cleanup transaction |
| Startup recovery | `crates/sandbox-runtime/workspace/src/lifecycle/persistence.rs` | Reconcile crash leftovers without replacing live supervision |
| Daemon runtime | `crates/sandbox-daemon/src/serve.rs` | Explicit worker and blocking-pool limits |
| Config | `crates/sandbox-config/src/configs/daemon.rs`, `runtime.rs`, production YAML | New names, defaults, profiles, validation |
| Manager resource service | `crates/sandbox-manager/src/management/service/impls/resource_metrics.rs` | Manager-only single and fleet resource operations |
| Topology collector | `crates/sandbox-observability/telemetry/src/collect/process_topology.rs` | Empty-index early return |
| Console Resources | `ephemeral-sandbox-console/web/src/pages/sandbox/observability/ResourcesView.tsx` | Manager resource operation only |
| Console fleet | `ephemeral-sandbox-console/web/src/poll/useFleetCurrentUsage.ts` | One batch manager request |
| Console polling | `ephemeral-sandbox-console/web/src/poll/usePoll.ts` | Semantic activity/revision gating |
| Live qualification | `ephemeral-sandbox-test/e2e/observability` | Companion E2E specification and cases |

## 12. Verification requirements

### 12.1 Product unit tests

- holder normal exit, signal exit, duplicate exit notification, and wait error;
- explicit destroy racing holder exit in every interleaving;
- PID identity mismatch refuses to signal or clean an unrelated process;
- teardown step failure retries without double releasing another step;
- finalization policy behavior after holder death;
- config alias, conflict, lower bound, upper bound, and defaults;
- topology collector performs zero proc enumeration when the reverse index is
  empty; and
- topology collector retains all existing bounds when the index is nonempty.

### 12.2 Product integration tests

Use a counting fake daemon client and real manager service:

- 10,000 single-sandbox resource reads produce zero daemon invocations;
- 10,000 fleet resource reads produce zero daemon invocations;
- one explicit topology read produces exactly one daemon invocation;
- daemon failure does not remove manager resource series; and
- one batch response covers all requested ready sandboxes without per-sandbox
  manager RPC fanout.

Console fake-timer tests prove:

- Resources never invokes topology;
- fleet usage performs one batch request per cadence;
- timestamp/counter changes do not reset activity;
- stable idle topology stops daemon requests;
- a manager revision or direct interaction resumes one request; and
- hidden and focus behavior follows Section 8.3.

### 12.3 Live E2E and release qualification

The companion E2E specification owns exact case IDs and artifacts. Passing
only unit or integration tests is insufficient because zombie state, procfs,
allocator residence, Docker limits, and packaged daemon configuration are live
properties.

## 13. Acceptance gates

The correction is complete only when all of these pass:

### Correctness

- exact holder fault injection leaves no zombie after one second;
- the affected workspace stops accepting new work immediately;
- cleanup reaches zero run-owned namespace/control FDs and zero active layer
  leases;
- peer workspaces and commands remain healthy;
- explicit destroy/exit races are idempotent; and
- 100 sequential workspace create/use/destroy cycles leave no holder,
  descriptor, lease, scratch, command, or persisted-session growth.

### Full-daemon efficiency

The following are initial product SLO candidates and become hard gates after a
baseline qualification on supported Linux and Docker Desktop environments:

- standard-profile idle daemon RSS at or below 12 MiB;
- standard-profile idle sandbox cgroup memory at or below 20 MiB;
- post-workspace daemon RSS at or below 16 MiB within 60 seconds;
- idle daemon CPU below 0.1% of one core over a five-minute median window;
- at most eight daemon threads while idle and at most the configured worker
  plus blocking/admission budget under pressure; and
- no upward anonymous-memory, FD, thread, or lease trend across lifecycle
  cycles.

The authoritative observability deltas remain those in the resource-isolation
specification: at most 4 KiB/hour anonymous slope, at most 64 KiB steady delta,
at most 128 KiB post-burst cooldown delta, zero anonymous huge pages, and zero
daemon invocations for manager resource reads.

### Routing and topology

- Resources and fleet traffic make zero daemon requests in counting-fake
  integration tests;
- live manager resource polling causes no daemon storage I/O and no meaningful
  daemon CPU or anonymous-memory delta;
- fleet usage is one manager request per cadence, independent of sandbox count;
- an empty or all-invalid namespace index performs no numeric proc scan in the
  instrumented collector test; and
- explicit live topology remains correct under process churn.

### Protection and diagnosis

- configured CPU, memory, and PID profiles appear in Docker/cgroup state;
- workload pressure cannot prevent interrupt, workspace cleanup, or sandbox
  destroy;
- every unexpected holder exit increments a metric and produces one bounded
  event; and
- a forced sustained CPU test produces one capped diagnostic bundle containing
  enough evidence to identify the active task class.

## 14. Rollout plan

1. Land failing unit and integration tests for holder exit, route ownership,
   polling, config, and topology fast path.
2. Land live holder-fault and lifecycle-cycle smoke tests behind the external
   suite's normal capability gating.
3. Implement holder supervision and idempotent teardown.
4. Split manager resource and explicit topology operations; migrate Resources
   and fleet UI traffic.
5. Reduce runtime defaults and add blocking/admission bounds after focused load
   qualification.
6. Add self-metrics, triggered diagnostics, and named resource profiles.
7. Run focused live cases, then the existing resource-isolation nightly cases.
8. Run one complete release qualification, including GC isolation and the
   lifecycle soak, before removing the legacy config name or combined UI path.

Each stage is independently revertible except persisted lifecycle schema
changes, which require forward/backward compatibility for one release.

## 15. Rejected fixes

| Proposal | Reason rejected |
| --- | --- |
| Restart daemons on a timer | Hides zombies and loses attribution; does not make cleanup correct. |
| Increase sandbox memory | Raises the failure ceiling without reducing retention. |
| Remove workspaces when idle | Workspaces are a required execution boundary; normal idle workspaces should be cheap. |
| Poll faster to detect activity | Increases the very daemon work this design removes. |
| Cache `/proc` PID results | Violates PID-reuse and current topology contracts. |
| Treat a missing holder as topology-only `partial` forever | Preserves an unusable active workspace and its resources. |
| Call allocator trim after every request | Adds latency and masks reachable-state leaks. |
| Apply Docker limits only | Contains damage but does not fix lifecycle, polling, or oversized defaults. |

## 16. Definition of done

The feature is done when the implementation, product tests, external live
cases, artifacts, compatibility behavior, and resource budgets all satisfy
this specification. A one-time recovery of the motivating sandbox or a lower
RSS immediately after restart is not completion evidence.
