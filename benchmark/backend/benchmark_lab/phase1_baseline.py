from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import shlex
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError

from .models import StrictModel
from .observability import LayerstackView, SnapshotView
from .product import ProductAccess
from .sessions import SessionLifecycle
from .transport import GatewayProductError, TimedGatewayResponse


MAX_PUBLIC_FILE_READ_BYTES = 256 * 1024


class Phase1BaselineError(RuntimeError):
    pass


class CorpusFile(StrictModel):
    path: str = Field(min_length=1)
    gzip_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content_bytes: int = Field(ge=1, le=4 * 1024 * 1024)


class CorpusManifest(StrictModel):
    schema_version: int = Field(ge=1, le=1)
    corpus_version: str = Field(pattern=r"^v1$")
    seed: int = Field(ge=0)
    localized_marker_offset: int = Field(ge=0)
    localized_marker_bytes: int = Field(ge=1)
    small_file_count: int = Field(ge=1, le=1024)
    small_file_bytes: int = Field(ge=1, le=1024 * 1024)
    history_depths: list[int] = Field(min_length=3, max_length=3)
    files: dict[str, CorpusFile]


def load_phase1_corpus(root: Path) -> tuple[CorpusManifest, dict[str, Any]]:
    try:
        manifest_path = (root / "manifest.json").resolve(strict=True)
        if manifest_path.parent != root.resolve(strict=True):
            raise Phase1BaselineError("corpus manifest escaped its immutable root")
        manifest = CorpusManifest.model_validate_json(manifest_path.read_bytes())
    except (OSError, ValidationError) as error:
        raise Phase1BaselineError("phase1 corpus manifest is invalid") from error
    if (
        manifest.history_depths != [1, 8, 32]
        or manifest.small_file_count != 256
        or manifest.small_file_count * manifest.small_file_bytes != 1024 * 1024
        or manifest.localized_marker_bytes != 1024
        or set(manifest.files)
        != {"localized_base", "incompressible", "small_source", "history_payloads"}
    ):
        raise Phase1BaselineError("phase1 corpus shape is not the frozen tiny baseline")

    values: dict[str, Any] = {}
    canonical_root = root.resolve(strict=True)
    for name, definition in manifest.files.items():
        try:
            path = (canonical_root / definition.path).resolve(strict=True)
            if path.parent != canonical_root or not path.is_file():
                raise Phase1BaselineError("corpus member escaped its immutable root")
            compressed = path.read_bytes()
            if _sha(compressed) != definition.gzip_sha256:
                raise Phase1BaselineError("corpus compressed digest mismatch")
            content = gzip.decompress(compressed)
        except (OSError, gzip.BadGzipFile) as error:
            raise Phase1BaselineError("phase1 corpus member is invalid") from error
        if len(content) != definition.content_bytes or _sha(content) != definition.content_sha256:
            raise Phase1BaselineError("corpus content digest mismatch")
        try:
            decoded = content.decode()
            values[name] = json.loads(decoded) if name == "history_payloads" else decoded
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Phase1BaselineError("phase1 corpus text is invalid") from error
    payloads = values["history_payloads"]
    if (
        not isinstance(payloads, list)
        or len(payloads) != 32
        or any(not isinstance(item, str) or len(item.encode()) != 4096 for item in payloads)
    ):
        raise Phase1BaselineError("history corpus is not 32 deterministic 4 KiB payloads")
    return manifest, values


