# Disk-only observability state live E2E specification

Status: Draft; cases described here are not yet implemented.
Required root:
`/Users/yifanxu/Ephemeral-AI-Lab/ephemeral-sandbox-test/e2e/observability`

## Relationship to existing specifications

This is a new, additive live-test specification. It does not replace or modify
[`test_spec.md`](test_spec.md). That document owns the broader resource-
isolation catalog. This document owns the focused admission proof for the
disk-only, zero-retained-history contract in
[`sandbox-observability-disk-only-state-spec.md`](../../../ephemeral-sandbox/docs/sandbox-observability-disk-only-state-spec.md).

## 1. Purpose

The suite proves, using the packaged Linux daemon in a real Docker sandbox,
that:

- daemon and manager retain no observability history in process memory;
- historical telemetry exists only in hard-capped files;
- query memory is independent of persisted history size;
- polling is read-only and does not wake the daemon for host resource data;
- disk exhaustion and corruption degrade observability without harming runtime
  operations; and
- teardown removes every run-owned resource-history file.

“Memory-free” means zero retained history after an operation and bounded
transient memory during that operation. It does not mean zero process memory.

## 2. Required implementation location

New test modules and helpers must be committed below:

```text
e2e/observability/disk_only_state/
  __init__.py
  conftest.py
  helpers.py
  test_incident_history.py
  test_idle_polling.py
  test_event_disk_cap.py
  test_resource_ring.py
  test_failure_modes.py
  test_workload_gc.py
```

An equivalent test in the product repository, a temporary script, another E2E
family, or a browser-only suite does not satisfy this contract.

## 3. Live boundary

Every case must:

- create and destroy sandboxes with the shared lifecycle fixtures;
- run the packaged daemon installed by the normal Docker gateway flow;
- invoke product behavior only through `sandbox-manager-cli`,
  `sandbox-runtime-cli`, and `sandbox-observability-cli` via the shared CLI
  harness;
- declare a stable `@e2e_test` id, feature set, validation checkpoints,
  execution surface, markers, and timeout;
- use Docker and `/proc` only for measurement or deterministic fixture
  installation;
- avoid assertions based on daemon or gateway log text;
- mutate only run-owned configuration, sandbox ids, filesystems, quotas, and
  paths; and
- preserve bounded evidence before cleanup on both pass and failure.

Direct daemon RPC/HTTP observability requests are forbidden. Tests must pass
through the same public gateway path used by operators and the console.

## 4. Test-process memory and artifact rules

The harness must not reproduce the product defect in Python.

- Generate history one line at a time; never multiply a record into one large
  string.
- Stream samples directly to JSONL and flush each sample.
- Do not use `mmap`, `Path.read_text`, `readlines`, or unbounded subprocess
  capture for telemetry and evidence files.
- Hash and count files in fixed chunks.
- Analyze artifacts with online statistics or a streaming second pass.
- Retain at most 2,048 sampled points per metric in memory.
- Limit each case's uncompressed artifacts to 32 MiB.
- Never retain repeated CLI response bodies; retain hashes, counts, extrema,
  and one bounded failure example.

The helper package has a non-Docker unit test that processes ten million
synthetic measurements and proves peak Python allocation is independent of the
input count within a 2 MiB tolerance.

## 5. Measurement contract

Sample at one-second monotonic cadence unless a case declares a five-second
release-soak cadence.

### Daemon process

Read from `/proc/1` after first proving container PID 1 is the packaged daemon:

- `smaps_rollup`: `Rss`, `Pss`, `Anonymous`, `Private_Dirty`, and
  `AnonHugePages`;
- `stat`: user and system CPU ticks;
- `io`: read/write characters, syscall counts, and storage bytes;
- open file-descriptor count.

### Sandbox cgroup

Collect:

- `memory.current`;
- `memory.stat`: `anon`, `file`, `kernel`, `kernel_stack`, `pagetables`, `sock`,
  `slab`, and `anon_thp`;
- CPU and I/O counters;
- PID count.

### Gateway/manager process

For manager cases, resolve the process from the run-owned gateway handle. Do
not select it by process name or by choosing an arbitrary matching PID. Collect
anonymous/private memory, CPU, I/O, file-descriptor count, and the set of open
run-owned resource-ring files.

### Disk state

For active, rotated, and manager-ring files, record:

- existence and path relative to the run root;
- logical bytes and allocated blocks;
- inode and nanosecond modification time;
- streaming SHA-256;
- complete parseable line count and partial-tail state for NDJSON;
- ring header, record size, capacity, next index, and valid count.

Unavailable required Linux fields fail nightly and release cases. They may
produce an explicit capability skip only in a declared developer smoke
environment.

## 6. Stable case catalog

