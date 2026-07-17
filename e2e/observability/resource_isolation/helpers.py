"""Bounded evidence collection for packaged-daemon resource isolation.

Product behavior is always invoked by callers through the shared public CLI
wrappers.  This module uses Docker only as the out-of-band measurement and
fixture-installation channel allowed by the live specification.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import struct
import subprocess
import threading
import time
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Mapping, Sequence


EVENT_DIRECTORY = "/eos/runtime/daemon/observability"
EVENT_SEGMENTS = (
    f"{EVENT_DIRECTORY}/observability.ndjson",
    f"{EVENT_DIRECTORY}/observability.ndjson.1",
)
MAX_LINE_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_RESPONSE_RECORDS = 500
MAX_RING_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
SUMMARY_RESERVE_BYTES = 64 * 1024
RESERVOIR_SIZE = 2_048
MAX_THEIL_SEN_PAIRS = 100_000
FINGERPRINT_CHUNK_BYTES = 64 * 1024
MEASUREMENT_TIMEOUT_SECONDS = 10
DAEMON_PID_PATH = "/eos/runtime/daemon/runtime.pid"

ANONYMOUS_SLOPE_LIMIT_BYTES_PER_HOUR = 4 * 1024
ANONYMOUS_DELTA_LIMIT_BYTES = 64 * 1024
ENABLED_DISABLED_LIMIT_BYTES = 64 * 1024
COOLDOWN_LIMIT_BYTES = 128 * 1024
SMOKE_ANONYMOUS_LIMIT_BYTES = 1024 * 1024
SOAK_PROFILE = "soak"
COMPRESSED_PROFILE = "compressed-10x"
COMPRESSED_DURATION_DIVISOR = 10
COMPRESSED_LOAD_MULTIPLIER = 10


def env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def qualification_profile() -> dict[str, int | str]:
    """Return the explicit soak or time-compressed live-test profile."""
    name = os.environ.get("E2E_RI_QUALIFICATION_PROFILE", SOAK_PROFILE)
    if name == SOAK_PROFILE:
        return {"name": name, "duration_divisor": 1, "load_multiplier": 1}
    if name == COMPRESSED_PROFILE:
        return {
            "name": name,
            "duration_divisor": COMPRESSED_DURATION_DIVISOR,
            "load_multiplier": COMPRESSED_LOAD_MULTIPLIER,
        }
    raise ValueError(
        "E2E_RI_QUALIFICATION_PROFILE must be 'soak' or 'compressed-10x'"
    )


def qualification_duration(name: str, default: int, *, minimum: int) -> int:
    """Preserve soak minima unless an evidence-labelled compressed run is explicit."""
    divisor = int(qualification_profile()["duration_divisor"])
    return env_int(
        name,
        max(1, math.ceil(default / divisor)),
        minimum=max(1, math.ceil(minimum / divisor)),
    )


def qualification_load_multiplier() -> int:
    minimum = int(qualification_profile()["load_multiplier"])
    return env_int("E2E_RI_LOAD_MULTIPLIER", minimum, minimum=minimum)


def allowed_missed_deadlines(sample_ticks: int) -> int:
    """Give short phases the same one-outlier tolerance as a 100-tick phase."""
    assert sample_ticks > 0
    return max(1, sample_ticks // 100)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def compact_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def iter_capped_binary_lines(handle: BinaryIO, *, max_bytes: int) -> Iterator[bytes]:
    """Yield lines without ever asking the stream for an unbounded line."""
    assert max_bytes > 0
    while True:
        raw = handle.readline(max_bytes + 1)
        if not raw:
            return
        if len(raw) > max_bytes:
            raise AssertionError(
                {"line_bytes_exceed": max_bytes, "read_bytes": len(raw)}
            )
        yield raw


def pretty_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True).encode("utf-8")
        + b"\n"
    )


def docker(
    *args: str,
    timeout: float = MEASUREMENT_TIMEOUT_SECONDS,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["docker", *map(str, args)],
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode:
        stderr = result.stderr.decode("utf-8", "replace")[-2_000:]
        stdout = result.stdout.decode("utf-8", "replace")[-2_000:]
        raise AssertionError(
            f"docker {' '.join(args)} failed ({result.returncode}): {stderr or stdout}"
        )
    return result


def docker_exec(
    sandbox_id: str,
    script: str,
    *,
    timeout: float = MEASUREMENT_TIMEOUT_SECONDS,
    check: bool = True,
) -> str:
    result = docker(
        "exec",
        sandbox_id,
        "sh",
        "-c",
        script,
        timeout=timeout,
        check=check,
    )
    return result.stdout.decode("utf-8", "replace")


def sandbox_id_from_docker_create_event(event: Mapping[str, Any]) -> str | None:
    """Extract an EphemeralOS sandbox id from one Docker create event."""
    actor = event.get("Actor")
    if not isinstance(actor, Mapping):
        return None
    attributes = actor.get("Attributes")
    if not isinstance(attributes, Mapping):
        return None
    sandbox_id = attributes.get("eos.sandbox_id")
    return sandbox_id if isinstance(sandbox_id, str) and sandbox_id else None


class DockerSandboxCreationMonitor:
    """Bounded live guard against concurrent sandbox creation noise."""

    _MAX_RETAINED_EVENTS = 64

    def __init__(self, expected_sandbox_ids: Iterable[str]):
        self._expected = frozenset(expected_sandbox_ids)
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._foreign_count = 0
        self._foreign_ids: list[str] = []
        self._parse_errors = 0

    def __enter__(self) -> DockerSandboxCreationMonitor:
        self._process = subprocess.Popen(
            [
                "docker",
                "events",
                "--filter",
                "type=container",
                "--filter",
                "event=create",
                "--format",
                "{{json .}}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._thread = threading.Thread(target=self._consume, daemon=True)
        self._thread.start()
        # Let the Docker CLI establish its event subscription before the phase.
        time.sleep(0.1)
        if self._process.poll() is not None:
            stderr = (
                self._process.stderr.read()[-2_000:] if self._process.stderr else ""
            )
            raise AssertionError(
                f"docker event monitor exited before sampling: {stderr}"
            )
        return self

    def _consume(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                with self._lock:
                    self._parse_errors += 1
                continue
            sandbox_id = sandbox_id_from_docker_create_event(event)
            if sandbox_id is None or sandbox_id in self._expected:
                continue
            with self._lock:
                self._foreign_count += 1
                if len(self._foreign_ids) < self._MAX_RETAINED_EVENTS:
                    self._foreign_ids.append(sandbox_id)

    def result(self) -> dict[str, Any]:
        with self._lock:
            return {
                "foreign_sandbox_creations": self._foreign_count,
                "foreign_sandbox_ids": list(self._foreign_ids),
                "parse_errors": self._parse_errors,
            }

    def __exit__(self, exc_type, exc, traceback) -> None:
        assert self._process is not None
        self._process.terminate()
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=2)
        if self._thread is not None:
            self._thread.join(timeout=2)
        if exc_type is None:
            result = self.result()
            assert result["parse_errors"] == 0, result
            assert result["foreign_sandbox_creations"] == 0, {
                "reason": "another E2E worker created a sandbox during measurement",
                **result,
            }


def validate_packaged_daemon_identity(fields: Mapping[str, str]) -> dict[str, str]:
    daemon_pid = fields.get("daemon_pid", "")
    daemon_ppid = fields.get("daemon_ppid", "")
    daemon_exe = fields.get("daemon_exe", "")
    pid1_exe = fields.get("pid1_exe", "")
    assert daemon_pid.isdecimal() and int(daemon_pid) > 0, fields
    assert "sandbox-daemon" in daemon_exe, fields
    assert "Linux" in fields.get("kernel", ""), fields
    if daemon_pid == "1":
        assert "sandbox-daemon" in pid1_exe, fields
    else:
        assert pid1_exe.endswith(("/docker-init", "/tini")), fields
        assert daemon_ppid == "1", fields
    return {
        key: fields[key]
        for key in (
            "pid1_exe",
            "daemon_pid",
            "daemon_ppid",
            "daemon_exe",
            "kernel",
        )
    }


def verify_packaged_daemon(sandbox_id: str) -> dict[str, str]:
    """Prove the measured Linux process is the installed packaged daemon."""
    output = docker_exec(
        sandbox_id,
        f"daemon_pid=$(sed -n '1p' {DAEMON_PID_PATH}); "
        "case \"$daemon_pid\" in ''|*[!0-9]*) exit 71;; esac; "
        "printf 'pid1_exe\\t'; readlink /proc/1/exe; "
        "printf 'daemon_pid\\t%s\\n' \"$daemon_pid\"; "
        "printf 'daemon_ppid\\t'; sed -n 's/^PPid:[[:space:]]*//p' \"/proc/$daemon_pid/status\"; "
        "printf 'daemon_exe\\t'; readlink \"/proc/$daemon_pid/exe\"; "
        "printf 'kernel\\t'; uname -srmo",
    )
    fields = {}
    for line in output.splitlines():
        key, separator, value = line.partition("\t")
        if separator:
            fields[key] = value.strip()
    try:
        return validate_packaged_daemon_identity(fields)
    except AssertionError as error:
        raise AssertionError({"sandbox_id": sandbox_id, "identity": fields}) from error


_SAMPLE_SCRIPT = r"""
set +e
daemon_pid=$(sed -n '1p' /eos/runtime/daemon/runtime.pid)
case "$daemon_pid" in ''|*[!0-9]*) exit 71;; esac
printf '@DAEMON_PID\n%s\n' "$daemon_pid"
printf '@EXE\n'; readlink "/proc/$daemon_pid/exe"
printf '@SMAPS\n'; cat "/proc/$daemon_pid/smaps_rollup"
printf '@STAT\n'; cat "/proc/$daemon_pid/stat"
printf '@IO\n'; cat "/proc/$daemon_pid/io"
printf '@CGROUP\n'; cat "/proc/$daemon_pid/cgroup"
cg=$(sed -n 's/^0:://p' "/proc/$daemon_pid/cgroup" | sed -n '1p')
case "$cg" in /) cg="" ;; esac
printf '@MEMORY_CURRENT\n'; cat "/sys/fs/cgroup${cg}/memory.current"
printf '@MEMORY_STAT\n'; cat "/sys/fs/cgroup${cg}/memory.stat"
printf '@FILES\n'
for f in /eos/runtime/daemon/observability/observability.ndjson /eos/runtime/daemon/observability/observability.ndjson.1; do
  if [ -e "$f" ]; then
    mt=$(find "$f" -maxdepth 0 -printf '%T@' 2>/dev/null)
    printf '%s\t' "$f"
    stat --printf '%s\t%b\t%i\t%Y\t' "$f"
    printf '%s\n' "$mt"
  else
    printf '%s\tmissing\n' "$f"
  fi