async def run_phase1_baseline(
    *,
    product: ProductAccess,
    sessions: SessionLifecycle,
    sandbox: str,
    body: dict[str, Any],
    timeout_ms: int,
    trial_id: str,
    corpus_root: Path,
    check_cancelled: Callable[[], None],
) -> tuple[list[TimedGatewayResponse], dict[str, Any]]:
    manifest, corpus = load_phase1_corpus(corpus_root)
    if body["corpus_version"] != manifest.corpus_version:
        raise Phase1BaselineError("planned corpus version disagrees with immutable corpus")
    responses: list[TimedGatewayResponse] = []
    raw_pairs: list[dict[str, Any]] = []
    total_pairs = body["pair_warmups"] + body["measured_pairs"]
    for pair_index in range(total_pairs):
        check_cancelled()
        warmup = pair_index < body["pair_warmups"]
        pair_label = f"{trial_id}.pair.{pair_index:02d}"
        arms: dict[str, dict[str, Any]] = {}
        order = ("control", "raw") if pair_index % 2 == 0 else ("raw", "control")
        for arm in order:
            if arm == "control":
                arms[arm] = await _control_arm(
                    product,
                    sessions,
                    sandbox,
                    timeout_ms,
                    pair_label,
                    responses,
                    check_cancelled,
                )
            else:
                arms[arm] = await _raw_arm(
                    product,
                    sessions,
                    sandbox,
                    timeout_ms,
                    pair_label,
                    manifest,
                    corpus,
                    responses,
                    check_cancelled,
                )
        raw_ns = arms["raw"]["latency_ns"]
        control_ns = arms["control"]["latency_ns"]
        raw_pairs.append(
            {
                "pair_index": pair_index,
                "warmup": warmup,
                "order": list(order),
                "raw": arms["raw"],
                "control": arms["control"],
                "absolute_delta_ns": raw_ns - control_ns,
                "raw_to_control_ratio": raw_ns / control_ns if control_ns else None,
            }
        )

    sentinel = await _reclamation_sentinel(
        product=product,
        sessions=sessions,
        sandbox=sandbox,
        body=body,
        timeout_ms=timeout_ms,
        trial_id=trial_id,
        responses=responses,
        check_cancelled=check_cancelled,
    )
    final = await product.observe_layerstack(
        sandbox, request_id=f"{trial_id}.final.layerstack"
    )
    _assert_legacy_route(final)
    if sessions.owned_count != 0:
        raise Phase1BaselineError("phase1 operation retained owned sessions")
    measured = [pair for pair in raw_pairs if not pair["warmup"]]
    evidence = {
        "schema_version": 1,
        "corpus": {
            "version": manifest.corpus_version,
            "seed": manifest.seed,
            "manifest_sha256": _sha((corpus_root / "manifest.json").read_bytes()),
            "cases": [
                "empty_no_op",
                "localized_edit_1k_in_1m",
                "incompressible_1m",
                "small_files_256_total_1m",
                "overwrite_history_1",
                "overwrite_history_8",
                "overwrite_history_32",
            ],
        },
        "pair_protocol": {
            "warmup_pairs": body["pair_warmups"],
            "measured_pairs": body["measured_pairs"],
            "counterbalanced": True,
            "percentiles_normative": False,
            "raw_control_noise_band_bytes": body["noise_band_bytes"],
        },
        "pairs": raw_pairs,
        "measured_summary": {
            "sample_count": len(measured),
            "raw_median_ns": int(statistics.median(pair["raw"]["latency_ns"] for pair in measured)),
            "control_median_ns": int(
                statistics.median(pair["control"]["latency_ns"] for pair in measured)
            ),
            "absolute_delta_median_ns": int(
                statistics.median(pair["absolute_delta_ns"] for pair in measured)
            ),
            "ratio_median": statistics.median(
                pair["raw_to_control_ratio"]
                for pair in measured
                if pair["raw_to_control_ratio"] is not None
            ),
        },
        "reclamation": sentinel,
        "final_route": final.route.model_dump(mode="json"),
        "final_resources": final.resources.model_dump(mode="json"),
    }
    return responses, evidence