| Case | `@e2e_test` id | Tier | Timeout | Required validations |
|---|---|---|---:|---|
| DOS-00 | `observability.disk-only.smoke` | smoke | 8 min | `idle-store-pure`, `memory-coarse`, `artifact-bounded` |
| DOS-01 | `observability.disk-only.incident-history` | nightly | 60 min | `input-size-independent`, `response-bounded`, `post-response-released`, `store-pure` |
| DOS-02 | `observability.disk-only.idle-polling` | nightly | 2 h | `daemon-idle`, `polling-read-only`, `no-anon-thp`, `ring-only-resource-history` |
| DOS-03 | `observability.disk-only.event-cap` | nightly config | 60 min | `strict-total-cap`, `pre-append-rotation`, `segments-parseable` |
| DOS-04 | `observability.disk-only.resource-ring` | release config | 4 h | `fixed-ring-size`, `manager-history-zero`, `destroy-removes-ring` |
| DOS-05 | `observability.disk-only.storage-failure` | release config | 60 min | `runtime-fail-open`, `drop-count-bounded`, `no-retry-storm` |
| DOS-06 | `observability.disk-only.legacy-migration` | release config | 60 min | `migration-memory-bounded`, `migration-disk-nonincreasing`, `post-migration-cap` |
| DOS-07 | `observability.disk-only.workload-gc` | release | 4 h | `no-oom`, `gc-regression-bounded`, `daemon-memory-gates` |

All cases use `execution_surface="cli"`. Configuration-owning cases serialize
gateway custody and restore the baseline in a finalizer.

## 7. Case definitions

### DOS-00: smoke

Create one sandbox and warm it for one minute. Fingerprint its event store,
leave it idle for two minutes, then request public scoped snapshots and manager
resources once per second for two minutes.

Pass conditions:

- every response is structured and the sandbox remains ready;
- event-store fingerprints do not change;
- daemon `Anonymous` grows by no more than 1 MiB;
- `AnonHugePages` and `anon_thp` remain zero;
- ring length never exceeds 64 KiB; and
- artifacts remain within their cap.

This is a gross regression check, not release qualification.

### DOS-01: incident-sized history

Run three nightly repetitions. Generate these fixtures as streams:

1. one valid event record;
2. a store just below the 4 MiB v2 default;
3. a legacy incident store containing approximately 56,000 records, with an
   8 MiB rotated segment and a 4 MiB active segment.

Install each fixture with `docker cp` after daemon warmup. Fixture installation
is out-of-band measurement setup; all reads use public observability CLI routes.
Exercise events, raw, trace-by-id, latest trace, and scoped snapshot at least
twelve times for each fixture.

For every fixture:

- encoded output is at most 256 KiB and 500 records;
- peak daemon `Anonymous` above the settled pre-query median is at most 1 MiB;
- peak memory differs between the 4 MiB and 12 MiB fixtures by at most 64 KiB;
- five minutes after the last response, `Anonymous` is within 128 KiB of the
  pre-query median;
- `AnonHugePages` and `anon_thp` remain zero; and
- both input segment fingerprints are unchanged by every read.

The old whole-file reader must fail this case because its memory step scales
with the 12 MiB input. The test must not treat a later append or migration as
evidence that the read itself was bounded.

### DOS-02: idle and polling purity

Use paired target and idle-control sandboxes for three repetitions. Warm both
for five minutes. Leave the control untouched. Against the target, alternate
public manager status, aggregate snapshot, scoped snapshot, events, trace, and
manager resource routes at the console's active cadence for thirty minutes,
then allow ten minutes of cooldown.

Pass conditions for each repetition:

- idle-phase daemon `Anonymous` Theil-Sen slope is at most 4 KiB/hour;
- final-five-minute minus first-five-minute median is at most 64 KiB;
- target-minus-control post-cooldown median is at most 128 KiB;
- daemon event-store fingerprints remain identical;
- daemon storage I/O does not advance during manager status/resource polling;
- daemon CPU differs from control by less than one scheduler tick per minute;
- manager resource rings remain fixed at or below 64 KiB; and
- no anonymous huge pages appear.

The product integration suite separately proves exact zero manager-to-daemon
invocations. This live case proves the external memory, CPU, I/O, and disk
consequence.

### DOS-03: strict event-store cap

Use a generated configuration with `max_disk_bytes: 1048576`. Stream valid
fixtures to exact-fit, one-byte-under, and one-byte-over boundaries, then
produce real events through public runtime operations. Observe at least ten
rotations and sample both files after every operation.

Pass conditions:

```text
active.len <= 524288
rotated.len <= 524288
active.len + rotated.len <= 1048576
```

The bound must hold after every append, not only after a later collector pass.
Every complete line parses, no middle line is partial, and allocated blocks are
within one filesystem block per segment of the logical cap. Oversized escaped
and multibyte records produce one bounded marker or one counted drop.

### DOS-04: manager resource ring

Use a generated manager registry under a run-owned directory. Create 100
sandboxes, register each id immediately, and wait through the declared ring
wrap interval.

Pass conditions:

- exactly one ring exists per live sandbox;
- every ring is at most 64 KiB and aggregate logical bytes are at most
  `sandbox_count * 64 KiB`;