done
""".strip()


def _sectioned(output: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in output.splitlines():
        if line.startswith("@"):
            current = line[1:]
            sections[current] = []
        elif current:
            sections[current].append(line)
    return sections


def _kilobytes(
    lines: Iterable[str], keys: Sequence[str]
) -> tuple[dict[str, int | None], list[str]]:
    values: dict[str, int | None] = {key: None for key in keys}
    for line in lines:
        name, separator, raw = line.partition(":")
        if not separator or name not in values:
            continue
        try:
            values[name] = int(raw.strip().split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    unavailable = [name for name, value in values.items() if value is None]
    return values, unavailable


def _integer_map(
    lines: Iterable[str], keys: Sequence[str]
) -> tuple[dict[str, int | None], list[str]]:
    values: dict[str, int | None] = {key: None for key in keys}
    for line in lines:
        parts = line.split()
        name = parts[0].removesuffix(":") if parts else ""
        if len(parts) >= 2 and name in values:
            try:
                values[name] = int(parts[1])
            except ValueError:
                pass
    unavailable = [name for name, value in values.items() if value is None]
    return values, unavailable


def _proc_stat(line: str) -> tuple[dict[str, int | None], list[str]]:
    values = {"user_ticks": None, "system_ticks": None}
    close = line.rfind(")")
    if close >= 0:
        fields = line[close + 1 :].split()
        try:
            # fields begins with proc stat field 3 (state).
            values["user_ticks"] = int(fields[11])
            values["system_ticks"] = int(fields[12])
        except (ValueError, IndexError):
            pass
    return values, [name for name, value in values.items() if value is None]


def _parse_file_stats(lines: Iterable[str]) -> dict[str, dict[str, Any]]:
    result = {}
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        path = parts[0]
        if parts[1] == "missing":
            result[path] = {"exists": False}
            continue
        try:
            fractional_mtime = parts[5] if len(parts) > 5 else ""
            mtime_ns = _decimal_seconds_to_ns(fractional_mtime)
            result[path] = {
                "exists": True,
                "logical_bytes": int(parts[1]),
                "allocated_bytes": int(parts[2]) * 512,
                "inode": int(parts[3]),
                "mtime_seconds": int(parts[4]),
                "mtime_ns": mtime_ns,
            }
        except (ValueError, IndexError):
            result[path] = {"exists": None, "unavailable": "invalid stat output"}
    for path in EVENT_SEGMENTS:
        result.setdefault(path, {"exists": None, "unavailable": "missing stat output"})
    return result


def _decimal_seconds_to_ns(value: str) -> int:
    whole, separator, fraction = value.partition(".")
    if not separator:
        return int(whole) * 1_000_000_000
    digits = (fraction.rstrip("0") + "000000000")[:9]
    return int(whole) * 1_000_000_000 + int(digits)


def host_file_stat(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"exists": False, "unavailable": "manager registry path not configured"}
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False}
    return {
        "exists": True,
        "logical_bytes": stat.st_size,
        "allocated_bytes": getattr(stat, "st_blocks", 0) * 512,
        "inode": stat.st_ino,
        "mtime_ns": stat.st_mtime_ns,
    }


def default_resource_ring_path(sandbox_id: str) -> Path:
    root = os.environ.get("XDG_STATE_HOME")
    if root:
        state = Path(root) / "eos-sandbox"
    elif platform.system() == "Darwin":
        state = Path.home() / "Library/Application Support/eos-sandbox"
    else:
        state = Path.home() / ".local/state/eos-sandbox"
    return state / "observability-resources" / f"{sandbox_id}.ring"


def registry_resource_ring_path(registry_path: Path, sandbox_id: str) -> Path:
    return registry_path.parent / "observability-resources" / f"{sandbox_id}.ring"


def wait_for_path(path: Path, *, exists: bool, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while path.exists() is not exists:
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"timed out waiting for {path} existence to become {exists}"
            )
        time.sleep(0.25)


def resource_ring_header(path: Path) -> dict[str, int | str]:
    with path.open("rb") as handle:
        header = handle.read(64)
    assert len(header) == 64, {"path": str(path), "header_bytes": len(header)}
    magic, version, record_bytes, capacity, next_index, count, sequence = struct.unpack(
        "<8sIIIIIQ", header[:36]
    )
    return {
        "magic": magic.rstrip(b"\0").decode("ascii", "replace"),
        "version": version,
        "record_bytes": record_bytes,
        "capacity": capacity,
        "next_index": next_index,
        "count": count,
        "sequence": sequence,
    }


def collect_sample(
    sandbox_id: str,
    *,
    phase: str,
    arm: str,
    repetition: int,
    ring_path: Path | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    output = docker_exec(sandbox_id, _SAMPLE_SCRIPT)
    sections = _sectioned(output)
    executable = "".join(sections.get("EXE", []))
    assert "sandbox-daemon" in executable, {
        "sandbox_id": sandbox_id,
        "daemon_executable": executable,
    }
    daemon_pid = "".join(sections.get("DAEMON_PID", []))
    assert daemon_pid.isdecimal() and int(daemon_pid) > 0, {
        "sandbox_id": sandbox_id,
        "daemon_pid": daemon_pid,
    }

    smaps_keys = ("Rss", "Pss", "Anonymous", "Private_Dirty", "AnonHugePages")
    smaps, smaps_missing = _kilobytes(sections.get("SMAPS", []), smaps_keys)
    cpu, cpu_missing = _proc_stat("".join(sections.get("STAT", [])))
    io_keys = (
        "rchar",
        "wchar",
        "syscr",
        "syscw",
        "read_bytes",
        "write_bytes",
        "cancelled_write_bytes",
    )
    process_io, io_missing = _integer_map(sections.get("IO", []), io_keys)
    memory_keys = (
        "anon",
        "file",
        "kernel",
        "kernel_stack",
        "pagetables",
        "sock",
        "slab",
        "anon_thp",
    )
    memory_stat, memory_missing = _integer_map(
        sections.get("MEMORY_STAT", []), memory_keys
    )
    memory_current = None
    try:
        memory_current = int("".join(sections.get("MEMORY_CURRENT", [])).strip())
    except ValueError:
        pass
    cgroup_membership = "".join(sections.get("CGROUP", []))
    unavailable = []
    unavailable.extend(f"smaps.{name}" for name in smaps_missing)
    unavailable.extend(f"cpu.{name}" for name in cpu_missing)
    unavailable.extend(f"io.{name}" for name in io_missing)
    unavailable.extend(f"cgroup.{name}" for name in memory_missing)
    if memory_current is None:
        unavailable.append("cgroup.memory_current")
    if not cgroup_membership.startswith("0::"):
        unavailable.append("cgroup.membership")

    return {
        "schema_version": 1,
        "wall_time": utc_now(),
        "monotonic_seconds": started,
        "measurement_duration_ms": round((time.monotonic() - started) * 1_000, 3),
        "phase": phase,
        "arm": arm,
        "repetition": repetition,
        "sandbox_id": sandbox_id,
        "daemon_pid": int(daemon_pid),
        "smaps": smaps,
        "cpu": cpu,
        "io": process_io,
        "cgroup": {
            "membership": cgroup_membership,
            "memory_current": memory_current,
            "memory_stat": memory_stat,
        },
        "event_store": _parse_file_stats(sections.get("FILES", [])),
        "resource_ring": host_file_stat(ring_path),
        "unavailable": unavailable,
    }


class DeterministicReservoir:
    """Fixed-size Algorithm-R reservoir using an isolated deterministic LCG."""

    __slots__ = ("capacity", "count", "values", "_state")

    def __init__(self, capacity: int = RESERVOIR_SIZE, seed: int = 0xE05_1A7E):
        self.capacity = capacity
        self.count = 0
        self.values: list[Any] = []
        self._state = seed & 0xFFFFFFFFFFFFFFFF

    def add(self, value: Any) -> None:
        self.count += 1
        if len(self.values) < self.capacity:
            self.values.append(value)
            return
        self._state = (
            self._state * 6_364_136_223_846_793_005 + 1_442_695_040_888_963_407
        ) & 0xFFFFFFFFFFFFFFFF
        index = self._state % self.count
        if index < self.capacity:
            self.values[index] = value


@dataclass
class FixedMetricSummary:
    """Online summary whose memory use is independent of sample count."""

    capacity: int = RESERVOIR_SIZE
    count: int = 0
    minimum: float | None = None
    maximum: float | None = None
    total: float = 0.0
    histogram: list[int] = field(default_factory=lambda: [0] * 64)
    reservoir: DeterministicReservoir = field(init=False)

    def __post_init__(self) -> None:
        self.reservoir = DeterministicReservoir(self.capacity)

    def update(self, monotonic_seconds: float, value: float) -> None:
        self.count += 1
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.total += value
        magnitude = 0 if value <= 0 else min(63, int(math.log2(value)) + 1)
        self.histogram[magnitude] += 1
        self.reservoir.add((monotonic_seconds, value))

    def result(self) -> dict[str, Any]:
        values = sorted(value for _, value in self.reservoir.values)
        return {
            "count": self.count,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "mean": self.total / self.count if self.count else None,
            "sample_median": statistics.median(values) if values else None,
            "reservoir_size": len(values),
            "histogram": self.histogram,
        }


class ArtifactDirectory:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=False)
        self.samples_path = self.root / "samples.jsonl"
        self._sample_handle = self.samples_path.open("ab", buffering=0)

    def append_sample(self, sample: Mapping[str, Any]) -> None:
        encoded = compact_json_bytes(sample) + b"\n"
        if (
            self.total_bytes() + len(encoded) + SUMMARY_RESERVE_BYTES
            > MAX_ARTIFACT_BYTES
        ):
            raise AssertionError("resource-isolation artifact cap would be exceeded")
        self._sample_handle.write(encoded)
        self._sample_handle.flush()

    def append_jsonl(self, name: str, value: Mapping[str, Any]) -> None:
        encoded = compact_json_bytes(value) + b"\n"
        if (
            self.total_bytes() + len(encoded) + SUMMARY_RESERVE_BYTES
            > MAX_ARTIFACT_BYTES
        ):
            raise AssertionError("resource-isolation artifact cap would be exceeded")
        with (self.root / name).open("ab", buffering=0) as handle:
            handle.write(encoded)

    def write_json(self, name: str, value: Any, *, reserved: bool = False) -> Path:
        encoded = pretty_json_bytes(value)
        allowance = 0 if reserved else SUMMARY_RESERVE_BYTES
        path = self.root / name
        replaced_bytes = path.stat().st_size if path.exists() else 0
        if (
            self.total_bytes() - replaced_bytes + len(encoded) + allowance
            > MAX_ARTIFACT_BYTES
        ):
            raise AssertionError("resource-isolation artifact cap would be exceeded")
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(path)
        return path

    def finalize_summary(self) -> int:
        """Atomically persist the exact final artifact size in ``summary.json``."""
        self.close()
        path = self.root / "summary.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise AssertionError("resource-isolation summary must be a JSON object")
            replaced_bytes = path.stat().st_size
        else:
            payload = {}
            replaced_bytes = 0
        base_bytes = self.total_bytes() - replaced_bytes
        artifact_bytes = base_bytes
        encoded = b""
        for _ in range(16):
            payload["artifact_bytes"] = artifact_bytes
            encoded = pretty_json_bytes(payload)
            next_bytes = base_bytes + len(encoded)
            if next_bytes == artifact_bytes:
                break
            artifact_bytes = next_bytes
        else:
            raise AssertionError("resource-isolation artifact size did not converge")
        if artifact_bytes > MAX_ARTIFACT_BYTES:
            raise AssertionError(
                {
                    "artifact_bytes": artifact_bytes,
                    "max_artifact_bytes": MAX_ARTIFACT_BYTES,
                }
            )
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(path)
        actual_bytes = self.total_bytes()
        assert actual_bytes == artifact_bytes, {
            "artifact_bytes": actual_bytes,
            "summary_artifact_bytes": artifact_bytes,
        }
        return actual_bytes

    def total_bytes(self) -> int:
        total = 0
        for path in self.root.iterdir():
            if path.is_file():
                total += path.stat().st_size
        return total

    def close(self) -> None:
        if not self._sample_handle.closed:
            self._sample_handle.close()

    def assert_bounded(self) -> int:
        self.close()
        size = self.total_bytes()
        assert size <= MAX_ARTIFACT_BYTES, {
            "artifact_bytes": size,
            "max_artifact_bytes": MAX_ARTIFACT_BYTES,
        }
        return size


def environment_evidence(sandbox_id: str | None = None) -> dict[str, Any]:
    docker_version = docker("version", "--format", "{{json .}}", timeout=30)
    value: dict[str, Any] = {
        "captured_at": utc_now(),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "logical_cpu_count": os.cpu_count(),
            "load_average": list(os.getloadavg()),
        },
        "docker": json.loads(docker_version.stdout),
        "configuration": {
            key: value
            for key, value in os.environ.items()
            if key.startswith(("E2E_RI_", "E2E_DS_", "E2E_GC_"))
        },
    }
    if sandbox_id:
        value["daemon"] = verify_packaged_daemon(sandbox_id)
        measurement = docker_exec(
            sandbox_id,
            f"daemon_pid=$(sed -n '1p' {DAEMON_PID_PATH}); "
            "printf 'clock_ticks_per_second\\t'; getconf CLK_TCK; "
            "printf 'cgroup_filesystem\\t'; stat -fc '%T' /sys/fs/cgroup; "
            "printf 'cgroup_membership\\t'; cat \"/proc/$daemon_pid/cgroup\"",
        )
        measurement_fields = {}
        for line in measurement.splitlines():
            key, separator, raw = line.partition("\t")
            if separator:
                measurement_fields[key] = raw.strip()
        clock_ticks = measurement_fields.get("clock_ticks_per_second", "")
        assert clock_ticks.isdecimal(), measurement_fields
        value["measurement"] = {
            "clock_ticks_per_second": int(clock_ticks),
            "cgroup_filesystem": measurement_fields.get("cgroup_filesystem"),
            "cgroup_mode": (
                "v2"
                if measurement_fields.get("cgroup_membership", "").startswith("0::")
                else "unavailable"
            ),
            "cgroup_membership": measurement_fields.get("cgroup_membership"),
        }
        inspect = docker("inspect", sandbox_id)
        document = json.loads(inspect.stdout)[0]
        value["sandbox"] = {
            "id": sandbox_id,
            "image": document.get("Config", {}).get("Image"),
            "memory_limit": document.get("HostConfig", {}).get("Memory"),
            "nano_cpus": document.get("HostConfig", {}).get("NanoCpus"),
            "privileged": document.get("HostConfig", {}).get("Privileged"),
        }
    return value


def stream_group(
    artifacts: ArtifactDirectory,
    targets: Sequence[tuple[str, str, Path | None]],
    *,
    phase: str,
    repetition: int,
    duration_seconds: float,
    interval_seconds: float = 1.0,
    action: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Sample all targets at one fixed cadence without retaining raw records."""
    assert targets
    with DockerSandboxCreationMonitor(
        sandbox_id for sandbox_id, _, _ in targets
    ) as guard:
        phase_start = time.monotonic()
        deadline = phase_start + duration_seconds
        next_tick = phase_start
        index = 0
        late = 0
        summaries = {arm: FixedMetricSummary() for _, arm, _ in targets}
        while True:
            now = time.monotonic()
            if index and now >= deadline:
                break
            if now < next_tick:
                time.sleep(next_tick - now)
            observed = time.monotonic()
            if observed - next_tick > interval_seconds:
                late += 1
            if action is not None:
                action(index)
            for sandbox_id, arm, ring_path in targets:
                sample = collect_sample(
                    sandbox_id,
                    phase=phase,
                    arm=arm,
                    repetition=repetition,
                    ring_path=ring_path,
                )
                artifacts.append_sample(sample)
                anonymous = sample["smaps"].get("Anonymous")
                if isinstance(anonymous, int):
                    summaries[arm].update(sample["monotonic_seconds"], anonymous)
            index += 1
            next_tick = phase_start + index * interval_seconds
        phase_end = time.monotonic()
        creation_guard = guard.result()
    late_fraction = late / index if index else 1.0
    allowed_late = allowed_missed_deadlines(index)
    assert late <= allowed_late, {
        "phase": phase,
        "missed_deadlines": late,
        "allowed_missed_deadlines": allowed_late,
        "sample_ticks": index,
        "fraction": late_fraction,
    }
    return {
        "phase": phase,
        "repetition": repetition,
        "started_monotonic": phase_start,
        "ended_monotonic": phase_end,
        "duration_seconds": phase_end - phase_start,
        "sample_ticks": index,
        "missed_deadlines": late,
        "allowed_missed_deadlines": allowed_late,
        "docker_creation_guard": creation_guard,
        "online": {arm: summary.result() for arm, summary in summaries.items()},
    }