async def _control_arm(
    product: ProductAccess,
    sessions: SessionLifecycle,
    sandbox: str,
    timeout_ms: int,
    pair_label: str,
    responses: list[TimedGatewayResponse],
    check_cancelled: Callable[[], None],
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    latency = 0
    for case_index, case_name in enumerate(_CASE_NAMES):
        check_cancelled()
        key = f"{pair_label}.control.{case_index:02d}"
        started = time.monotonic_ns()
        session, created = await sessions.create_no_op(
            sandbox, "shared", request_id=f"{key}.create", timeout_ms=timeout_ms
        )
        published, response = await sessions.publish(
            session, request_id=f"{key}.publish", timeout_ms=timeout_ms
        )
        elapsed = time.monotonic_ns() - started
        if not published.publish.no_op or published.publish.route_summary.source_count != 0:
            raise Phase1BaselineError("control arm did not execute a no-op publication")
        responses.extend((created, response))
        latency += elapsed
        cases.append({"case": case_name, "latency_ns": elapsed, "no_op": True})
    return {"latency_ns": latency, "cases": cases}


async def _raw_arm(
    product: ProductAccess,
    sessions: SessionLifecycle,
    sandbox: str,
    timeout_ms: int,
    pair_label: str,
    manifest: CorpusManifest,
    corpus: dict[str, Any],
    responses: list[TimedGatewayResponse],
    check_cancelled: Callable[[], None],
) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for case_index, case_name in enumerate(_CASE_NAMES):
        check_cancelled()
        key = f"{pair_label}.raw.{case_index:02d}"
        if case_name == "empty_no_op":
            result = await _publish_no_op(
                product, sessions, sandbox, timeout_ms, key, responses
            )
        elif case_name == "localized_edit_1k_in_1m":
            result = await _localized_edit(
                product,
                sessions,
                sandbox,
                timeout_ms,
                key,
                manifest,
                corpus["localized_base"],
                responses,
            )
        elif case_name == "incompressible_1m":
            result = await _single_file_publish(
                product,
                sessions,
                sandbox,
                timeout_ms,
                key,
                corpus["incompressible"],
                responses,
            )
        elif case_name == "small_files_256_total_1m":
            result = await _small_files_publish(
                product,
                sessions,
                sandbox,
                timeout_ms,
                key,
                manifest,
                corpus["small_source"],
                responses,
            )
        else:
            depth = int(case_name.rsplit("_", 1)[1])
            result = await _overwrite_history(
                product,
                sessions,
                sandbox,
                timeout_ms,
                key,
                depth,
                corpus["history_payloads"],
                responses,
            )
        result["case"] = case_name
        cases.append(result)
    return {"latency_ns": sum(case["latency_ns"] for case in cases), "cases": cases}


async def _publish_no_op(
    product: ProductAccess,
    sessions: SessionLifecycle,
    sandbox: str,
    timeout_ms: int,
    key: str,
    responses: list[TimedGatewayResponse],
) -> dict[str, Any]:
    started = time.monotonic_ns()
    session, created = await sessions.create_no_op(
        sandbox, "shared", request_id=f"{key}.create", timeout_ms=timeout_ms
    )
    published, response = await sessions.publish(
        session, request_id=f"{key}.publish", timeout_ms=timeout_ms
    )
    elapsed = time.monotonic_ns() - started
    if not published.publish.no_op:
        raise Phase1BaselineError("empty workload unexpectedly published a layer")
    responses.extend((created, response))
    return {"latency_ns": elapsed, "published_layers": 0}


async def _localized_edit(
    product: ProductAccess,
    sessions: SessionLifecycle,
    sandbox: str,
    timeout_ms: int,
    key: str,
    manifest: CorpusManifest,
    base: str,
    responses: list[TimedGatewayResponse],
) -> dict[str, Any]:
    path = f".eos-benchmark-phase1/{key}/localized.bin"
    base_session, created = await sessions.create_no_op(
        sandbox, "shared", request_id=f"{key}.base.create", timeout_ms=timeout_ms
    )
    written = await product.file_write(
        sandbox,
        session_id=base_session.session_id,
        path=path,
        content=base,
        timeout_ms=timeout_ms,
        request_id=f"{key}.base.write",
    )
    base_published, base_response = await sessions.publish(
        base_session, request_id=f"{key}.base.publish", timeout_ms=timeout_ms
    )
    if base_published.publish.no_op:
        raise Phase1BaselineError("localized base publication was a no-op")
    responses.extend((created, written, base_response))
    old = base[
        manifest.localized_marker_offset : manifest.localized_marker_offset
        + manifest.localized_marker_bytes
    ]
    new = "E" * manifest.localized_marker_bytes
    expected = (
        base[: manifest.localized_marker_offset]
        + new
        + base[manifest.localized_marker_offset + manifest.localized_marker_bytes :]
    )
    started = time.monotonic_ns()
    session, created = await sessions.create_no_op(
        sandbox, "shared", request_id=f"{key}.create", timeout_ms=timeout_ms
    )
    edited = await product.file_edit(
        sandbox,
        session_id=session.session_id,
        path=path,
        edits=[{"old_string": old, "new_string": new}],
        timeout_ms=timeout_ms,
        request_id=f"{key}.edit",
    )
    published, response = await sessions.publish(
        session, request_id=f"{key}.publish", timeout_ms=timeout_ms
    )
    elapsed = time.monotonic_ns() - started
    if published.publish.no_op or edited.value.get("edits_applied") != 1:
        raise Phase1BaselineError("localized edit publication did not apply exactly once")
    responses.extend((created, edited, response))
    await _verify_file(
        product, sandbox, path, expected, timeout_ms, f"{key}.verify", responses
    )
    return {
        "latency_ns": elapsed,
        "published_layers": 1,
        "content_sha256": _sha(expected.encode()),
    }


async def _single_file_publish(
    product: ProductAccess,
    sessions: SessionLifecycle,
    sandbox: str,
    timeout_ms: int,
    key: str,
    content: str,
    responses: list[TimedGatewayResponse],
) -> dict[str, Any]:
    path = f".eos-benchmark-phase1/{key}/incompressible.bin"
    started = time.monotonic_ns()
    session, created = await sessions.create_no_op(
        sandbox, "shared", request_id=f"{key}.create", timeout_ms=timeout_ms
    )
    written = await product.file_write(
        sandbox,
        session_id=session.session_id,
        path=path,
        content=content,
        timeout_ms=timeout_ms,
        request_id=f"{key}.write",
    )
    published, response = await sessions.publish(
        session, request_id=f"{key}.publish", timeout_ms=timeout_ms
    )
    elapsed = time.monotonic_ns() - started
    if published.publish.no_op or written.value.get("bytes_written") != len(content.encode()):
        raise Phase1BaselineError("incompressible publication response is inconsistent")
    responses.extend((created, written, response))
    await _verify_file(
        product, sandbox, path, content, timeout_ms, f"{key}.verify", responses
    )
    return {
        "latency_ns": elapsed,
        "published_layers": 1,
        "content_sha256": _sha(content.encode()),
    }


async def _small_files_publish(
    product: ProductAccess,
    sessions: SessionLifecycle,
    sandbox: str,
    timeout_ms: int,
    key: str,
    manifest: CorpusManifest,
    source: str,
    responses: list[TimedGatewayResponse],
) -> dict[str, Any]:
    directory = f".eos-benchmark-phase1/{key}/small"
    source_path = f"{directory}/source.bin"
    command = (
        f"set -eu; i=0; while [ \"$i\" -lt {manifest.small_file_count} ]; do "
        f"dd if=/workspace/{source_path} of=/workspace/{directory}/file-$(printf '%03d' \"$i\").bin "
        f"bs={manifest.small_file_bytes} skip=\"$i\" count=1 status=none; "
        f"i=$((i + 1)); done; rm /workspace/{source_path}"
    )
    started = time.monotonic_ns()
    session, created = await sessions.create_no_op(
        sandbox, "shared", request_id=f"{key}.create", timeout_ms=timeout_ms
    )
    written = await product.file_write(
        sandbox,
        session_id=session.session_id,
        path=source_path,
        content=source,
        timeout_ms=timeout_ms,
        request_id=f"{key}.write",
    )
    split = await product.exec_command(
        sandbox,
        session_id=session.session_id,
        command=command,
        timeout_ms=timeout_ms,
        request_id=f"{key}.split",
    )
    published, response = await sessions.publish(
        session, request_id=f"{key}.publish", timeout_ms=timeout_ms
    )
    elapsed = time.monotonic_ns() - started
    if published.publish.no_op or split.value.get("exit_code") != 0:
        raise Phase1BaselineError("small-file publication failed to materialize its corpus")
    responses.extend((created, written, split, response))
    verify = await product.exec_command(
        sandbox,
        session_id=None,
        command=_small_files_totals_command(f"/workspace/{directory}"),
        timeout_ms=timeout_ms,
        request_id=f"{key}.verify",
    )
    responses.append(verify)
    if not _command_output_matches(verify.value, ["256", "1048576"]):
        raise Phase1BaselineError("small-file corpus count or byte total is incorrect")
    return {
        "latency_ns": elapsed,
        "published_layers": 1,
        "file_count": manifest.small_file_count,
        "total_bytes": len(source.encode()),
        "content_sha256": _sha(source.encode()),
    }


async def _overwrite_history(
    product: ProductAccess,
    sessions: SessionLifecycle,
    sandbox: str,
    timeout_ms: int,
    key: str,
    depth: int,
    payloads: list[str],
    responses: list[TimedGatewayResponse],
) -> dict[str, Any]:
    path = f".eos-benchmark-phase1/{key}/history.bin"
    started = time.monotonic_ns()
    for revision in range(depth):
        session, created = await sessions.create_no_op(
            sandbox,
            "shared",
            request_id=f"{key}.revision.{revision:02d}.create",
            timeout_ms=timeout_ms,
        )
        written = await product.file_write(
            sandbox,
            session_id=session.session_id,
            path=path,
            content=payloads[revision],
            timeout_ms=timeout_ms,
            request_id=f"{key}.revision.{revision:02d}.write",
        )
        published, response = await sessions.publish(
            session,
            request_id=f"{key}.revision.{revision:02d}.publish",
            timeout_ms=timeout_ms,
        )
        if published.publish.no_op:
            raise Phase1BaselineError("overwrite history unexpectedly published a no-op")
        responses.extend((created, written, response))
    elapsed = time.monotonic_ns() - started
    await _verify_file(
        product,
        sandbox,
        path,
        payloads[depth - 1],
        timeout_ms,
        f"{key}.verify",
        responses,
    )
    return {
        "latency_ns": elapsed,
        "published_layers": depth,
        "history_depth": depth,
        "content_sha256": _sha(payloads[depth - 1].encode()),
    }


async def _verify_file(
    product: ProductAccess,
    sandbox: str,
    path: str,
    expected: str,
    timeout_ms: int,
    request_id: str,
    responses: list[TimedGatewayResponse],
) -> None:
    expected_bytes = expected.encode()
    if len(expected_bytes) > MAX_PUBLIC_FILE_READ_BYTES:
        workspace_path = shlex.quote(f"/workspace/{path}")
        response = await product.exec_command(
            sandbox,
            session_id=None,
            command=(
                f"set -eu; bytes=$(wc -c < {workspace_path}); "
                f"digest=$(sha256sum {workspace_path} | awk '{{print $1}}'); "
                "printf '%s %s\\n' \"$bytes\" \"$digest\""
            ),
            timeout_ms=timeout_ms,
            request_id=request_id,
        )
        responses.append(response)
        value = response.value
        if not _command_output_matches(
            value,
            [str(len(expected_bytes)), hashlib.sha256(expected_bytes).hexdigest()],
        ):
            raise Phase1BaselineError("published content digest verification failed")
        return

    response = await product.file_read(
        sandbox,
        session_id=None,
        path=path,
        offset=1,
        limit=2000,
        timeout_ms=timeout_ms,
        request_id=request_id,
    )
    responses.append(response)
    value = response.value
    if (
        not isinstance(value, dict)
        or value.get("path") != path
        or value.get("content") != expected
        or value.get("bytes_read") != len(expected_bytes)
        or value.get("total_bytes") != len(expected_bytes)
        or value.get("truncated") is not False
    ):
        raise Phase1BaselineError("published content verification failed")


async def _reclamation_sentinel(
    *,
    product: ProductAccess,
    sessions: SessionLifecycle,
    sandbox: str,
    body: dict[str, Any],
    timeout_ms: int,
    trial_id: str,
    responses: list[TimedGatewayResponse],
    check_cancelled: Callable[[], None],
) -> dict[str, Any]:
    cycles: list[dict[str, Any]] = []
    total_cycles = body["sentinel_warmups"] + body["sentinel_cycles"]
    for cycle_index in range(total_cycles):
        check_cancelled()
        warmup = cycle_index < body["sentinel_warmups"]
        key = f"{trial_id}.sentinel.{cycle_index:02d}"
        idle = await _sample_state(product, sandbox, f"{key}.idle")
        session, created = await sessions.create_no_op(
            sandbox, "shared", request_id=f"{key}.create", timeout_ms=timeout_ms
        )
        content = _sentinel_content(cycle_index)
        path = f".eos-benchmark-phase1/sentinel/cycle-{cycle_index:02d}.txt"
        written = await product.file_write(
            sandbox,
            session_id=session.session_id,
            path=path,
            content=content,
            timeout_ms=timeout_ms,
            request_id=f"{key}.write",
        )
        published, publish_response = await sessions.publish(
            session, request_id=f"{key}.publish", timeout_ms=timeout_ms
        )
        read = await product.file_read(
            sandbox,
            session_id=None,
            path=path,
            offset=1,
            limit=2,
            timeout_ms=timeout_ms,
            request_id=f"{key}.read",
        )
        execution, execution_created = await sessions.create_no_op(
            sandbox,
            "shared",
            request_id=f"{key}.execution.create",
            timeout_ms=timeout_ms,
        )
        executed = await product.exec_command(
            sandbox,
            session_id=execution.session_id,
            command=f"wc -c < /workspace/{path}",
            timeout_ms=timeout_ms,
            request_id=f"{key}.execute",
        )
        peak = await _sample_state(product, sandbox, f"{key}.peak")
        failure: dict[str, Any]
        try:
            await product.file_edit(
                sandbox,
                session_id=execution.session_id,
                path=path,
                edits=[
                    {
                        "old_string": "stage00-missing-injected-failure-marker",
                        "new_string": "unreachable",
                    }
                ],
                timeout_ms=timeout_ms,
                request_id=f"{key}.failure",
            )
        except GatewayProductError as error:
            failure = {"kind": error.kind, "detail": error.detail}
        else:
            raise Phase1BaselineError("injected edit failure unexpectedly succeeded")
        failure_peak = await _sample_state(product, sandbox, f"{key}.failure_peak")
        destroyed = await sessions.destroy(
            execution, request_id=f"{key}.destroy", timeout_ms=timeout_ms
        )
        responses.extend(
            (
                created,
                written,
                publish_response,
                read,
                execution_created,
                executed,
                destroyed,
            )
        )
        if (
            published.publish.no_op
            or read.value.get("content") != content
            or not _command_output_matches(
                executed.value, [str(len(content.encode()))]
            )
        ):
            raise Phase1BaselineError("sentinel lifecycle correctness check failed")
        settled, polls = await _wait_settled(
            product,
            sandbox,
            key,
            body["poll_interval_ms"],
            body["settle_timeout_ms"],
            body["registry_entry_cap"],
            check_cancelled,
        )
        cycles.append(
            {
                "cycle_index": cycle_index,
                "warmup": warmup,
                "idle": idle,
                "create_write_publish_read_execute_peak": peak,
                "injected_failure": failure,
                "injected_failure_peak": failure_peak,
                "destroyed": True,
                "poll_count": polls,
                "settled": settled,
            }
        )
    measured = [cycle for cycle in cycles if not cycle["warmup"]]
    memory = [cycle["settled"]["cgroup_memory_current_bytes"] for cycle in measured]
    logical = all(cycle["settled"]["logical_release_complete"] for cycle in measured)
    saturated = any(
        cycle["settled"]["route_counter_saturated"]
        or cycle["settled"]["resource_counter_saturated"]
        for cycle in measured
    )
    available_memory = [value for value in memory if value is not None]
    first = (
        int(statistics.median(available_memory[:5]))
        if len(available_memory) == len(memory) and len(memory) >= 5
        else None
    )
    last = (
        int(statistics.median(available_memory[-5:]))
        if len(available_memory) == len(memory) and len(memory) >= 5
        else None
    )
    slope = (
        robust_slope_bytes_per_cycle([int(value) for value in memory])
        if len(available_memory) == len(memory)
        else None
    )
    verdict = reclamation_verdict(
        logical_release_complete=logical,
        counter_saturated=saturated,
        memory_samples=[int(value) for value in available_memory],
        noise_band_bytes=body["noise_band_bytes"],
    )
    return {
        "same_daemon_no_restart": True,
        "warmup_cycles": body["sentinel_warmups"],
        "measured_cycles": body["sentinel_cycles"],
        "poll_interval_ms": body["poll_interval_ms"],
        "settle_timeout_ms": body["settle_timeout_ms"],
        "raw_control_noise_band_bytes": body["noise_band_bytes"],
        "registry_entry_cap": body["registry_entry_cap"],
        "cycles": cycles,
        "settled_sample_count": len(memory),
        "settled_cgroup_memory_current_bytes": memory,
        "first_five_median_bytes": first,
        "last_five_median_bytes": last,
        "first_last_delta_bytes": None if first is None or last is None else last - first,
        "robust_slope_bytes_per_cycle": slope,
        "logical_release_complete": logical,
        "counter_saturated": saturated,
        "verdict": verdict,
        "exit_blocking": verdict
        in {"suspected-leak", "confirmed-leak", "measurement-unavailable"},
    }


async def _sample_state(
    product: ProductAccess, sandbox: str, request_id: str
) -> dict[str, Any]:
    layerstack = await product.observe_layerstack(
        sandbox, request_id=f"{request_id}.layerstack"
    )
    snapshot = await product.observe_snapshot(
        sandbox, request_id=f"{request_id}.snapshot"
    )
    cgroup = await product.observe_cgroup(sandbox, request_id=f"{request_id}.cgroup")
    _assert_legacy_route(layerstack)
    return _state_evidence(layerstack, snapshot, cgroup.series[-1].metrics.mem_cur)


async def _wait_settled(
    product: ProductAccess,
    sandbox: str,
    key: str,
    poll_interval_ms: int,
    settle_timeout_ms: int,
    registry_entry_cap: int,
    check_cancelled: Callable[[], None],
) -> tuple[dict[str, Any], int]:
    deadline = time.monotonic() + settle_timeout_ms / 1000
    poll = 0
    last: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        check_cancelled()
        last = await _sample_state(product, sandbox, f"{key}.settled.{poll:02d}")
        resources = last["resources"]
        released = (
            last["workspace_count"] == 0
            and last["active_namespace_execution_count"] == 0
            and resources["logical_cleanup_complete"]
            and all(
                resources[name] == 0
                for name in (
                    "active_operations",
                    "active_publications",
                    "active_buffers",
                    "active_tasks",
                    "active_workers",
                    "queued_items",
                    "queued_bytes",
                    "byte_permits_in_use",
                    "active_leases",
                    "open_transactions",
                    "staging_owners",
                )
            )
            and resources["live_owned_bytes"] <= resources["high_water_owned_bytes"]
            and resources["cache_entries"] <= registry_entry_cap
            and resources["registry_entries"] <= registry_entry_cap
        )
        last["logical_release_complete"] = released
        if released:
            return last, poll + 1
        poll += 1
        await asyncio.sleep(poll_interval_ms / 1000)
    raise Phase1BaselineError(
        f"sentinel resources did not settle within {settle_timeout_ms} ms: {last}"
    )


def _state_evidence(
    layerstack: LayerstackView,
    snapshot: SnapshotView,
    memory_current: int | None,
) -> dict[str, Any]:
    resources = layerstack.resources.model_dump(mode="json")
    return {
        "observation_epoch": layerstack.route.observation_epoch,
        "manifest_version": layerstack.manifest_version,
        "layer_count": len(layerstack.layers),
        "storage_logical_bytes": layerstack.storage_logical_bytes,
        "storage_allocated_bytes": layerstack.storage_allocated_bytes,
        "workspace_count": len(snapshot.workspaces),
        "active_namespace_execution_count": sum(
            len(workspace.active_namespace_executions) for workspace in snapshot.workspaces
        ),
        "cgroup_memory_current_bytes": memory_current,
        "process_rss_bytes": None,
        "process_rss_unavailable_reason": "container PID is not mapped into the host runner namespace",
        "route_counter_saturated": layerstack.route.counter_saturated,
        "resource_counter_saturated": layerstack.resources.counter_saturated,
        "resources": resources,
    }


def _assert_legacy_route(view: LayerstackView) -> None:
    route = view.route
    if (
        route.configured_mode != "legacy"
        or route.write_authority != "legacy_v1"
        or route.read_authority != "legacy_v1"
        or route.fallback_count != 0
        or route.fallback_reason_counts
        or route.mismatch_count != 0
        or route.shadow_comparison_count != 0
        or route.shadow_completed_count != 0
        or route.counter_saturated
    ):
        raise Phase1BaselineError("observed route escaped the legacy-only Stage00 contract")


def robust_slope_bytes_per_cycle(samples: list[int]) -> float | None:
    if len(samples) < 2:
        return None
    slopes = [
        (samples[right] - samples[left]) / (right - left)
        for left in range(len(samples) - 1)
        for right in range(left + 1, len(samples))
    ]
    return float(statistics.median(slopes))


def reclamation_verdict(
    *,
    logical_release_complete: bool,
    counter_saturated: bool,
    memory_samples: list[int],
    noise_band_bytes: int,
) -> str:
    if counter_saturated or len(memory_samples) < 20:
        return "measurement-unavailable"
    if not logical_release_complete:
        return "confirmed-leak"
    first = int(statistics.median(memory_samples[:5]))
    last = int(statistics.median(memory_samples[-5:]))
    delta = last - first
    slope = robust_slope_bytes_per_cycle(memory_samples)
    if slope is None:
        return "measurement-unavailable"
    slope_band = noise_band_bytes / max(1, len(memory_samples) - 1)
    if delta > noise_band_bytes * 2 and slope > slope_band * 2:
        return "confirmed-leak"
    if delta > noise_band_bytes and slope > slope_band:
        return "suspected-leak"
    if delta > 0 and slope > 0:
        return "allocator-or-page-cache-retained"
    if delta != 0:
        return "bounded-retained-by-design"
    return "bounded-and-released"


def _sentinel_content(cycle_index: int) -> str:
    prefix = f"stage00-sentinel-cycle-{cycle_index:02d}:"
    return prefix + ("s" * (4096 - len(prefix)))


def _command_output_matches(value: Any, expected_tokens: list[str]) -> bool:
    return (
        isinstance(value, dict)
        and value.get("exit_code") == 0
        and isinstance(value.get("output"), str)
        and value["output"].split() == expected_tokens
    )


def _small_files_totals_command(directory: str) -> str:
    root = shlex.quote(directory)
    return (
        f"set -eu; find {root} -type f -name 'file-*.bin' "
        "-exec sh -c 'for path do wc -c < \"$path\"; done' sh {} + "
        "| awk '{count += 1; sum += $1} "
        "END {printf \"%s %s\\n\", count, sum}'"
    )


def _sha(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


_CASE_NAMES = (
    "empty_no_op",
    "localized_edit_1k_in_1m",
    "incompressible_1m",
    "small_files_256_total_1m",
    "overwrite_history_1",
    "overwrite_history_8",
    "overwrite_history_32",
)
