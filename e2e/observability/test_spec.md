# Observability Resource-Isolation Live E2E Specification

Status: implemented; the live cases below are registered under
`e2e/observability/resource_isolation/`.
Product contract:
[`sandbox-observability-resource-isolation-spec.md`](../../../ephemeral-sandbox/docs/sandbox-observability-resource-isolation-spec.md)

## 1. Purpose and ownership

This directory owns the backend end-to-end proof that sandbox observability is
idle-memory-neutral and disk-bounded. In this specification, **live E2E** means
a real sandbox created by the external harness, running the packaged Linux
daemon in Docker, reached through the product's public CLI and gateway paths.

The product repository owns deterministic Rust unit tests, service integration
tests with counting fakes, allocator tests, and console tests with fake timers.
Those tests are necessary, but they cannot replace this suite. Browser and
Playwright tests are outside this backend E2E boundary. Console-like traffic is
represented here by repeatedly calling the same public backend operations that
the console calls.

The governing rule is stronger than “RSS eventually falls”: after warmup,
observability must retain no telemetry history in the daemon, must perform no
work while idle, and must not create memory pressure that makes a colocated
workload collect more often. Test code follows the same discipline: samples are
streamed to bounded disk artifacts and are never accumulated in an unbounded
Python list.

## 2. Live-test boundary

Every case must satisfy all of these constraints:

- create and destroy sandboxes through the shared manager lifecycle helpers;
- use the packaged daemon installed by the normal Docker sandbox flow, never a
  host debug binary or an in-process daemon;
- invoke product behavior only through `sandbox-manager-cli`,
  `sandbox-runtime-cli`, and `sandbox-observability-cli` via the shared
  `harness.runner.cli` wrappers;
- declare a stable `@e2e_test` id, features, validations, execution surface,
  and a realistic timeout;
- register every declared validation checkpoint with `validation(...)`;
- use Docker only as an out-of-band measurement or fixture-installation tool;
  Docker commands must not invoke observability behavior directly;
- make no assertion from daemon or gateway log text;
- clean up only sandbox ids and temporary paths created by the current run;
  broad Docker prune, broad container deletion, and shared-state deletion are
  forbidden.

Direct `/proc` and filesystem inspection is measurement evidence, not the
operation surface. A test may use `docker exec` to read the verified daemon's
`/proc/<pid>/*` entries and `stat`, or `docker cp` to install a deterministic
persisted-history fixture. It must first prove that the measured process is the
packaged daemon and, in the normal Docker-init topology, that it is PID 1's
direct child named by the daemon PID file. One sampler invocation should
collect all process, cgroup, and file counters so the measurement itself
creates as little activity as possible.

Exact internal call counts are not a live-test assertion. The product
integration suite proves “zero manager-to-daemon calls” with a counting fake.
This live suite proves the externally observable consequence: no daemon memory
trend, no daemon CPU or I/O work, and no event-store mutation during idle
manager polling.

## 3. Implemented layout

```text
e2e/observability/
  README.md
  test_spec.md
  snapshot/
    test_snapshot.py                 # existing public-contract smoke tests
  resource_isolation/
    __init__.py
    conftest.py                      # run-scoped artifacts and config custody
    helpers.py                       # bounded samplers and streaming analysis
    test_memory.py                   # RI-SMOKE, RI-01..RI-04
    test_disk.py                     # DS-01..DS-04
    test_faults.py                   # DS-05 and admitted recovery tests
    test_gc_isolation.py             # GC-01 release qualification
```

Configuration-changing tests must use generated YAML below `tmp_path`; they
must never edit checked-in `config/prd.yml` or `config/bench.yml`. They own the
gateway serially, following the existing configuration-family custody pattern,
and restore the baseline gateway in a finalizer even after failure.

## 4. Measurement contract

### 4.1 Raw sample

At a monotonic one-second cadence, the sampler writes one compact JSON object
directly to `samples.jsonl` containing:

- wall and monotonic timestamps, phase, arm, repetition, and sandbox id;
- daemon `/proc/<pid>/smaps_rollup`: `Rss`, `Pss`, `Anonymous`, `Private_Dirty`, and
  `AnonHugePages`;