def measure_sampler_free_cpu_baseline(
    artifacts: ArtifactDirectory,
    targets: Sequence[tuple[str, str, Path | None]],
    *,
    phase: str,
    repetition: int,
    duration_seconds: float,
) -> dict[str, dict[str, float | int]]:
    """Measure idle daemon ticks with no per-second sampler between endpoints."""
    assert targets and duration_seconds > 0
    with DockerSandboxCreationMonitor(sandbox_id for sandbox_id, _, _ in targets):
        started = time.monotonic()
        first = {}
        for sandbox_id, arm, ring_path in targets:
            sample = collect_sample(
                sandbox_id,
                phase=phase,
                arm=arm,
                repetition=repetition,
                ring_path=ring_path,
            )
            artifacts.append_sample(sample)
            first[arm] = sample
        time.sleep(duration_seconds)
        last = {}
        for sandbox_id, arm, ring_path in targets:
            sample = collect_sample(
                sandbox_id,
                phase=phase,
                arm=arm,
                repetition=repetition,
                ring_path=ring_path,
            )
            artifacts.append_sample(sample)
            last[arm] = sample
        ended = time.monotonic()

    elapsed_minutes = (ended - started) / 60.0
    results = {}
    for _, arm, _ in targets:
        first_cpu = first[arm]["cpu"]
        last_cpu = last[arm]["cpu"]
        first_user = first_cpu.get("user_ticks")
        first_system = first_cpu.get("system_ticks")
        last_user = last_cpu.get("user_ticks")
        last_system = last_cpu.get("system_ticks")
        assert all(
            isinstance(value, int)
            for value in (first_user, first_system, last_user, last_system)
        ), {
            "arm": arm,
            "first_cpu": first_cpu,
            "last_cpu": last_cpu,
        }
        first_ticks = first_user + first_system
        last_ticks = last_user + last_system
        results[arm] = {
            "duration_seconds": ended - started,
            "cpu_tick_delta": last_ticks - first_ticks,
            "cpu_ticks_per_minute": (last_ticks - first_ticks) / elapsed_minutes,
        }
    return results


