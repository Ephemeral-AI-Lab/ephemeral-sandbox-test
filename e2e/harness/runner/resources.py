"""Best-effort, case-bound runtime resource evidence.

The public helpers in this module are deliberately small.  Product boundary
adapters tell the collector about lifecycle/operation events; one private
scheduler thread owns sampling, serialization, and finalization.  Sampling
never calls the instrumented E2E CLI wrapper.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Mapping


SCHEMA_VERSION = 1
SAMPLE_INTERVAL_MS = 1_000
QUERY_TIMEOUT_SECONDS = 0.75
FINAL_HARVEST_SECONDS = 3.0
WORKSPACE_WINDOW_MS = 600_000
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_SCOPES = 20
MAX_ERRORS = 10
MAX_MESSAGE = 512
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PHASES = frozenset({"setup", "call", "teardown"})
_METRICS = frozenset(
    {
        "cpu_usec",
        "mem_cur",
        "mem_max",
        "mem_max_unlimited",
        "io_rbytes",
        "io_wbytes",
        "disk_bytes",
        "disk_allocated_bytes",
        "files",
        "disk_truncated",
    }
)
_COUNTERS = ("cpu_usec", "io_rbytes", "io_wbytes")
_MESSAGES = {
    "query_timeout": "Runtime resource sampling timed out.",
    "query_failed": "Runtime resource sampling failed.",
    "malformed_response": "Runtime resource sampling returned malformed structured data.",
    "unsupported": "Runtime resource sampling is unsupported by this product build.",
    "counter_reset": "A resource counter reset; a new series segment was started.",
    "timestamp_reset": "A resource timestamp did not increase; a new series segment was started.",
    "sampling_late": "The collector skipped a late sampling interval.",
    "cgroup_unavailable": "Cgroup metrics were unavailable for this scope.",
    "final_harvest_timeout": "The bounded final resource harvest expired.",
    "cap_reached": "The runtime evidence size limit was reached.",
    "child_interrupted": "The pytest child stopped before normal resource finalization.",
    "artifact_write_failed": "The runtime evidence file could not be finalized.",
    "torn_final_line": "An incomplete final evidence line was discarded during recovery.",
    "invalid_record": "An invalid evidence record ended the recoverable prefix.",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_id(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_ID.fullmatch(value):
        return value
    return None


def _case_id(value: Any) -> str | None:
    if isinstance(value, str) and value and len(value) <= MAX_MESSAGE and "\0" not in value:
        return value
    return None


def case_key(test_id: str, case_id: str, attempt_id: str) -> str:
    return hashlib.sha256(f"{test_id}\0{case_id}\0{attempt_id}".encode()).hexdigest()


def _json_line(record: Mapping[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


@dataclass
class _ScopeSummary:
    sandbox_id: str
    kind: str
    scope_id: str
    source: str
    sample_count: int = 0
    cpu_peak_cores: float | None = None
    cpu_time_seconds: float = 0.0
    cpu_delta_count: int = 0
    memory_peak_bytes: int | None = None
    memory_limit_bytes: int | None = None
    memory_limit_unlimited: bool = False
    memory_limit_observed: bool = False
    io_read_bytes: int = 0
    io_read_delta_count: int = 0
    io_write_bytes: int = 0
    io_write_delta_count: int = 0
    disk_peak_bytes: int | None = None
    disk_allocated_peak_bytes: int | None = None
    file_peak: int | None = None
    disk_truncated: bool = False
    disk_truncated_observed: bool = False

    def update(self, record: Mapping[str, Any]) -> None:
        self.sample_count += 1
        metrics = record.get("metrics", {})
        derived = record.get("derived", {})
        delta = record.get("delta", {})
        cores = derived.get("cpu_cores")
        if isinstance(cores, (int, float)):
            self.cpu_peak_cores = max(self.cpu_peak_cores or 0.0, float(cores))
        cpu_delta = delta.get("cpu_usec")
        if isinstance(cpu_delta, int):
            self.cpu_time_seconds += cpu_delta / 1_000_000
            self.cpu_delta_count += 1
        for key, attr in (
            ("mem_cur", "memory_peak_bytes"),
            ("disk_bytes", "disk_peak_bytes"),
            ("disk_allocated_bytes", "disk_allocated_peak_bytes"),
            ("files", "file_peak"),
        ):
            value = metrics.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                prior = getattr(self, attr)
                setattr(self, attr, max(prior or 0, value))
        limit = metrics.get("mem_max")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            self.memory_limit_bytes = limit
            self.memory_limit_observed = True
        if isinstance(metrics.get("mem_max_unlimited"), bool):
            self.memory_limit_unlimited |= metrics["mem_max_unlimited"]
            self.memory_limit_observed = True
        if isinstance(metrics.get("disk_truncated"), bool):
            self.disk_truncated |= metrics["disk_truncated"]
            self.disk_truncated_observed = True
        for key, attr, count_attr in (
            ("io_rbytes", "io_read_bytes", "io_read_delta_count"),
            ("io_wbytes", "io_write_bytes", "io_write_delta_count"),
        ):
            value = delta.get(key)
            if isinstance(value, int) and value >= 0:
                setattr(self, attr, getattr(self, attr) + value)
                setattr(self, count_attr, getattr(self, count_attr) + 1)

    def value(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "sandbox_id": self.sandbox_id,
            "scope": {"kind": self.kind, "id": self.scope_id},
            "source": self.source,
            "sample_count": self.sample_count,
        }
        if self.cpu_delta_count:
            value["cpu_time_seconds"] = round(self.cpu_time_seconds, 6)
        if self.io_read_delta_count:
            value["io_read_bytes"] = self.io_read_bytes
        if self.io_write_delta_count:
            value["io_write_bytes"] = self.io_write_bytes
        if self.memory_limit_observed:
            value["memory_limit_unlimited"] = self.memory_limit_unlimited
        if self.disk_truncated_observed:
            value["disk_truncated"] = self.disk_truncated
        for name in (
            "cpu_peak_cores",
            "memory_peak_bytes",
            "memory_limit_bytes",
            "disk_peak_bytes",
            "disk_allocated_peak_bytes",
            "file_peak",
        ):
            item = getattr(self, name)
            if item is not None:
                value[name] = round(item, 6) if isinstance(item, float) else item
        return value


@dataclass
class _Case:
    run_id: str
    test_id: str
    case_id: str
    attempt_id: str
    root: Path
    started_mono: float = field(default_factory=time.monotonic)
    started_at: str = field(default_factory=_utc_now)
    phase: str = "setup"
    tracked: set[str] = field(default_factory=set)
    seen_sandboxes: set[str] = field(default_factory=set)
    workspaces: dict[str, set[str]] = field(default_factory=dict)
    previous: dict[tuple[str, str, str], tuple[int, dict[str, Any], float]] = field(default_factory=dict)
    scopes: dict[tuple[str, str, str], _ScopeSummary] = field(default_factory=dict)
    pending: list[dict[str, Any]] = field(default_factory=list)
    errors: Counter = field(default_factory=Counter)
    observed_ticks: int = 0
    sample_count: int = 0
    operation_count: int = 0
    gap_count: int = 0
    capped: bool = False
    stream: Any = None
    part_path: Path | None = None
    final_path: Path | None = None
    bytes_written: int = 0

    @property
    def key(self) -> str:
        return case_key(self.test_id, self.case_id, self.attempt_id)

    def offset(self) -> float:
        return round((time.monotonic() - self.started_mono) * 1000.0, 3)

    def ensure_stream(self) -> None:
        if self.stream is not None:
            return
        directory = self.root / "evidence" / "runtime"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        self.part_path = directory / f"{self.key}.ndjson.part"
        self.final_path = directory / f"{self.key}.ndjson"
        fd = os.open(self.part_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.stream = os.fdopen(fd, "wb")
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "kind": "metadata",
            "offset_ms": 0,
            "run_id": self.run_id,
            "test_id": self.test_id,
            "case_id": self.case_id,
            "attempt_id": self.attempt_id,
            "started_at": self.started_at,
            "sample_interval_ms": SAMPLE_INTERVAL_MS,
        }
        # Keep metadata first while batching it with the immediate sample into
        # the track tick's single flush/fsync.
        self.pending.insert(0, metadata)

    def add_gap(
        self,
        reason: str,
        *,
        sandbox_id: str | None = None,
        scope: Mapping[str, str] | None = None,
        from_offset_ms: float | None = None,
    ) -> None:
        reason = reason if reason in _MESSAGES else "query_failed"
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "kind": "gap",
            "offset_ms": self.offset(),
            "reason_code": reason,
            "message": _MESSAGES[reason][:MAX_MESSAGE],
        }
        if sandbox_id is not None:
            record["sandbox_id"] = sandbox_id
        if scope is not None:
            record["scope"] = dict(scope)
        if from_offset_ms is not None:
            record["from_offset_ms"] = round(from_offset_ms, 3)
            record["to_offset_ms"] = record["offset_ms"]
        self.pending.append(record)
        self.errors[reason] += 1
        self.gap_count += 1

    def flush(self) -> None:
        if not self.pending or self.stream is None:
            return
        records, self.pending = self.pending, []
        self._write(records)

    def _write(self, records: list[Mapping[str, Any]]) -> None:
        if self.capped or self.stream is None:
            return
        lines = [_json_line(record) for record in records]
        chosen: list[bytes] = []
        used = 0
        for line in lines:
            if self.bytes_written + used + len(line) >= MAX_ARTIFACT_BYTES:
                break
            chosen.append(line)
            used += len(line)
        if len(chosen) != len(lines):
            cap = _json_line(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "gap",
                    "offset_ms": self.offset(),
                    "reason_code": "cap_reached",
                    "message": _MESSAGES["cap_reached"],
                }
            )
            if self.bytes_written + used + len(cap) < MAX_ARTIFACT_BYTES:
                chosen.append(cap)
                used += len(cap)
            self.errors["cap_reached"] += 1
            self.gap_count += 1
            self.capped = True
        if chosen:
            self.stream.writelines(chosen)
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.bytes_written += used


@dataclass
class _Request:
    action: str
    values: tuple[Any, ...]
    done: threading.Event | None = None
    result: Any = None


class ResourceCollector:
    """One scheduler/serializer for the serial pytest child."""

    def __init__(self, manifest: Mapping[str, Any], run_root: Path, binary: Path):
        attempts = manifest.get("attempt_ids") or []
        self.run_id = _safe_id(manifest.get("run_id")) or "run-unknown"
        self.attempt_id = _safe_id(attempts[0] if attempts else None) or "attempt-unknown"
        self.run_root = run_root
        self.binary = binary
        self._condition = threading.Condition()
        self._requests: deque[_Request] = deque()
        self._stopping = False
        self._case: _Case | None = None
        self._next_tick = time.monotonic() + 1.0
        self._thread = threading.Thread(target=self._loop, name="runtime-resource-collector", daemon=True)
        self._thread.start()

    def request(self, action: str, *values: Any, wait: bool = False, timeout: float = 4.0) -> Any:
        done = threading.Event() if wait else None
        request = _Request(action, values, done)
        with self._condition:
            self._requests.append(request)
            self._condition.notify()
        if done is None:
            return None
        done.wait(timeout)
        return request.result

    def close(self) -> None:
        self.request(
            "stop",
            wait=True,
            timeout=FINAL_HARVEST_SECONDS + QUERY_TIMEOUT_SECONDS + 1.0,
        )
        self._thread.join(timeout=1.0)

    def _loop(self) -> None:
        while True:
            request: _Request | None = None
            with self._condition:
                timeout = max(0.0, self._next_tick - time.monotonic())
                if not self._requests and not self._stopping:
                    self._condition.wait(timeout)
                if self._requests:
                    request = self._requests.popleft()
            if request is not None:
                try:
                    request.result = self._handle(request.action, *request.values)
                except Exception:
                    request.result = None
                finally:
                    if request.done is not None:
                        request.done.set()
                if request.action == "stop":
                    return
                continue
            now = time.monotonic()
            if now >= self._next_tick:
                late_from = self._next_tick
                self._next_tick = now + 1.0
                if self._case is not None and self._case.tracked:
                    if now - late_from > 1.5:
                        self._case.add_gap("sampling_late", from_offset_ms=max(0.0, self._case.offset() - (now - late_from) * 1000))
                    self._sample_tracked()

    def _handle(self, action: str, *values: Any) -> Any:
        if action == "begin":
            if self._case is not None:
                self._finalize()
            test_id, case_id = _safe_id(values[0]), _case_id(values[1])
            if not test_id or not case_id:
                return None
            self._case = _Case(self.run_id, test_id, case_id, self.attempt_id, self.run_root)
            self._next_tick = time.monotonic() + 1.0
        elif action == "phase" and self._case is not None:
            phase, edge = values
            if phase in _PHASES:
                self._case.phase = phase
                self._operation("pytest", f"phase.{phase}", edge=edge)
        elif action == "operation" and self._case is not None:
            self._operation(*values)
        elif action == "workspace" and self._case is not None:
            sandbox_id, workspace_id = map(_safe_id, values)
            if sandbox_id and workspace_id and sandbox_id in self._case.seen_sandboxes:
                self._case.workspaces.setdefault(sandbox_id, set()).add(workspace_id)
        elif action == "track" and self._case is not None:
            sandbox_id = _safe_id(values[0])
            if sandbox_id:
                self._case.seen_sandboxes.add(sandbox_id)
                self._case.tracked.add(sandbox_id)
                self._case.ensure_stream()
                self._operation(
                    "cleanup",
                    "sandbox.registered",
                    edge="marker",
                    sandbox_id=sandbox_id,
                )
                self._sample_one(sandbox_id, "sandbox", "sandbox")
                self._case.observed_ticks += 1
                self._case.flush()
        elif action == "untrack" and self._case is not None:
            sandbox_id = _safe_id(values[0])
            if sandbox_id and sandbox_id in self._case.tracked:
                self._harvest(sandbox_id, time.monotonic() + FINAL_HARVEST_SECONDS)
                self._case.tracked.discard(sandbox_id)
                self._case.flush()
        elif action == "finalize":
            return self._finalize(failure_phase=values[0] if values else None)
        elif action == "stop":
            self._stopping = True
            return self._finalize()
        return None

    def _operation(
        self,
        surface: str,
        operation: str,
        edge: str = "finish",
        duration_ms: float | None = None,
        returncode: int | None = None,
        sandbox_id: str | None = None,
        workspace_id: str | None = None,
    ) -> None:
        case = self._case
        if case is None:
            return
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", surface or ""):
            return
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", operation or ""):
            return
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "kind": "operation",
            "offset_ms": case.offset(),
            "phase": case.phase,
            "edge": edge if edge in {"start", "finish", "marker"} else "finish",
            "surface": surface,
            "operation": operation,
        }
        sandbox_id = _safe_id(sandbox_id)
        workspace_id = _safe_id(workspace_id)
        if sandbox_id:
            record["sandbox_id"] = sandbox_id
        if workspace_id:
            record["workspace_id"] = workspace_id
        if isinstance(duration_ms, (int, float)):
            record["duration_ms"] = round(max(0.0, min(float(duration_ms), 86_400_000.0)), 3)
        if isinstance(returncode, int):
            record["returncode"] = max(-255, min(returncode, 255))
        case.pending.append(record)
        case.operation_count += 1

    def _sample_tracked(self) -> None:
        case = self._case
        if case is None:
            return
        for sandbox_id in sorted(case.tracked):
            self._sample_one(sandbox_id, "sandbox", "sandbox")
            # Lifecycle requests must not wait behind the rest of a slow
            # multi-sandbox tick. The current query is already bounded; stop
            # here so final harvest can run before cleanup proceeds.
            with self._condition:
                if any(
                    request.action in {"untrack", "finalize", "stop"}
                    for request in self._requests
                ):
                    break
        case.observed_ticks += 1
        case.flush()

    def _harvest(self, sandbox_id: str, deadline: float) -> None:
        case = self._case
        if case is None:
            return
        self._operation(
            "cleanup",
            "sandbox.final_harvest",
            edge="marker",
            sandbox_id=sandbox_id,
        )
        self._sample_one(sandbox_id, "sandbox", "sandbox", deadline=deadline)
        if time.monotonic() < deadline:
            _, reason = self._query(
                ["snapshot", "--sandbox-id", sandbox_id],
                deadline=deadline,
                expect_series=False,
            )
            if reason:
                case.add_gap(reason, sandbox_id=sandbox_id)
        for workspace_id in sorted(case.workspaces.get(sandbox_id, ())):
            if time.monotonic() >= deadline:
                case.add_gap("final_harvest_timeout", sandbox_id=sandbox_id)
                break
            self._sample_one(sandbox_id, "workspace", workspace_id, window_ms=WORKSPACE_WINDOW_MS, deadline=deadline)
        case.observed_ticks += 1

    def _sample_one(
        self,
        sandbox_id: str,
        kind: str,
        scope_id: str,
        *,
        window_ms: int = 0,
        deadline: float | None = None,
    ) -> None:
        case = self._case
        if case is None:
            return
        result, reason = self._query(
            ["cgroup", "--sandbox-id", sandbox_id, "--scope", scope_id, "--window-ms", str(window_ms)],
            deadline=deadline,
            expect_series=True,
        )
        scope = {"kind": kind, "id": scope_id}
        if reason:
            case.add_gap(reason, sandbox_id=sandbox_id, scope=scope)
            return
        series = result.get("series") if isinstance(result, Mapping) else None
        if not isinstance(series, list):
            case.add_gap("malformed_response", sandbox_id=sandbox_id, scope=scope)
            return
        if not series:
            case.add_gap("cgroup_unavailable", sandbox_id=sandbox_id, scope=scope)
            return
        for sample in series:
            self._record_sample(case, sandbox_id, kind, scope_id, sample)

    def _query(
        self,
        argv: list[str],
        *,
        deadline: float | None,
        expect_series: bool,
    ) -> tuple[Mapping[str, Any], str | None]:
        timeout = QUERY_TIMEOUT_SECONDS
        if deadline is not None:
            timeout = min(timeout, max(0.01, deadline - time.monotonic()))
        try:
            proc = subprocess.run(
                [str(self.binary), *argv],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {}, "query_timeout"
        except OSError:
            return {}, "unsupported"
        raw = proc.stdout.strip() or proc.stderr.strip()
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}, "malformed_response"
        if proc.returncode != 0:
            code = value.get("error", {}).get("code") if isinstance(value, Mapping) and isinstance(value.get("error"), Mapping) else None
            return {}, "unsupported" if code in {"unknown_operation", "unsupported"} else "query_failed"
        if isinstance(value, list) and not expect_series:
            return {"nodes": value}, None
        if not isinstance(value, Mapping):
            return {}, "malformed_response"
        return value, None

    def _record_sample(
        self,
        case: _Case,
        sandbox_id: str,
        kind: str,
        scope_id: str,
        sample: Any,
    ) -> None:
        if not isinstance(sample, Mapping):
            case.add_gap("malformed_response", sandbox_id=sandbox_id, scope={"kind": kind, "id": scope_id})
            return
        ts = sample.get("ts")
        metrics_raw = sample.get("metrics")
        if not isinstance(ts, int) or isinstance(ts, bool) or not isinstance(metrics_raw, Mapping):
            case.add_gap("malformed_response", sandbox_id=sandbox_id, scope={"kind": kind, "id": scope_id})
            return
        metrics: dict[str, Any] = {}
        for name in _METRICS:
            value = metrics_raw.get(name)
            if name in {"mem_max_unlimited", "disk_truncated"}:
                if isinstance(value, bool):
                    metrics[name] = value
            elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                metrics[name] = value
        if metrics_raw.get("cgroup_available") is False:
            case.add_gap("cgroup_unavailable", sandbox_id=sandbox_id, scope={"kind": kind, "id": scope_id})
        source = "docker_engine" if kind == "sandbox" else "sandbox_daemon"
        key = (sandbox_id, kind, scope_id)
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "kind": "sample",
            "offset_ms": case.offset(),
            "observed_at": _utc_now(),
            "source_ts_ms": ts,
            "phase": case.phase,
            "sandbox_id": sandbox_id,
            "scope": {"kind": kind, "id": scope_id},
            "source": source,
            "metrics": metrics,
        }
        previous = case.previous.get(key)
        if previous is not None:
            prior_ts, prior_metrics, prior_offset = previous
            wall = ts - prior_ts
            reset = next((name for name in _COUNTERS if name in metrics and name in prior_metrics and metrics[name] < prior_metrics[name]), None)
            if wall <= 0:
                case.add_gap("timestamp_reset", sandbox_id=sandbox_id, scope=record["scope"], from_offset_ms=prior_offset)
            elif reset is not None:
                case.add_gap("counter_reset", sandbox_id=sandbox_id, scope=record["scope"], from_offset_ms=prior_offset)
            elif wall > SAMPLE_INTERVAL_MS * 2.5 and kind == "sandbox":
                case.add_gap("sampling_late", sandbox_id=sandbox_id, scope=record["scope"], from_offset_ms=prior_offset)
            else:
                delta: dict[str, Any] = {"sample_ms": wall}
                for name in _COUNTERS:
                    if name in metrics and name in prior_metrics:
                        delta[name] = metrics[name] - prior_metrics[name]
                record["delta"] = delta
                if "cpu_usec" in delta:
                    record["derived"] = {"cpu_cores": round(delta["cpu_usec"] / (wall * 1000), 6)}
        case.previous[key] = (ts, metrics, record["offset_ms"])
        case.pending.append(record)
        case.sample_count += 1
        summary = case.scopes.setdefault(key, _ScopeSummary(sandbox_id, kind, scope_id, source))
        summary.update(record)

    def _finalize(self, failure_phase: str | None = None) -> dict[str, Any] | None:
        case, self._case = self._case, None
        if case is None:
            return None
        if failure_phase in _PHASES:
            case.phase = failure_phase
            self._case = case
            self._operation("pytest", "case_failure", edge="marker")
            self._case = None
        if not case.seen_sandboxes:
            return _not_applicable(case)
        deadline = time.monotonic() + FINAL_HARVEST_SECONDS
        self._case = case
        for sandbox_id in sorted(case.tracked):
            if time.monotonic() >= deadline:
                case.add_gap("final_harvest_timeout", sandbox_id=sandbox_id)
                break
            self._harvest(sandbox_id, deadline)
        self._case = None
        try:
            case.flush()
            if case.stream is None or case.part_path is None or case.final_path is None:
                return _artifact(case, status="unavailable")
            case.stream.close()
            digest_value = hashlib.sha256(case.part_path.read_bytes()).hexdigest()
            os.replace(case.part_path, case.final_path)
            directory_fd = os.open(case.final_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, ValueError):
            case.errors["artifact_write_failed"] += 1
            case.gap_count += 1
            try:
                if case.stream is not None and not case.stream.closed:
                    case.stream.close()
            except OSError:
                pass
            return _artifact(
                case,
                status="partial" if case.sample_count else "unavailable",
            )
        return _artifact(case, digest_value=digest_value)


def _not_applicable(case: _Case) -> dict[str, Any]:
    return {
        "evidence_id": f"runtime-{case.key[:32]}",
        "kind": "runtime_observability",
        "role": "supporting",
        "availability": "available",
        "status": "not_applicable",
        "reason_code": "no_sandbox_observed",
        "message": "This case did not own a sandbox.",
    }


def _artifact(
    case: _Case,
    status: str | None = None,
    digest_value: str | None = None,
) -> dict[str, Any]:
    if status is None:
        if case.sample_count == 0:
            status = "unsupported" if case.errors and set(case.errors) == {"unsupported"} else "unavailable"
        else:
            status = "partial" if case.errors or case.capped else "available"
    availability = status if status in {"partial", "unavailable", "unsupported", "invalid"} else "available"
    scopes = [summary.value() for _, summary in sorted(case.scopes.items())[:MAX_SCOPES]]
    sandbox_scopes = [scope for scope in scopes if scope["scope"]["kind"] == "sandbox"]
    workspace_scopes = [scope for scope in scopes if scope["scope"]["kind"] == "workspace"]

    def maximum(name: str, entries: list[Mapping[str, Any]]) -> Any:
        values = [entry[name] for entry in entries if isinstance(entry.get(name), (int, float))]
        return max(values) if values else None

    def total(name: str, entries: list[Mapping[str, Any]]) -> int | float | None:
        values = [entry[name] for entry in entries if isinstance(entry.get(name), (int, float))]
        return sum(values) if values else None

    summary: dict[str, Any] = {"scopes": scopes}
    for name in ("cpu_time_seconds", "io_read_bytes", "io_write_bytes"):
        value = total(name, sandbox_scopes)
        if value is not None:
            summary[name] = round(value, 6) if name == "cpu_time_seconds" else value
    if any("memory_limit_unlimited" in scope for scope in sandbox_scopes):
        summary["memory_limit_unlimited"] = any(
            scope.get("memory_limit_unlimited") is True for scope in sandbox_scopes
        )
    if any("disk_truncated" in scope for scope in workspace_scopes):
        summary["workspace_disk_truncated"] = any(
            scope.get("disk_truncated") is True for scope in workspace_scopes
        )
    for output, field, entries in (
        ("cpu_peak_cores", "cpu_peak_cores", sandbox_scopes),
        ("memory_peak_bytes", "memory_peak_bytes", sandbox_scopes),
        ("memory_limit_bytes", "memory_limit_bytes", sandbox_scopes),
        ("workspace_disk_peak_bytes", "disk_peak_bytes", workspace_scopes),
        ("workspace_disk_allocated_peak_bytes", "disk_allocated_peak_bytes", workspace_scopes),
        ("workspace_file_peak", "file_peak", workspace_scopes),
    ):
        value = maximum(field, entries)
        if value is not None:
            summary[output] = value
    duration_ms = max(0.0, case.offset())
    expected = max(1, int(duration_ms // SAMPLE_INTERVAL_MS) + 1)
    artifact: dict[str, Any] = {
        "evidence_id": f"runtime-{case.key[:32]}",
        "kind": "runtime_observability",
        "role": "supporting",
        "availability": availability,
        "status": status,
        "media_type": "application/x-ndjson",
        "sample_count": case.sample_count,
        "operation_count": case.operation_count,
        "gap_count": case.gap_count,
        "summary": summary,
        "coverage": {
            "started_at": case.started_at,
            "ended_at": _utc_now(),
            "sample_interval_ms": SAMPLE_INTERVAL_MS,
            "expected_ticks": expected,
            "observed_ticks": min(case.observed_ticks, expected),
            "missed_ticks": max(0, expected - case.observed_ticks),
            "sandbox_count": len(case.seen_sandboxes),
            "workspace_count": sum(len(ids) for ids in case.workspaces.values()),
        },
        "errors": [
            {"reason_code": reason, "count": count, "message": _MESSAGES.get(reason, _MESSAGES["query_failed"])}
            for reason, count in case.errors.most_common(MAX_ERRORS)
        ],
    }
    if case.final_path is not None and case.final_path.is_file():
        try:
            digest_value = digest_value or hashlib.sha256(case.final_path.read_bytes()).hexdigest()
        except OSError:
            digest_value = None
        if digest_value is not None:
            artifact["storage_ref"] = f"runtime/{case.final_path.name}"
            artifact["sha256"] = f"sha256:{digest_value}"
    return artifact


def _valid_recovery_record(record: Any, identity: Mapping[str, str], first: bool) -> bool:
    if not isinstance(record, Mapping) or record.get("schema_version") != SCHEMA_VERSION:
        return False
    if record.get("kind") not in {"metadata", "sample", "operation", "gap"}:
        return False
    if first:
        return record.get("kind") == "metadata" and all(record.get(name) == value for name, value in identity.items())
    return record.get("kind") != "metadata" and isinstance(record.get("offset_ms"), (int, float))


def recover_artifact(run_root: Path, manifest: Mapping[str, Any], case: Mapping[str, Any]) -> dict[str, Any] | None:
    """Finalize only the deterministic run-owned prefix after child interruption."""
    attempts = manifest.get("attempt_ids") or []
    attempt_id = _safe_id(attempts[0] if attempts else None)
    test_id, case_id = _safe_id(case.get("test_id")), _case_id(case.get("case_id"))
    run_id = _safe_id(manifest.get("run_id"))
    if not all((attempt_id, test_id, case_id, run_id)):
        return None
    key = case_key(test_id, case_id, attempt_id)
    directory = run_root / "evidence" / "runtime"
    final_path = directory / f"{key}.ndjson"
    part_path = directory / f"{key}.ndjson.part"
    path = final_path if final_path.is_file() else part_path
    if not path.is_file():
        return None
    data = path.read_bytes()
    complete = data.endswith(b"\n")
    lines = data.splitlines(keepends=True)
    valid: list[bytes] = []
    identity = {"run_id": run_id, "test_id": test_id, "case_id": case_id, "attempt_id": attempt_id}
    records: list[Mapping[str, Any]] = []
    for index, raw in enumerate(lines):
        if not raw.endswith(b"\n"):
            break
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            break
        if not _valid_recovery_record(record, identity, index == 0):
            break
        valid.append(raw)
        records.append(record)
    if not valid:
        return None
    if path == part_path:
        fd = os.open(part_path, os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.writelines(valid)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part_path, final_path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    synthetic = _Case(run_id, test_id, case_id, attempt_id, run_root)
    synthetic.started_at = str(records[0].get("started_at") or _utc_now())[:40]
    synthetic.final_path = final_path
    for record in records[1:]:
        kind = record.get("kind")
        if kind == "sample":
            synthetic.sample_count += 1
            scope = record.get("scope", {})
            sandbox_id = _safe_id(record.get("sandbox_id"))
            scope_kind, scope_id = scope.get("kind"), _safe_id(scope.get("id"))
            source = record.get("source")
            if sandbox_id and scope_kind in {"sandbox", "workspace"} and scope_id and source in {"docker_engine", "sandbox_daemon"}:
                synthetic.seen_sandboxes.add(sandbox_id)
                key_tuple = (sandbox_id, scope_kind, scope_id)
                synthetic.scopes.setdefault(key_tuple, _ScopeSummary(sandbox_id, scope_kind, scope_id, source)).update(record)
        elif kind == "operation":
            synthetic.operation_count += 1
        elif kind == "gap":
            synthetic.gap_count += 1
            reason = record.get("reason_code")
            if reason in _MESSAGES:
                synthetic.errors[reason] += 1
    synthetic.errors["child_interrupted"] += 1
    if not complete or len(valid) != len(lines):
        synthetic.errors["torn_final_line" if not complete else "invalid_record"] += 1
    return _artifact(synthetic, status="partial" if synthetic.sample_count else "unavailable")


def interrupted_artifact(manifest: Mapping[str, Any], case: Mapping[str, Any]) -> dict[str, Any] | None:
    attempts = manifest.get("attempt_ids") or []
    attempt_id = _safe_id(attempts[0] if attempts else None)
    test_id, case_id = _safe_id(case.get("test_id")), _case_id(case.get("case_id"))
    if not all((attempt_id, test_id, case_id)):
        return None
    key = case_key(test_id, case_id, attempt_id)
    return {
        "evidence_id": f"runtime-{key[:32]}",
        "kind": "runtime_observability",
        "role": "supporting",
        "availability": "unavailable",
        "status": "unavailable",
        "media_type": "application/x-ndjson",
        "sample_count": 0,
        "operation_count": 0,
        "gap_count": 1,
        "summary": {"scopes": []},
        "coverage": {
            "sample_interval_ms": SAMPLE_INTERVAL_MS,
            "expected_ticks": 0,
            "observed_ticks": 0,
            "missed_ticks": 0,
            "sandbox_count": 0,
            "workspace_count": 0,
        },
        "errors": [
            {
                "reason_code": "child_interrupted",
                "count": 1,
                "message": _MESSAGES["child_interrupted"],
            }
        ],
    }


_COLLECTOR: ResourceCollector | None = None
_GLOBAL_LOCK = threading.Lock()


def configure(manifest: Mapping[str, Any], run_root: Path, binary: Path) -> None:
    global _COLLECTOR
    with _GLOBAL_LOCK:
        if _COLLECTOR is not None:
            _COLLECTOR.close()
        _COLLECTOR = ResourceCollector(manifest, run_root, binary)


def shutdown() -> None:
    global _COLLECTOR
    with _GLOBAL_LOCK:
        collector, _COLLECTOR = _COLLECTOR, None
    if collector is not None:
        collector.close()


def begin_case(test_id: str, case_id: str) -> None:
    if _COLLECTOR is not None:
        _COLLECTOR.request("begin", test_id, case_id, wait=True)


def phase(name: str, edge: str) -> None:
    if _COLLECTOR is not None:
        _COLLECTOR.request("phase", name, edge)


def track(sandbox_id: str) -> None:
    if _COLLECTOR is not None:
        _COLLECTOR.request("track", sandbox_id, wait=True, timeout=QUERY_TIMEOUT_SECONDS + 0.5)


def untrack(sandbox_id: str) -> None:
    if _COLLECTOR is not None:
        _COLLECTOR.request(
            "untrack",
            sandbox_id,
            wait=True,
            timeout=FINAL_HARVEST_SECONDS + QUERY_TIMEOUT_SECONDS + 1.0,
        )


def remember_workspace(sandbox_id: str | None, workspace_id: str | None) -> None:
    if _COLLECTOR is not None and sandbox_id and workspace_id:
        _COLLECTOR.request("workspace", sandbox_id, workspace_id)


def operation(
    surface: str,
    name: str,
    *,
    edge: str = "finish",
    duration_ms: float | None = None,
    returncode: int | None = None,
    sandbox_id: str | None = None,
    workspace_id: str | None = None,
) -> None:
    if _COLLECTOR is not None:
        _COLLECTOR.request(
            "operation",
            surface,
            name,
            edge,
            duration_ms,
            returncode,
            sandbox_id,
            workspace_id,
        )


def finalize_case(failure_phase: str | None = None) -> dict[str, Any] | None:
    if _COLLECTOR is None:
        return None
    return _COLLECTOR.request(
        "finalize",
        failure_phase,
        wait=True,
        timeout=FINAL_HARVEST_SECONDS + QUERY_TIMEOUT_SECONDS + 1.0,
    )


def unavailable_case_artifact(test_id: str, case_id: str) -> dict[str, Any]:
    """Return the mandatory bounded status when collection cannot finalize."""
    collector = _COLLECTOR
    attempt_id = collector.attempt_id if collector is not None else "attempt-unknown"
    key = case_key(str(test_id), str(case_id), attempt_id)
    return {
        "evidence_id": f"runtime-{key[:32]}",
        "kind": "runtime_observability",
        "role": "supporting",
        "availability": "unavailable",
        "status": "unavailable",
        "media_type": "application/x-ndjson",
        "sample_count": 0,
        "operation_count": 0,
        "gap_count": 1,
        "summary": {"scopes": []},
        "coverage": {
            "sample_interval_ms": SAMPLE_INTERVAL_MS,
            "expected_ticks": 0,
            "observed_ticks": 0,
            "missed_ticks": 0,
            "sandbox_count": 0,
            "workspace_count": 0,
        },
        "errors": [
            {
                "reason_code": "artifact_write_failed",
                "count": 1,
                "message": _MESSAGES["artifact_write_failed"],
            }
        ],
    }


def trusted_ids(value: Any) -> tuple[set[str], set[str]]:
    """Extract only allowlisted IDs from a parsed, trusted structured boundary."""
    sandboxes: set[str] = set()
    workspaces: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                safe = _safe_id(child)
                if key in {"sandbox_id", "id"} and safe and safe.startswith("eos-"):
                    sandboxes.add(safe)
                elif key in {"workspace_id", "workspace_session_id"} and safe:
                    workspaces.add(safe)
                elif isinstance(child, (Mapping, list, tuple)):
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item[:100]:
                visit(child)

    visit(value)
    return sandboxes, workspaces


def raw_cli_start(args: tuple[Any, ...]) -> tuple[str, str | None, str | None]:
    """Mark an allowlisted structured CLI boundary without retaining argv."""
    values = tuple(map(str, args))
    if not values:
        operation_name = "unknown.unknown"
    elif values[0] == "runtime":
        operation_name = f"runtime.{values[3] if len(values) > 3 else 'unknown'}"
    else:
        operation_name = f"{values[0]}.{values[1] if len(values) > 1 else 'unknown'}"

    def flag(name: str) -> str | None:
        try:
            return _safe_id(values[values.index(name) + 1])
        except (ValueError, IndexError):
            return None

    sandbox_id = flag("--sandbox-id")
    workspace_id = flag("--workspace-session-id") or flag("--workspace-id")
    operation("cli", operation_name, edge="start", sandbox_id=sandbox_id, workspace_id=workspace_id)
    return operation_name, sandbox_id, workspace_id


def raw_cli_finish(
    context: tuple[str, str | None, str | None],
    result: Any,
    duration_ms: float,
    returncode: int,
) -> None:
    # Legacy catalog helpers own their subprocess call but still cross the same
    # purpose-built CLI boundary as harness.runner.cli.  Publish that proof here
    # so a passing product case cannot be downgraded during durable projection.
    from .reporter import record_surface

    operation_name, sandbox_id, workspace_id = context
    operation(
        "cli",
        operation_name,
        duration_ms=duration_ms,
        returncode=returncode,
        sandbox_id=sandbox_id,
        workspace_id=workspace_id,
    )
    sandboxes, workspaces = trusted_ids(result)
    sandbox_id = sandbox_id or (next(iter(sandboxes)) if len(sandboxes) == 1 else None)
    if sandbox_id:
        for item in workspaces:
            remember_workspace(sandbox_id, item)
    record_surface(
        "cli",
        duration_ms=duration_ms,
        evidence={"operation": operation_name, "returncode": returncode},
    )
