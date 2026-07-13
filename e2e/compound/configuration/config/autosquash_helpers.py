"""Live-Docker helpers and retained evidence for the autosquash catalog.

The helpers deliberately poll product-owned structured state (the manifest,
layerstack view, or NDJSON records).  They never use fixed sleeps to infer that
background work has finished.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import subprocess
import threading
import time
from pathlib import Path

import yaml

from config import helpers
from harness.runner import cli as climod
from harness.runner.config import E2E_STATE_ROOT, PRODUCT_ROOT
from manager.management import helpers as mgmt
from runtime.file import helpers as filemod


MANIFEST = "/eos/layer-stack/manifest.json"
DAEMON_CONFIG = "/eos/config/daemon.yml"
OBS_FILES = (
    "/eos/runtime/daemon/observability/observability.ndjson.1",
    "/eos/runtime/daemon/observability/observability.ndjson",
)
AUTOSQUASH_PREFIX = "layerstack.autosquash."
EVALUATE = f"{AUTOSQUASH_PREFIX}evaluate"
TRIGGERED = f"{AUTOSQUASH_PREFIX}triggered"
COMPLETED = f"{AUTOSQUASH_PREFIX}completed"
FAILED = f"{AUTOSQUASH_PREFIX}failed"
SQUASH = "layerstack.squash"
SQUASH_CHILDREN = {
    "layerstack.squash.plan",
    "layerstack.squash.flatten",
    "layerstack.squash.commit",
    "layerstack.squash.remount_sweep",
}

_RUN = os.environ.get(
    "AUTOSQUASH_ARTIFACT_RUN",
    f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}",
)
ARTIFACT_ROOT = E2E_STATE_ROOT / "autosquash-artifacts" / _RUN
_ARTIFACT_LOCK = threading.Lock()


def configure(daemon_yaml: Path, threshold: int | None) -> Path:
    """Configure the next sandbox; ``None`` is the disabled custom-config arm."""
    layerstack = {}
    if threshold is not None:
        layerstack["autosquash_policies"] = {"squash_at_n_layers": threshold}
    return helpers.rewrite_daemon_yaml(
        daemon_yaml,
        {"runtime": {"layerstack": layerstack}},
    )


def configure_raw(daemon_yaml: Path, policies: dict) -> Path:
    return helpers.rewrite_daemon_yaml(
        daemon_yaml,
        {"runtime": {"layerstack": {"autosquash_policies": policies}}},
    )


def create() -> str:
    return helpers.create_sandbox_or_fail()


def destroy(sandbox_id: str) -> None:
    result = mgmt.destroy_sandbox(sandbox_id)
    assert not climod.is_error(result), result


def docker(sandbox_id: str, *args: str, timeout: float = 120, check: bool = True):
    result = subprocess.run(
        ["docker", "exec", sandbox_id, *map(str, args)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout or f"docker exec failed: {args}")
    return result


def manifest(sandbox_id: str) -> dict:
    return json.loads(docker(sandbox_id, "cat", MANIFEST).stdout)


def layers(sandbox_id: str) -> list[dict]:
    return manifest(sandbox_id)["layers"]


def layer_prefixes(sandbox_id: str) -> list[str]:
    return [layer["layer_id"][0] for layer in layers(sandbox_id)]


def layerstack_view(sandbox_id: str) -> dict:
    result = filemod.layerstack(sandbox_id)
    assert not climod.is_error(result), result
    return result


def wait_gateway(sandbox_id: str, timeout_s: float = 120) -> dict:
    return wait_for(
        "gateway forwarding",
        lambda: filemod.layerstack(sandbox_id),
        lambda result: isinstance(result, dict) and not climod.is_error(result),
        timeout_s,
    )


def records(sandbox_id: str) -> list[dict]:
    command = "for f in " + " ".join(OBS_FILES) + "; do test ! -f \"$f\" || cat \"$f\"; done"
    result = docker(sandbox_id, "sh", "-c", command)
    parsed = []
    for line in result.stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            parsed.append(record)
    parsed.sort(key=lambda record: (float(record.get("ts", 0)), record.get("name", "")))
    return parsed


def autosquash_records(sandbox_id: str) -> list[dict]:
    return [record for record in records(sandbox_id) if record.get("name", "").startswith(AUTOSQUASH_PREFIX)]


def wait_for(description: str, probe, predicate=lambda value: bool(value), timeout_s: float = 10):
    deadline = time.monotonic_ns() + int(timeout_s * 1_000_000_000)
    last = None
    while time.monotonic_ns() < deadline:
        last = probe()
        if predicate(last):
            return last
        os.sched_yield()
    raise AssertionError(f"timed out waiting for {description}; last structured evidence={last!r}")


def wait_for_record(
    sandbox_id: str,
    name: str,
    *,
    after: int = 0,
    predicate=lambda record: True,
    timeout_s: float = 10,
) -> tuple[dict, list[dict]]:
    def probe():
        selected = [record for record in records(sandbox_id) if record.get("name") == name]
        return selected

    selected = wait_for(
        f"{name} record after index {after}",
        probe,
        lambda found: len(found) > after and any(predicate(record) for record in found[after:]),
        timeout_s,
    )
    return next(record for record in selected[after:] if predicate(record)), selected


def wait_below(sandbox_id: str, threshold: int, timeout_s: float = 10) -> dict:
    return wait_for(
        f"active layer count below {threshold}",
        lambda: manifest(sandbox_id),
        lambda current: len(current["layers"]) < threshold,
        timeout_s,
    )


def wait_evaluation_count(sandbox_id: str, count: int, timeout_s: float = 10) -> list[dict]:
    return wait_for(
        f"{count} completed autosquash evaluations",
        lambda: [record for record in records(sandbox_id) if record.get("name") == EVALUATE],
        lambda found: len(found) >= count,
        timeout_s,
    )


def assert_ok(result: dict) -> dict:
    assert isinstance(result, dict) and not climod.is_error(result), result
    return result


def write(sandbox_id: str, path: str, content: str, *, timeout: float = 180) -> dict:
    return assert_ok(filemod.file_write(sandbox_id, path, content, timeout=timeout))


def edit(sandbox_id: str, path: str, old: str, new: str) -> dict:
    return assert_ok(filemod.file_edit(sandbox_id, path, [filemod.edit(old, new)]))


def execute(sandbox_id: str, command: str, *, timeout: float = 240) -> dict:
    result = assert_ok(
        filemod.exec_command(
            sandbox_id,
            command,
            timeout_ms=int(timeout * 1000),
            yield_time_ms=min(int(timeout * 1000), 10_000),
            timeout=timeout,
        )
    )
    if result.get("status") == "running":
        command_session_id = result.get("command_session_id")
        assert command_session_id, result
        deadline = time.monotonic_ns() + int(timeout * 1_000_000_000)
        while result.get("status") == "running" and time.monotonic_ns() < deadline:
            remaining_ms = max(1, (deadline - time.monotonic_ns()) // 1_000_000)
            continued = filemod.write_command_stdin(
                sandbox_id,
                command_session_id,
                "\n",
                yield_time_ms=min(10_000, remaining_ms),
                timeout=min(timeout, 180),
            )
            if climod.is_error(continued):
                continued = filemod.read_command_lines(
                    sandbox_id,
                    command_session_id,
                    start_offset=0,
                    limit=1000,
                    timeout=min(timeout, 180),
                )
            result = assert_ok(continued)
        assert result.get("status") != "running", (
            f"command session {command_session_id} did not reach terminal state: {result}"
        )
    assert result.get("status") == "ok" and result.get("exit_code") == 0, result
    return result


def start_squash_staging_watch(sandbox_id: str) -> subprocess.Popen:
    command = (
        "set -eu; printf '{\"ready\":true}\\n'; "
        "while :; do set -- /eos/layer-stack/staging/S*.staging; "
        "if [ -e \"$1\" ]; then printf '{\"staging_entry\":\"%s\"}\\n' \"$1\"; exit 0; fi; done"
    )
    process = subprocess.Popen(
        ["docker", "exec", sandbox_id, "sh", "-c", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    ready = json.loads(process.stdout.readline())
    assert ready == {"ready": True}, ready
    return process


def finish_squash_staging_watch(process: subprocess.Popen, timeout_s: float = 120) -> dict:
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise AssertionError(
            f"timed out waiting for squash staging evidence: stdout={stdout!r} stderr={stderr!r}"
        )
    assert process.returncode == 0, stderr
    evidence = json.loads(stdout.strip())
    assert Path(evidence.get("staging_entry", "")).name.startswith("S"), evidence
    assert evidence["staging_entry"].endswith(".staging"), evidence
    return evidence


def stop_squash_staging_watch(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def manual_squash(sandbox_id: str, *, timeout: float = 300) -> dict:
    return climod.manager("squash_layerstacks", "--sandbox-id", sandbox_id, timeout=timeout)


def read(sandbox_id: str, path: str) -> str:
    result = assert_ok(filemod.file_read(sandbox_id, path))
    return result["content"]


def blame(sandbox_id: str, path: str) -> dict:
    return assert_ok(filemod.file_blame(sandbox_id, path))


def visible_tree_digest(sandbox_id: str, exclude: str | None = None) -> str:
    exclude_arg = f" --exclude={json.dumps('./' + exclude)}" if exclude else ""
    command = (
        "cd /workspace && tar --sort=name --format=gnu --mtime=@0 --owner=0 --group=0 "
        f"--numeric-owner{exclude_arg} -cf - . | sha256sum | awk '{{print $1}}'"
    )
    return execute(sandbox_id, command)["output"].strip()


def assert_clean(sandbox_id: str) -> dict:
    view = layerstack_view(sandbox_id)
    disk = docker(
        sandbox_id,
        "sh",
        "-c",
        "set -eu; "
        "printf 'staging='; find /eos/layer-stack/staging -mindepth 1 2>/dev/null | wc -l; "
        "printf 'remount='; find /eos \\( -name '.remount-*' -o -name 'remount-*' \\) "
        "2>/dev/null | wc -l; "
        "printf 'layers='; for root in /eos/layer-stack/layers /eos/layer-stack/base; do "
        "test ! -d \"$root\" || find \"$root\" -mindepth 1 -maxdepth 1 -type d -printf '%f\\n'; "
        "done | sort",
    ).stdout.splitlines()
    staging = int(disk[0].split("=", 1)[1])
    remount = int(disk[1].split("=", 1)[1])
    disk_layers = set(disk[2].split("=", 1)[1:] + disk[3:])
    active_layers = {layer["layer_id"] for layer in layers(sandbox_id)}
    evidence = {
        "active_lease_count": int(view.get("active_lease_count", 0)),
        "staging_entries": staging,
        "remount_residue": remount,
        "active_layers": sorted(active_layers),
        "disk_layers": sorted(disk_layers),
    }
    assert evidence["active_lease_count"] == 0, evidence
    assert staging == 0 and remount == 0, evidence
    assert active_layers == disk_layers, evidence
    return evidence


def write_artifact(case_id: str, payload: dict) -> Path:
    with _ARTIFACT_LOCK:
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        target = ARTIFACT_ROOT / f"{case_id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(target)
        return target


def retain_case(sandbox_id: str, case_id: str, payload: dict) -> Path:
    evidence = dict(payload)
    evidence["manifest"] = manifest(sandbox_id)
    evidence["autosquash_records"] = autosquash_records(sandbox_id)
    evidence["teardown"] = assert_clean(sandbox_id)
    return write_artifact(case_id, evidence)


def attrs(record: dict) -> dict:
    value = record.get("attrs", {})
    assert isinstance(value, dict), record
    return value


def assert_exact_trace(sandbox_id: str, completed: dict) -> dict:
    trace = completed["trace"]
    related = [record for record in records(sandbox_id) if record.get("trace") == trace]
    spans = [record for record in related if record.get("kind") == "span"]
    events = [record for record in related if record.get("kind") == "event"]
    by_name = {record["name"]: record for record in spans}
    assert set(by_name) == {EVALUATE, SQUASH, *SQUASH_CHILDREN}, by_name
    evaluate = by_name[EVALUATE]
    squash = by_name[SQUASH]
    assert evaluate.get("parent") is None, evaluate
    assert squash.get("parent") == evaluate.get("span"), squash
    assert attrs(squash)["cause"] == "autosquash", squash
    for child in SQUASH_CHILDREN:
        assert by_name[child].get("parent") == squash.get("span"), by_name[child]
    assert {event["name"] for event in events} == {TRIGGERED, COMPLETED}, events
    assert all(event.get("parent") == evaluate.get("span") for event in events), events
    assert all(not record.get("name", "").startswith("operation.") for record in related), related
    return {"trace": trace, "records": related}


def percentile(values: list[float], percentile_value: float) -> float:
    assert values
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile_value * len(ordered)) - 1)
    return ordered[rank]


def distribution(values: list[float]) -> dict:
    return {
        "count": len(values),
        "raw": values,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values),
    }


def environment_metadata() -> dict:
    docker_version = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    git = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PRODUCT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "clock": "time.monotonic_ns",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "docker_server": docker_version.stdout.strip(),
        "product_commit": git.stdout.strip(),
        "warmups": 5,
        "samples": 30,
    }


def product_default_threshold() -> int:
    document = yaml.safe_load((PRODUCT_ROOT / "config" / "prd.yml").read_text(encoding="utf-8"))
    return document["runtime"]["layerstack"]["autosquash_policies"]["squash_at_n_layers"]


def patch_container_threshold(sandbox_id: str, threshold: int) -> None:
    script = (
        "import pathlib,yaml; p=pathlib.Path('/eos/config/daemon.yml'); "
        "d=yaml.safe_load(p.read_text()); "
        f"d.setdefault('runtime',{{}}).setdefault('layerstack',{{}})['autosquash_policies']={{'squash_at_n_layers':{threshold}}}; "
        "p.write_text(yaml.safe_dump(d,sort_keys=False))"
    )
    # The Ubuntu image need not carry PyYAML. Use the host to rewrite and docker cp.
    del script
    current = yaml.safe_load(docker(sandbox_id, "cat", DAEMON_CONFIG).stdout)
    current.setdefault("runtime", {}).setdefault("layerstack", {})["autosquash_policies"] = {
        "squash_at_n_layers": threshold
    }
    temporary = ARTIFACT_ROOT / f"{sandbox_id}-daemon.yml"
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    temporary.write_text(yaml.safe_dump(current, sort_keys=False), encoding="utf-8")
    result = subprocess.run(
        ["docker", "cp", str(temporary), f"{sandbox_id}:{DAEMON_CONFIG}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


def recover_gateway(sandbox_id: str, gateway_yaml: Path, timeout_s: float = 120) -> dict:
    helpers.start_gateway(gateway_yaml)
    return wait_gateway(sandbox_id, timeout_s)


def restart_container(
    sandbox_id: str, gateway_yaml: Path, timeout_s: float = 120
) -> None:
    result = subprocess.run(
        ["docker", "restart", sandbox_id], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    wait_for(
        "daemon runtime socket after restart",
        lambda: docker(sandbox_id, "test", "-S", "/eos/runtime/daemon/runtime.sock", check=False).returncode,
        lambda returncode: returncode == 0,
        timeout_s,
    )
    recover_gateway(sandbox_id, gateway_yaml, timeout_s)


def file_sha256(sandbox_id: str, path: str) -> str:
    return docker(sandbox_id, "sha256sum", path).stdout.split()[0]


def stable_payload(index: int, size: int = 0) -> str:
    seed = hashlib.sha256(f"autosquash-{index}".encode()).hexdigest()
    if size <= 0:
        return seed
    return (seed * ((size // len(seed)) + 1))[:size]