def _sampled_theil_sen(points: Sequence[tuple[float, float]]) -> float | None:
    if len(points) < 2:
        return None
    ordered = sorted(points)
    # 448 points yield 100,128 pairs; stop at the specified 100,000 cap.
    if len(ordered) > 448:
        step = (len(ordered) - 1) / 447
        ordered = [ordered[round(index * step)] for index in range(448)]
    slopes: list[float] = []
    for left in range(len(ordered)):
        left_time, left_value = ordered[left]
        for right in range(left + 1, len(ordered)):
            right_time, right_value = ordered[right]
            elapsed = right_time - left_time
            if elapsed > 0:
                slopes.append((right_value - left_value) / elapsed)
            if len(slopes) == MAX_THEIL_SEN_PAIRS:
                return statistics.median(slopes) * 3_600
    return statistics.median(slopes) * 3_600 if slopes else None


def _bootstrap_median_ci(
    values: Sequence[float], rounds: int = 400
) -> list[float] | None:
    if not values:
        return None
    state = 0xB005_7A9
    estimates = []
    count = len(values)
    for _ in range(rounds):
        sample = []
        for _ in range(count):
            state = (state * 1_103_515_245 + 12_345) & 0x7FFFFFFF
            sample.append(values[state % count])
        estimates.append(statistics.median(sample))
    estimates.sort()
    return [estimates[int(rounds * 0.025)], estimates[int(rounds * 0.975) - 1]]