- daemon `/proc/<pid>/stat`: user and system CPU ticks;
- daemon `/proc/<pid>/io`: read/write characters, syscall counts, and storage bytes;
- cgroup v2 `memory.current` and the `memory.stat` fields `anon`, `file`,
  `kernel`, `kernel_stack`, `pagetables`, `sock`, `slab`, and `anon_thp`;
- active and rotated event-store length, allocated blocks, inode, and mtime;
- manager host-ring length and allocated blocks when that ring exists.

`smaps_rollup Anonymous` is the daemon memory gate. Container/cgroup totals are
diagnostic because they include short-lived observer processes and workloads.
RSS alone is not a gate because it includes reclaimable file-backed pages.

Each read must have a deadline. A missing counter creates an explicit
`unavailable` field; it must not silently become zero. Required memory fields
being unavailable is a failure in the Linux nightly/release environment.

### 4.2 Bounded test-process memory

The sampler writes and flushes each JSON line immediately. Online state is
limited to counters, extrema, fixed histograms, and a deterministic reservoir
of at most 2,048 points per metric. Final analysis makes a streaming second
pass over `samples.jsonl`; it must not call `read_text`, `readlines`, or build a
list proportional to run duration.

The helper has its own unit test that feeds ten million synthetic samples and
asserts Python peak traced allocation remains within 2 MiB of the 10,000-sample
run. This is a harness test, not a sandbox test.

### 4.3 Store fingerprint

`store-before.json` and `store-after.json` record, for both segments:

- existence, byte length, allocated bytes, inode, nanosecond mtime, and SHA-256;
- count of complete parseable lines and presence of a partial final line;
- total logical and allocated bytes.

Repeated reads pass only when every fingerprint field is identical. Hashing is
streaming with a fixed chunk. The suite never copies a whole telemetry file
into Python memory.

### 4.4 Trend analysis

For each steady phase and each repetition:

1. discard the declared warmup interval, never data chosen after seeing it;
2. compute a deterministic sampled Theil-Sen slope, capped at 100,000 point
   pairs with a fixed seed;
3. compute first-five-minute and final-five-minute medians with a bounded
   deterministic reservoir;
4. report a fixed-seed bootstrap 95% confidence interval;
5. apply every hard bound to every repetition separately.

An average cannot hide one failing repetition. A failed run is not rerun until
it passes. The artifact records host architecture, kernel, Docker version,
daemon build identity, cgroup mode, clock ticks per second, sandbox limits, and
test configuration so environmental changes are visible.

## 5. Live case catalog

These are the required stable catalog declarations. Timeout is for the entire
case, including all required repetitions and cleanup; an implementation may
split repetitions into separate CI jobs only if it emits one aggregate verdict
that applies the per-repetition gates.

| Case | `@e2e_test` id | Marker | Timeout | Required validation checkpoints |
| --- | --- | --- | ---: | --- |
| RI-SMOKE | `observability.resource-isolation.smoke` | `smoke` | 8 min | `idle-store-pure`, `polling-memory-coarse`, `artifact-bounded` |
| RI-01 | `observability.resource-isolation.idle-memory` | `nightly` | 2 h | `idle-anonymous-trend`, `idle-daemon-quiescent`, `no-anon-thp` |
| RI-02 | `observability.resource-isolation.polling` | `nightly` | 2 h | `polling-read-pure`, `polling-memory-neutral`, `resource-ring-fixed` |
| RI-03 | `observability.resource-isolation.history-query` | `nightly` | 45 min | `query-response-bounded`, `query-memory-input-independent`, `query-store-pure` |
| RI-04 | `observability.resource-isolation.enabled-disabled` | `release observability_config` | 3.5 h | `fixed-overhead-bounded`, `disabled-store-absent`, `config-restored` |
| DS-01 | `observability.resource-isolation.disk-cap` | `nightly observability_config` | 45 min | `total-cap-strict`, `segments-parseable`, `allocated-bytes-bounded` |
| DS-02 | `observability.resource-isolation.read-purity` | `nightly` | 2 h | `all-views-store-pure`, `response-artifact-bounded` |
| DS-03 | `observability.resource-isolation.enospc` | `release observability_config` | 45 min | `runtime-fail-open`, `retry-loop-absent`, `drop-count-exact` |
| DS-04 | `observability.resource-isolation.ring-lifecycle` | `release observability_config` | 4 h | `per-ring-cap`, `aggregate-ring-cap`, `destroy-removes-rings` |
| DS-05 | `observability.resource-isolation.recovery` | `release observability_config` | 1 h | `corruption-bounded`, `lifecycle-survives`, `recovery-scope-safe` |
| GC-01 | `observability.resource-isolation.workload-gc` | `release` | 4 h | `workload-no-oom`, `gc-regression-bounded`, `daemon-gates-pass` |