- no unrelated ring is changed;
- after initial population, gateway/manager anonymous memory has no trend with
  sample or wrap count and its final median is within 1 MiB of the initial
  settled median;
- test instrumentation exposes zero retained decoded resource records; and
- destroying the sandboxes removes exactly their rings.

The exact zero-record assertion also exists as a deterministic product test.
The live memory gate is intentionally coarser because the gateway owns other
bounded lifecycle state for the 100 live sandboxes.

### DOS-05: isolated storage failure

Give only the current sandbox event store a test-owned tiny filesystem or
quota. Never fill Docker's global disk, the repository, `/tmp`, or a shared host
volume. Force `ENOSPC`, then run public runtime commands.

Commands must succeed with bounded latency. Daemon CPU and I/O must show no
tight retry loop, store bytes must stay within quota, and one fixed-width drop
counter must account for failed records without an in-memory error queue.

Developer Docker Desktop may skip only when it records the missing isolation
primitive. The release Linux lane must provide the primitive; a skip there is a
failure.

### DOS-06: legacy migration

Install the 12 MiB incident fixture before the migration trigger. Capture
logical and allocated bytes continuously while starting the packaged v2 daemon
or issuing the first event-producing operation, according to the implemented
migration boundary.

Pass conditions:

- peak daemon anonymous memory is at most 1 MiB above its settled baseline;
- disk usage never increases above the pre-migration footprint;
- no third full-size segment appears;
- after success, total event bytes are within the configured cap;
- if telemetry is dropped, runtime operations still succeed; and
- subsequent reads satisfy DOS-01.

### DOS-07: colocated workload GC

Run five alternating enabled/disabled repetitions of a pinned Node.js
allocation workload under identical tight cgroup limits. Stream GC and event-
loop measurements to bounded JSONL.

Pass conditions:

- neither arm OOMs;
- enabled p99 GC pause is no more than disabled p99 plus 1 ms;
- enabled p99 event-loop delay is no more than 5% above disabled plus 1 ms;
- daemon gates from DOS-02 continue to pass; and
- event and resource storage remain within their declared budgets.

## 8. Analysis rules

Use `smaps_rollup Anonymous` and cgroup `memory.stat anon` for anonymous-memory
gates. RSS is diagnostic because it includes reclaimable file-backed pages.

For every steady phase:

1. discard a predeclared warmup interval;
2. compute a fixed-seed sampled Theil-Sen slope;
3. compute first and final medians with a bounded reservoir;
4. report a fixed-seed bootstrap 95% confidence interval; and
5. apply hard gates to each repetition separately.

An average cannot hide a failing repetition. An automatic rerun cannot convert
a failed memory or disk gate into a pass.

## 9. Artifacts

Each case writes below a run-owned directory such as:

```text
.e2e-state/observability/<run-id>/<case-id>/
  environment.json
  samples.jsonl
  store-before.json
  store-after.json
  summary.json
  gc.jsonl                  # DOS-07 only
```

`summary.json` records all limits, measured peaks, slopes, medians, confidence
intervals, storage maxima, response maxima, cleanup status, daemon build id,
Docker/kernel versions, cgroup mode, and the final verdict.

Artifact-cap exhaustion is a test failure. It is not permission to delete
another run's evidence.

## 10. Scheduling

| Tier | Cases | Frequency |
|---|---|---|
| Per-change smoke | Existing observability smoke plus DOS-00 | Every observability change |
| Nightly | DOS-01 through DOS-03, three repetitions where declared | Nightly |
| Release | DOS-04 through DOS-07, five A/B repetitions where declared | Before release |
| Release soak | Six-hour DOS-03 producer and DOS-04 cleanup scale | Scheduled release lane |

Memory-comparison cases run without other sandbox-producing workers on the
same Docker daemon. Missing more than 1% of sampler deadlines invalidates the
phase as an environmental failure rather than a product pass.

## 11. Cleanup

- Register every sandbox immediately after successful creation.
- Harvest bounded evidence before destruction even after an assertion failure.
- Destroy sandboxes in reverse creation order.
- Restore baseline gateway configuration in a finalizer.
- Delete only generated files whose resolved parent is the current run root.
- Never use broad Docker prune, broad container deletion, or shared-directory
  cleanup.

Cleanup failure is an independent validation failure and does not overwrite the
product verdict.

## 12. Admission rule

The product is not disk-only observability compliant until:

- the new test modules exist under `e2e/observability/disk_only_state`;
- DOS-00 passes against the packaged binary on every observability change;
- DOS-01 through DOS-03 pass in three consecutive nightly repetitions;
- DOS-04 through DOS-07 pass in the release environment;
- all run-owned sandboxes, rings, quotas, and configuration are cleaned up; and
- the final bounded evidence is retained with an explicit passing verdict.

Passing unit tests, the existing coarse 50 MiB regression allowance, or a mock
manager/daemon test is not a substitute for this live proof.