def _bootstrap_slope_ci(
    points: Sequence[tuple[float, float]],
    *,
    rounds: int = 400,
    max_points: int = 128,
) -> list[float] | None:
    """Return a deterministic bounded bootstrap interval for Theil-Sen slope."""
    if len(points) < 2:
        return None
    ordered = sorted(points)
    if len(ordered) > max_points:
        step = (len(ordered) - 1) / (max_points - 1)
        ordered = [ordered[round(index * step)] for index in range(max_points)]
    state = 0x51_0F_EC7
    estimates = []
    count = len(ordered)
    for _ in range(rounds):
        sample = []
        for _ in range(count):
            state = (state * 1_103_515_245 + 12_345) & 0x7FFFFFFF
            sample.append(ordered[state % count])
        estimate = _sampled_theil_sen(sample)
        if estimate is not None:
            estimates.append(estimate)
    if not estimates:
        return None
    estimates.sort()
    return [
        estimates[int(len(estimates) * 0.025)],
        estimates[max(0, int(len(estimates) * 0.975) - 1)],
    ]


def analyze_phase(
    samples_path: Path,
    *,
    phase: str,
    arm: str,
    repetition: int,
    started_monotonic: float,
    ended_monotonic: float,
    warmup_seconds: float = 0,
    sampler_free_cpu_baseline_ticks_per_minute: float = 0.0,
) -> dict[str, Any]:
    """Make one streaming analysis pass over the raw artifact."""
    steady_start = started_monotonic + warmup_seconds
    steady_duration = max(0.0, ended_monotonic - steady_start)
    window_seconds = min(300.0, max(1.0, steady_duration / 3.0))
    first = DeterministicReservoir()
    final = DeterministicReservoir()
    all_points = DeterministicReservoir()
    cpu_first = cpu_last = None
    io_first = io_last = None
    huge_page_peak = 0
    cgroup_thp_peak = 0
    ring_peak = 0
    event_peak = 0
    required_missing: set[str] = set()
    sample_count = 0

    with samples_path.open("rb") as handle:
        for raw in iter_capped_binary_lines(handle, max_bytes=MAX_LINE_BYTES * 8):
            try:
                record = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if (
                record.get("phase") != phase
                or record.get("arm") != arm
                or record.get("repetition") != repetition
            ):
                continue
            unavailable = record.get("unavailable", ())
            if isinstance(unavailable, list):
                required_missing.update(
                    name for name in unavailable if isinstance(name, str)
                )
            else:
                required_missing.add("sample.unavailable")
            observed_at = record.get("monotonic_seconds")
            anonymous = record.get("smaps", {}).get("Anonymous")
            if not isinstance(observed_at, (int, float)) or observed_at < steady_start:
                continue
            if not isinstance(anonymous, int):
                required_missing.add("smaps.Anonymous")
                continue
            sample_count += 1
            point = (float(observed_at), float(anonymous))
            all_points.add(point)
            if observed_at <= steady_start + window_seconds:
                first.add(float(anonymous))
            if observed_at >= ended_monotonic - window_seconds:
                final.add(float(anonymous))
            cpu = record.get("cpu", {})
            user = cpu.get("user_ticks")
            system = cpu.get("system_ticks")
            if isinstance(user, int) and isinstance(system, int):
                total_cpu = user + system
                cpu_first = total_cpu if cpu_first is None else cpu_first
                cpu_last = total_cpu
            else:
                required_missing.add("cpu.ticks")
            process_io = record.get("io", {})
            read_bytes = process_io.get("read_bytes")
            write_bytes = process_io.get("write_bytes")
            if isinstance(read_bytes, int) and isinstance(write_bytes, int):
                total_io = read_bytes + write_bytes
                io_first = total_io if io_first is None else io_first
                io_last = total_io
            else:
                required_missing.add("io.storage_bytes")
            huge = record.get("smaps", {}).get("AnonHugePages")
            if isinstance(huge, int):
                huge_page_peak = max(huge_page_peak, huge)
            else:
                required_missing.add("smaps.AnonHugePages")
            cgroup_thp = record.get("cgroup", {}).get("memory_stat", {}).get("anon_thp")
            if isinstance(cgroup_thp, int):
                cgroup_thp_peak = max(cgroup_thp_peak, cgroup_thp)
            else:
                required_missing.add("cgroup.anon_thp")
            ring = record.get("resource_ring", {})
            if ring.get("exists") is True:
                ring_peak = max(ring_peak, int(ring.get("logical_bytes", 0)))
            event_store = record.get("event_store", {})
            event_peak = max(
                event_peak,
                sum(
                    int(segment.get("logical_bytes", 0))
                    for segment in event_store.values()
                    if segment.get("exists") is True
                ),
            )

    first_values = [float(value) for value in first.values]
    final_values = [float(value) for value in final.values]
    first_median = statistics.median(first_values) if first_values else None
    final_median = statistics.median(final_values) if final_values else None
    elapsed_minutes = steady_duration / 60.0
    raw_cpu_ticks_per_minute = None
    cpu_ticks_per_minute = None
    if cpu_first is not None and cpu_last is not None and elapsed_minutes > 0:
        raw_cpu_ticks_per_minute = (cpu_last - cpu_first) / elapsed_minutes
        cpu_ticks_per_minute = (
            raw_cpu_ticks_per_minute - sampler_free_cpu_baseline_ticks_per_minute
        )
    slope_points = [
        (float(observed_at), float(anonymous))
        for observed_at, anonymous in all_points.values
    ]
    return {
        "sample_count": sample_count,
        "steady_duration_seconds": steady_duration,
        "anonymous_slope_bytes_per_hour": _sampled_theil_sen(slope_points),
        "anonymous_slope_bootstrap_95": _bootstrap_slope_ci(slope_points),
        "first_window_median_bytes": first_median,
        "final_window_median_bytes": final_median,
        "final_minus_first_median_bytes": (
            final_median - first_median
            if first_median is not None and final_median is not None
            else None
        ),
        "first_median_bootstrap_95": _bootstrap_median_ci(first_values),
        "final_median_bootstrap_95": _bootstrap_median_ci(final_values),
        "cpu_ticks_per_minute_raw": raw_cpu_ticks_per_minute,
        "sampler_free_cpu_baseline_ticks_per_minute": (
            sampler_free_cpu_baseline_ticks_per_minute
        ),
        "cpu_ticks_per_minute": cpu_ticks_per_minute,
        "storage_io_delta_bytes": (
            io_last - io_first if io_first is not None and io_last is not None else None
        ),
        "anon_huge_pages_peak_bytes": huge_page_peak,
        "cgroup_anon_thp_peak_bytes": cgroup_thp_peak,
        "resource_ring_peak_bytes": ring_peak,
        "event_store_peak_bytes": event_peak,
        "required_unavailable": sorted(required_missing),
    }