All declarations use `execution_surface="cli"`. Feature tuples use the
existing catalog names appropriate to each case: `observability.snapshot`,
`observability.events`, `observability.trace`, `observability.cgroup`,
`runtime.command`, and `manager.management`. Configuration-owning cases also
declare the configuration feature they exercise.

### RI-SMOKE — gross idle regression

Tier: `smoke`; one real sandbox; target duration: five minutes.

Warm for one minute, fingerprint the store, leave the sandbox idle for two
minutes, then issue public scoped snapshots at one request per second for two
minutes. Assert:

- the sandbox stays ready and every public response is structured;
- event-store fingerprints do not change during the idle and read phases;
- `Anonymous` does not grow by more than 1 MiB from the post-warmup baseline;
- no `AnonHugePages` appears;
- the evidence artifact is within its disk cap.

This catches gross regressions only. It is not memory-isolation qualification.

### RI-01 — idle daemon is memory-neutral

Tier: `nightly`; three repetitions; five-minute warmup plus thirty-minute idle.

Create one enabled sandbox per repetition and dispatch no product request after
warmup. Sample out of band. Each repetition passes only when:

- `Anonymous` Theil-Sen slope is at most 4 KiB/hour;
- final-five-minute minus first-five-minute `Anonymous` median is at most
  64 KiB;
- daemon CPU advances by less than one scheduler tick per minute after
  subtracting sampler-free baseline noise measured before the phase;
- daemon read/write storage bytes and both store fingerprints are unchanged;
- `AnonHugePages` and cgroup `anon_thp` remain zero.

### RI-02 — backend polling is read-only and memory-neutral

Tier: `nightly`; three repetitions; paired target and idle-control sandboxes.

After equal warmup, leave the control untouched. Against the target, alternate
the public aggregate snapshot, scoped snapshot, events, trace/latest-trace, and
manager-owned resource/cgroup routes at the console's active cadence for thirty
minutes. Do not run a browser. Assert every route's documented response bound,
then apply the RI-01 gates to both arms. Additionally:

- target-minus-control `Anonymous` median growth is at most 64 KiB;
- target-minus-control post-cooldown `Anonymous` is at most 128 KiB;
- the daemon event store is byte-for-byte unchanged;
- daemon storage I/O does not increase after warmup;
- manager-owned resource-ring bytes stay fixed and within 64 KiB.

The counting-fake product test remains responsible for proving that manager
status/resource polling made exactly zero daemon calls.

### RI-03 — persisted history cannot scale query memory

Tier: `nightly`; one real sandbox per repetition.

Generate valid NDJSON as a stream, capped just below the configured event-store
budget, and install it as the rotated segment with `docker cp`. Do not create a
large Python string by record multiplication. Exercise every public daemon
view over one-record, half-cap, and near-cap stores. For each size:

- the response stays within 500 records and 256 KiB;
- peak `Anonymous` above the settled pre-query median is at most 512 KiB;
- after a five-minute cooldown, `Anonymous` returns within 128 KiB;
- response memory does not increase with input history size beyond 64 KiB
  measurement tolerance;
- both input file fingerprints remain unchanged.

The existing `observability.snapshot.bounded-memory-history` test, with twelve
polls and a 50 MiB container-memory allowance, remains only a coarse regression
until RI-03 replaces it; it is not conformance evidence.

### RI-04 — enabled versus disabled fixed overhead

Tier: `release`; five alternating A/B repetitions.

Use generated daemon YAML to create otherwise identical enabled and disabled
sandboxes. Alternate arm creation order each repetition. After five minutes of
warmup and thirty minutes idle:

- enabled-minus-disabled `Anonymous` median is at most 64 KiB;
- enabled-minus-disabled CPU is below one scheduler tick per minute;
- the disabled sandbox creates no observability event segments;
- the enabled sandbox has no idle store mutation and no anonymous huge pages.

Because daemon YAML is read at sandbox creation, every arm uses a fresh
sandbox. The suite must preserve evidence before teardown and restore the
baseline gateway in all outcomes.

### DS-01 — strict total event-store cap

Tier: `nightly`; configured total: 1 MiB.

Prefill near every boundary using streamed valid fixtures, then trigger real
event-producing runtime operations through `sandbox-runtime-cli`. Sample both
segments after every operation and through at least ten rotations. Assert:

- `active.len + rotated.len <= max_disk_bytes` after every append;
- each segment is at most half the total cap;
- every complete persisted line parses and no middle line is partial;
- allocated bytes are at most the logical cap plus one filesystem block per
  segment;
- oversized escaped and multi-byte records become one bounded marker or one
  accounted drop, never a cap overshoot.

### DS-02 — all public reads preserve disk exactly

Tier: `nightly`; 10,000 total reads distributed across all public views.

Seed a known two-segment store, fingerprint it, issue the reads, and fingerprint
it again. Length, allocated blocks, inode, mtime, SHA-256, complete-line count,
and partial-tail state must be identical. Only a compact response hash and
counter are retained; repeated response bodies are not written to artifacts.

### DS-03 — isolated storage exhaustion is fail-open

Tier: `release`; Linux capability-gated.

Give only the current sandbox's event store a test-owned tiny filesystem or
quota. Never fill Docker Desktop's global disk, the product repository, `/tmp`,
or the host volume. Force `ENOSPC`, then run successful public runtime commands.
Assert bounded command latency, no tight retry loop in daemon CPU/I/O counters,
no store growth beyond the quota, and a public fixed-width drop counter
increase matching attempted records.

A capability skip is allowed on developer Docker Desktop only when the result
records the missing isolation primitive. The release Linux environment must
provide the primitive; a skip there is a failure.

### DS-04 — host resource-ring budget and lifecycle

Tier: `release`; 100 real sandboxes.

Use a generated manager configuration whose registry parent is under a
run-owned temporary directory; the resource-ring root is derived from it. Wait
for multiple wraps. Assert one ring per live sandbox, each exactly 64 KiB or
less, total logical bytes at most `N * 64 KiB`, no unbounded manager resource
history, and no file for an unrelated sandbox. Destroy the 100 sandboxes and
assert only their ring files disappear.

### DS-05 — bounded recovery and corruption behavior

Tier: `release`; Linux capability-gated.

Seed a malformed line, invalid UTF-8, partial final line, torn newest ring
record, and unsupported ring version in run-owned state. Restart only the
test-owned sandbox/gateway through an explicit harness recovery primitive.
Assert reads return valid bounded data or a structured partial/unavailable
result, lifecycle operations still succeed, and every store remains within its
cap.

Timed `SIGKILL` during a guessed rotation window is forbidden: it is flaky and
can target the wrong process. Append/rename crash points remain deterministic
product tests until the external harness has a run-scoped restart and
fault-point primitive. The missing primitive must be reported as an admission
gap, not hidden with `xfail`.

### GC-01 — colocated Node workload isolation

Tier: `release`; five alternating A/B repetitions.

Run a pinned Node.js allocation workload under identical tight cgroup limits in
enabled and disabled sandboxes. Write workload measurements incrementally to a
bounded JSONL artifact using `--trace-gc` or `PerformanceObserver`; never buffer
the trace in the daemon or the test process. Assert:

- neither arm OOMs;
- enabled p99 GC pause is no more than disabled p99 plus 1 ms;
- enabled p99 event-loop delay is no more than 5% above disabled plus 1 ms;
- workload peak RSS has no unexplained enabled-arm step;
- the RI memory, huge-page, and store-mutation gates continue to pass.

## 6. Artifact and disk budget

All evidence lives below `.e2e-state/observability/<run-id>/<case-id>/` or the
CI-provided equivalent. Required files are:

