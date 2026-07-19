# Daemon disk polling live E2E specification

Status: Feature acceptance specification; admission-red until implementation
Product contract:
[`sandbox-observability-daemon-disk-polling-spec.md`](../../../ephemeral-sandbox/docs/sandbox-observability-daemon-disk-polling-spec.md)

## 1. Purpose

This focused live suite proves that ordinary selected-sandbox monitoring calls
the public sandbox-scoped `observability.resources` route, reaches the packaged
daemon, receives resource statistics read from daemon disk, and
does not turn completed polls into retained memory or additional telemetry.
It also proves pre-append rotation under concurrent polling.

“Memory-free” means zero retained history with bounded request-time memory. A
request still needs finite decode and transport buffers.

## 2. Live boundary

Both cases:

- use a real Docker sandbox and the packaged daemon;
- invoke product reads only through `sandbox-observability-cli` via the shared
  public CLI harness;
- use Docker only to install deterministic history and measure `/proc` or file
  state;
- configure only supported production limits under a run-owned generated
  gateway configuration;
- retain bounded summary evidence and destroy every exact run-owned sandbox;
  and
- assert response content, disk state, and process state rather than log text.

Direct daemon RPC, browser mocks, dry waiting, allocator trimming, reclaim, and
daemon restart are forbidden as evidence.

## 3. Time budget

| Budget | Hard limit |
|---|---:|
| DP-01 | 110 seconds |
| DP-02 | 110 seconds |
| Sum of declared case budgets | 220 seconds |
| Focused pytest session | 600 seconds |

The required command is:

```bash
E2E_REBUILD_BINARY=0 python3 -m pytest \
  observability/resource_isolation/test_daemon_disk_polling.py \
  --timeout=120 --session-timeout=600
```

`pytest-timeout` interrupts any case at 120 seconds even if its declaration is
accidentally weakened. The session limit is checked between cases; because
each case has the smaller hard limit, the complete focused run cannot consume
an unbounded final case. A cold product compilation is a build prerequisite,
not part of the focused qualification.

There are no sleep-based observation phases. DP-01 fills every measurement
interval with public polling. DP-02 polls and fingerprints continuously while
the normal sampler crosses a rotation boundary.

## 4. Deterministic resource history

Fixtures are streamed to files and copied into:

```text
/eos/runtime/daemon/observability/resources.ndjson
/eos/runtime/daemon/observability/resources.ndjson.1
```

Each file is valid newline-delimited JSON, every line is at most 16 KiB, and
fixtures are generated without constructing a history-sized Python value. A
unique `fixture_marker` inside the newest sample is the source oracle. The
public response must return that exact marker and `source: daemon_disk`; a
manager-owned ring or live sample cannot satisfy both assertions.

## 5. DP-01: disk source, read purity, and bounded memory

Configuration:

```yaml
observability:
  resource_stats:
    enabled: true
    sample_interval_ms: 600000
    max_disk_bytes: 1048576
```

The long supported interval separates the writer from the read-only proof.
After installing a 64-record active history, the case performs 24 warm-up
polls, records daemon anonymous memory, then performs 192 public polls at fixed
four-request concurrency. It samples `/proc` between poll batches; it never
sleeps to create those checkpoints.

Required validations:

- `daemon-disk-source`: every `resources` poll reports `source: daemon_disk`, and the
  newest resource metrics contain the installed marker;
- `read-only-store`: active and rotated file existence, size, allocated
  blocks, inode, nanosecond mtime, line counts, and SHA-256 are identical before
  and after the 192-poll load;
- `response-bounded`: every response is at most 500 records and 256 KiB;
- `poll-memory-bounded`: above the post-warm-up baseline, peak daemon
  `Anonymous` is at most 2 MiB, final `Anonymous` is at most 1 MiB, and no
  anonymous huge page appears; and
- `load-budget`: all 192 measured polls complete in at most 60 seconds.

The test retains only bounded response facts and one digest, never the response
history.

## 6. DP-02: strict rotation under polling load

Configuration uses a 1 MiB total resource budget and the normal two-second
sample interval. The case first drives public resource polls until it observes
a normal daemon-disk sample. It then installs a full 512 KiB rotated segment
and an active segment with less than one encoded-record slot remaining.

Without sleeping, the case continues public polling and bounded file
fingerprinting until the independent sampler appends. The append must remove
the old rotated file and atomically rename the previous active inode to the
rotated path before writing the new active record.

Required validations:

- `pre-append-rotation`: the pre-append active inode becomes the rotated inode;
- `strict-total-cap`: every observation has each segment at most 512 KiB and
  combined logical bytes at most 1 MiB;
- `segments-parseable`: complete lines parse, no oversized middle line or
  partial tail remains, and allocated blocks stay within one filesystem block
  per segment of the logical cap;
- `polling-remains-disk-backed`: every response during the rotation load keeps
  the daemon-disk origin marker; and
- `rotation-load-budget`: rotation is observed within 20 seconds of active
  polling.

## 7. Failure meaning

The suite is an admission test for an implementation-ready route. On the
current manager-ring implementation, DP-01 is expected to fail at the
origin/fixture oracle rather than silently pass against the wrong architecture.
The test must not be marked `xfail`, skipped for an architecture mismatch, or
weakened to accept manager-owned series.