def assert_memory_gates(
    result: Mapping[str, Any],
    *,
    require_idle_cpu: bool = True,
    event_cap_bytes: int = 4 * 1024 * 1024,
) -> None:
    assert not result["required_unavailable"], result
    slope = result["anonymous_slope_bytes_per_hour"]
    delta = result["final_minus_first_median_bytes"]
    assert slope is not None and slope <= ANONYMOUS_SLOPE_LIMIT_BYTES_PER_HOUR, result
    assert delta is not None and delta <= ANONYMOUS_DELTA_LIMIT_BYTES, result
    assert result["anon_huge_pages_peak_bytes"] == 0, result
    assert result["cgroup_anon_thp_peak_bytes"] == 0, result
    if require_idle_cpu:
        ticks = result["cpu_ticks_per_minute"]
        assert ticks is not None and ticks < 1.0, result
    assert result["storage_io_delta_bytes"] == 0, result
    assert result["resource_ring_peak_bytes"] <= MAX_RING_BYTES, result
    assert result["event_store_peak_bytes"] <= event_cap_bytes, result


def parse_container_stat_lines(lines: Sequence[str]) -> dict[str, Any]:
    if not lines or lines[0] == "missing":
        return {"exists": False}
    fields = lines[0].split("\t")
    assert len(fields) == 4, lines
    return {
        "exists": True,
        "logical_bytes": int(fields[0]),
        "allocated_bytes": int(fields[1]) * 512,
        "inode": int(fields[2]),
        "mtime_seconds": int(fields[3]),
        "mtime_ns": _decimal_seconds_to_ns(lines[1]),
    }