- `environment.json` — immutable run metadata;
- `samples.jsonl` — raw bounded measurements;
- `store-before.json` and `store-after.json` — streaming fingerprints;
- `summary.json` — gates, slopes, medians, confidence intervals, and verdict;
- `gc.jsonl` only for GC-01.

No CLI response transcript, daemon log, core dump, telemetry-file copy, or
container export is retained by default. Each case has a 32 MiB uncompressed
artifact hard cap; the helper stops adding optional diagnostics before the cap
but must always reserve space for `summary.json`. RI cases at one-second cadence
fit below the cap. The six-hour release soak, if enabled, samples at five-second
cadence. CI retains failed artifacts for seven days and successful artifacts
for three days; local `.e2e-state` remains disposable.

Artifact-cap exhaustion is a test failure, not permission to delete unrelated
artifacts. SHA-256 and online counters replace repeated payload storage.

## 7. Scheduling, isolation, and flake policy

Add and register `nightly`, `release`, and `observability_config` pytest
markers. `observability_config` owns the gateway and is serial with the existing
`config` family. Memory comparisons must run without other E2E workers creating
sandboxes on the same Docker daemon. The test records host load and rejects a
sample phase if its one-second cadence misses more than 1% of deadlines; this
is an environmental failure, not a product pass.

| Tier | Required coverage | Expected time |
| --- | --- | ---: |
| Per-change observability smoke | existing snapshot tests + RI-SMOKE | 5–8 min |
| Nightly | RI-01..RI-03, DS-01, DS-02; three repetitions | about 3 h |
| Release | RI-04, DS-03..DS-05, GC-01; five A/B repetitions | scheduled lane |
| Release soak | six-hour strict-cap producer and 100-sandbox cleanup | 6+ h |

No automatic rerun may convert a failed memory or disk gate to a pass. Every
failure preserves its bounded evidence. Environmental invalidation must name a
predeclared reason such as missing cgroup v2, missing isolated quota support,
or excessive sampler deadline loss.

## 8. Cleanup and failure guarantees

- Use the shared `sandbox` fixture for one-sandbox cases and a registered
  factory/finalizer for multi-sandbox cases.
- Register ids immediately after successful creation, before the next action.
- Harvest bounded evidence before destruction, including when the assertion
  body failed.
- Destroy in reverse creation order and let the session safety net handle only
  ids registered by this run.
- Restore baseline gateway configuration in a `finally`-equivalent finalizer.
- Delete only generated config, quota, ring, and workspace paths whose resolved
  parent is the current pytest temporary root.

Cleanup failure is a separate validation failure and cannot overwrite the
correctness verdict.

## 9. Current coverage and implementation order

The current live family contains five real-sandbox tests:

| Existing catalog id | What it proves | Conformance status |
| --- | --- | --- |
| `phase0.a67bb80023d113fed655fbfc` | aggregate snapshot includes the ready sandbox | public-contract smoke only |
| `phase0.003f7b03d5969c5ab9752a4b` | scoped snapshot selects the requested sandbox | public-contract smoke only |
| `observability.cgroup.proc-topology` | public cgroup topology and degraded contract | topology smoke only |
| `observability.cgroup.workspace-runner-placement` | live workspace runner cgroup placement | topology smoke only |
| `observability.snapshot.bounded-memory-history` | 12 polls over 67,500 records grow container memory by no more than 50 MiB | coarse regression; not qualification |

They do not yet prove an idle slope, polling purity, enabled/disabled overhead,
strict disk cap, bounded artifact production, storage-failure isolation, ring
cleanup, or workload GC isolation.

Implement in this order so each layer supplies reusable evidence for the next:

1. bounded sampler, streaming analyzer, artifact cap, and their harness tests;
2. RI-SMOKE and RI-03 to replace the coarse 50 MiB regression;
3. RI-01 and RI-02 nightly qualification;
4. DS-01 and DS-02 disk/read-purity qualification;
5. serialized generated-config fixture and RI-04;
6. isolated fault primitive, DS-03..DS-05;
7. GC-01 and the release soak.

The product is not resource-isolation compliant until the packaged daemon
passes the required live tier. A unit-test pass or the existing 50 MiB smoke
allowance is not sufficient.