def _container_stat(sandbox_id: str, path: str) -> dict[str, Any]:
    script = (
        f"if [ ! -e {path} ]; then printf 'missing\\n'; exit 0; fi; "
        f"stat --printf '%s\\t%b\\t%i\\t%Y\\n' {path}; "
        f"find {path} -maxdepth 0 -printf '%T@\\n'"
    )
    return parse_container_stat_lines(docker_exec(sandbox_id, script).splitlines())


def fingerprint_container_file(
    sandbox_id: str, path: str, *, timeout: float = 30
) -> dict[str, Any]:
    stat = _container_stat(sandbox_id, path)
    if stat["exists"] is False:
        return stat
    process = subprocess.Popen(
        ["docker", "exec", sandbox_id, "cat", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    digest = hashlib.sha256()
    line = bytearray()
    complete = parseable = malformed = oversized = 0
    last_byte = None
    discarding = False
    try:
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(FINGERPRINT_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            last_byte = chunk[-1]
            for byte in chunk:
                if byte == 10:
                    complete += 1
                    if discarding:
                        oversized += 1
                    else:
                        try:
                            json.loads(line)
                            parseable += 1
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            malformed += 1
                    line.clear()
                    discarding = False
                elif not discarding:
                    if len(line) == MAX_LINE_BYTES:
                        line.clear()
                        discarding = True
                    else:
                        line.append(byte)
        stderr = process.stderr.read() if process.stderr is not None else b""
        returncode = process.wait()
    finally:
        timer.cancel()
    assert returncode == 0, stderr.decode("utf-8", "replace")[-2_000:]
    return {
        **stat,
        "sha256": digest.hexdigest(),
        "complete_lines": complete,
        "parseable_lines": parseable,
        "malformed_complete_lines": malformed,
        "oversized_complete_lines": oversized,
        "partial_final_line": last_byte not in (None, 10),
    }


def fingerprint_store(sandbox_id: str) -> dict[str, Any]:
    segments = {
        path.rsplit("/", 1)[-1]: fingerprint_container_file(sandbox_id, path)
        for path in EVENT_SEGMENTS
    }
    return {
        "segments": segments,
        "total_logical_bytes": sum(
            value.get("logical_bytes", 0)
            for value in segments.values()
            if value.get("exists") is True
        ),
        "total_allocated_bytes": sum(
            value.get("allocated_bytes", 0)
            for value in segments.values()
            if value.get("exists") is True
        ),
    }


def rotation_renamed_active(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> bool:
    """Recognize the active-to-rotated rename even if one request emits many lines."""
    active_before = before["segments"]["observability.ndjson"]
    rotated_after = after["segments"]["observability.ndjson.1"]
    active_inode = active_before.get("inode")
    return (
        active_before.get("exists") is True
        and isinstance(active_inode, int)
        and rotated_after.get("exists") is True
        and rotated_after.get("inode") == active_inode
    )


def assert_store_unchanged(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    assert before == after, {"before": before, "after": after}


def assert_store_bounded(
    store: Mapping[str, Any], total_cap_bytes: int
) -> dict[str, Any]:
    segment_cap = total_cap_bytes // 2
    segments = store["segments"]
    logical = int(store["total_logical_bytes"])
    allocated = int(store["total_allocated_bytes"])
    existing = [item for item in segments.values() if item.get("exists") is True]
    assert logical <= total_cap_bytes, store
    for segment in existing:
        assert int(segment["logical_bytes"]) <= segment_cap, store
        assert segment["malformed_complete_lines"] == 0, store
        assert segment["oversized_complete_lines"] == 0, store
        assert segment["partial_final_line"] is False, store
        assert segment["complete_lines"] == segment["parseable_lines"], store
    block_allowance = 4_096 * len(existing)
    assert allocated <= total_cap_bytes + block_allowance, store
    return {
        "logical_bytes": logical,
        "allocated_bytes": allocated,
        "segment_count": len(existing),
        "block_allowance_bytes": block_allowance,
    }


def assert_response_bounded(response: Mapping[str, Any]) -> dict[str, int]:
    encoded_bytes = len(compact_json_bytes(response))
    max_list = 0
    stack = [response]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            max_list = max(max_list, len(value))
            stack.extend(value)
    assert encoded_bytes <= MAX_RESPONSE_BYTES, {
        "response_bytes": encoded_bytes,
        "limit": MAX_RESPONSE_BYTES,
    }
    assert max_list <= MAX_RESPONSE_RECORDS, {
        "max_response_records": max_list,
        "limit": MAX_RESPONSE_RECORDS,
    }
    return {"encoded_bytes": encoded_bytes, "max_list_records": max_list}


def response_digest(response: Mapping[str, Any], digest: Any) -> None:
    digest.update(compact_json_bytes(response))


def stream_container_jsonl(
    artifacts: ArtifactDirectory,
    sandbox_id: str,
    source: str,
    destination: str,
    consume: Callable[[Mapping[str, Any]], None] | None = None,
    *,
    timeout: float = 60,
) -> int:
    process = subprocess.Popen(
        ["docker", "exec", sandbox_id, "cat", source],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    records = 0
    try:
        assert process.stdout is not None
        for raw in iter_capped_binary_lines(process.stdout, max_bytes=MAX_LINE_BYTES):
            value = json.loads(raw)
            assert isinstance(value, dict), value
            artifacts.append_jsonl(destination, value)
            if consume is not None:
                consume(value)
            records += 1
        stderr = process.stderr.read() if process.stderr is not None else b""
        returncode = process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
    assert returncode == 0, stderr.decode("utf-8", "replace")[-2_000:]
    return records


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return float(ordered[index])


def stream_history_fixture(
    path: Path, target_bytes: int, *, trace_id: str = "fixture"
) -> int:
    """Write valid NDJSON incrementally, never constructing target-sized bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    index = 0
    with path.open("wb", buffering=FINGERPRINT_CHUNK_BYTES) as handle:
        while True:
            record = {
                "kind": "event",
                "ts": index,
                "trace": trace_id,
                "name": "fixture.event",
                "attrs": {"sequence": index, "payload": "x" * 96},
            }
            encoded = compact_json_bytes(record) + b"\n"
            padding = {
                "kind": "event",
                "ts": index,
                "trace": trace_id,
                "name": "fixture.padding",
                "attrs": {"payload": ""},
            }
            padding_overhead = len(compact_json_bytes(padding)) + 1
            remaining = target_bytes - written
            if remaining < len(encoded) + padding_overhead:
                break
            handle.write(encoded)
            written += len(encoded)
            index += 1
        remaining = target_bytes - written
        if padding_overhead <= remaining <= MAX_LINE_BYTES:
            padding["attrs"]["payload"] = "x" * (remaining - padding_overhead)
            encoded = compact_json_bytes(padding) + b"\n"
            assert len(encoded) == remaining
            handle.write(encoded)
            written += len(encoded)
    return written


def docker_copy_to(sandbox_id: str, source: Path, destination: str) -> None:
    result = docker(
        "cp", str(source), f"{sandbox_id}:{destination}", timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")[-2_000:]


def isolated_tmpfs_capability(sandbox_id: str) -> tuple[bool, str]:
    probe = f"{EVENT_DIRECTORY}/.e2e-mount-probe"
    script = (
        f"mkdir -p {probe} && mount -t tmpfs -o size=32768,nr_inodes=32 "
        f"e2e-observability-probe {probe} && umount {probe} && rmdir {probe}"
    )
    result = docker("exec", sandbox_id, "sh", "-c", script, timeout=15, check=False)
    reason = (result.stderr or result.stdout).decode("utf-8", "replace")[-1_000:]
    return result.returncode == 0, reason


@contextmanager
def isolated_event_store(
    sandbox_id: str, size_bytes: int = 32 * 1024
) -> Iterator[None]:
    """Mount a run-owned tiny filesystem at only this sandbox's store."""
    assert 4_096 <= size_bytes <= 1024 * 1024
    mount_name = f"e2e-observability-{sandbox_id[:24]}"
    docker_exec(
        sandbox_id,
        f"mkdir -p {EVENT_DIRECTORY} && mount -t tmpfs "
        f"-o size={size_bytes},nr_inodes=32 {mount_name} {EVENT_DIRECTORY}",
    )
    try:
        yield
    finally:
        docker_exec(
            sandbox_id,
            f"umount {EVENT_DIRECTORY}",
            check=False,
        )


def capability_is_required(name: str) -> bool:
    return (
        os.environ.get("E2E_RELEASE_REQUIRED", "0") == "1"
        or os.environ.get(f"E2E_REQUIRE_{name.upper()}", "0") == "1"
    )


def write_cleanup_evidence(
    artifacts: ArtifactDirectory,
    *,
    registered: Sequence[str],
    destroyed: Sequence[str],
    failures: Sequence[Mapping[str, str]],
) -> None:
    artifacts.write_json(
        "cleanup.json",
        {
            "registered_sandbox_ids": list(registered),
            "destroyed_sandbox_ids": list(destroyed),
            "failures": list(failures),
            "cleanup_complete": not failures and set(registered) == set(destroyed),
        },
    )
