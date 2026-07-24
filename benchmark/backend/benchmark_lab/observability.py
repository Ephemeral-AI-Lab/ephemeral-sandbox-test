from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from .models import StrictModel


class ObservabilityError(RuntimeError):
    pass


class CgroupMetrics(StrictModel):
    metrics_source: Literal["docker_engine"]
    cpu_usec: int | None = Field(ge=0)
    mem_cur: int | None = Field(ge=0)
    mem_max: int | None = Field(ge=0)
    io_rbytes: int | None = Field(ge=0)
    io_wbytes: int | None = Field(ge=0)


class CgroupDeltas(StrictModel):
    cpu_usec: int | None = Field(default=None, ge=0)
    io_rbytes: int | None = Field(default=None, ge=0)
    io_wbytes: int | None = Field(default=None, ge=0)


class CgroupSample(StrictModel):
    ts: int = Field(ge=0)
    sample_delta_ms: int | None = Field(ge=0)
    metrics: CgroupMetrics
    deltas: CgroupDeltas


class AppliedCgroupLimits(StrictModel):
    nano_cpus: int = Field(ge=0)
    memory_high_bytes: int = Field(ge=0)
    memory_max_bytes: int = Field(ge=0)
    pids_max: int = Field(ge=0)


class TopologyProcess(StrictModel):
    pid: int = Field(gt=0)
    namespace_pid: int = Field(gt=0)
    parent_pid: int = Field(ge=0)
    name: str = Field(min_length=1)
    state: str = Field(min_length=1)
    kind: Literal["namespace_init", "process"]
    cgroup_memberships: list[str] = Field(max_length=4096)
    resident_memory_bytes: int | None = Field(ge=0)
    cpu_time_us: int | None = Field(ge=0)
    start_time_ticks: int | None = Field(ge=0)


class TopologyWorkspace(StrictModel):
    workspace_id: str = Field(min_length=1)
    state: Literal["active", "idle", "partial"]
    holder_pid: int = Field(gt=0)
    cgroup_path: str | None
    applied_cgroup_limits: AppliedCgroupLimits | None
    workload_cgroup_state: str = Field(min_length=1)
    workload_cgroup_reason: str | None
    pid_namespace: str | None
    mount_namespace: str | None
    processes: list[TopologyProcess] = Field(max_length=512)


class DaemonRuntimeConfig(StrictModel):
    worker_threads: int | None = Field(ge=0)
    max_blocking_threads: int | None = Field(ge=0)
    blocking_thread_keep_alive_s: float | None = Field(ge=0)
    max_concurrent_connections: int | None = Field(ge=0)
    max_active_commands: int | None = Field(ge=0)
    max_blocking_queue_depth: int | None = Field(ge=0)
    max_command_queue_depth: int | None = Field(ge=0)
    infrastructure_thread_allowance: int | None = Field(ge=0)


class DaemonRuntimeUsage(StrictModel):
    active_async_tasks: int | None = Field(ge=0)
    active_blocking_tasks: int | None = Field(ge=0)
    blocking_queue_depth: int | None = Field(ge=0)
    blocking_admission_in_use: int | None = Field(ge=0)
    connection_admission_in_use: int | None = Field(ge=0)
    active_commands: int | None = Field(ge=0)
    command_queue_depth: int | None = Field(ge=0)


class DaemonOwnership(StrictModel):
    open_workspaces: int = Field(ge=0)
    live_holders: int = Field(ge=0)
    exited_unreaped_holders: int | None = Field(ge=0)
    namespace_fd_count: int | None = Field(ge=0)
    control_fd_count: int | None = Field(ge=0)
    namespace_control_fd_count: int | None = Field(ge=0)
    active_scratch_directories: int | None = Field(ge=0)
    persisted_workspace_handles: int | None = Field(ge=0)
    active_layer_leases: int | None = Field(ge=0)


class DaemonLifecycle(StrictModel):
    holder_exit_total: int = Field(ge=0)
    cleanup_attempt_total: int = Field(ge=0)
    cleanup_failure_total: int = Field(ge=0)
    cleanup_terminal_total: int = Field(ge=0)
    dropped_event_total: int = Field(ge=0)
    retained_event_count: int = Field(ge=0)
    last_holder_exit_reason: str | None
    last_cleanup_failure: str | None
    last_cleanup_result: str | None
    last_cleanup_duration_ms: int | None = Field(ge=0)


class DaemonAllocator(StrictModel):
    supported: bool
    allocated_bytes: int | None = Field(ge=0)
    active_bytes: int | None = Field(ge=0)
    mapped_bytes: int | None = Field(ge=0)
    resident_bytes: int | None = Field(ge=0)


class DiagnosticWindow(StrictModel):
    trigger: Literal["cpu", "anonymous_memory", "exited_unreaped_holder"] | None
    started_at_unix_ms: int | None = Field(ge=0)
    elapsed_ms: int = Field(ge=0)


class DiagnosticCooldown(StrictModel):
    active: bool
    until_unix_ms: int | None = Field(ge=0)
    remaining_ms: int = Field(ge=0)


class DiagnosticCpuInterval(StrictModel):
    elapsed_ms: int = Field(ge=0)
    cpu_time_delta_us: int | None = Field(ge=0)
    percent_of_one_core: float | None = Field(ge=0)


class DiagnosticMemory(StrictModel):
    resident_memory_bytes: int | None = Field(ge=0)
    proportional_set_size_bytes: int | None = Field(ge=0)
    anonymous_memory_bytes: int | None = Field(ge=0)
    private_dirty_bytes: int | None = Field(ge=0)
    anonymous_huge_pages_bytes: int | None = Field(ge=0)


class DiagnosticRedaction(StrictModel):
    workspace_file_content_excluded: bool
    environment_variables_excluded: bool
    authentication_material_excluded: bool
    full_command_lines_excluded: bool


class DiagnosticWorkspaceHolder(StrictModel):
    workspace_id: str = Field(min_length=1)
    holder_pid: int = Field(gt=0)


class DiagnosticSummary(StrictModel):
    id: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    captured_at_unix_ms: int = Field(ge=0)
    trigger: Literal["cpu", "anonymous_memory", "exited_unreaped_holder"]
    activity_classes: list[str]
    cpu_interval: DiagnosticCpuInterval
    memory: DiagnosticMemory
    thread_count: int | None = Field(ge=0)
    runtime_config: DaemonRuntimeConfig
    runtime_usage: DaemonRuntimeUsage
    ownership: DaemonOwnership
    workspace_ids: list[str]
    workspace_holders: list[DiagnosticWorkspaceHolder]
    workspace_ids_truncated: bool
    omitted_workspace_id_count: int = Field(ge=0)
    redaction: DiagnosticRedaction


class DaemonDiagnostics(StrictModel):
    enabled: bool
    max_artifact_bytes: int = Field(ge=0)
    trigger_count: int = Field(ge=0)
    active_window: DiagnosticWindow
    cooldown: DiagnosticCooldown
    latest: DiagnosticSummary | None
    last_error: str | None


class TopologyDaemon(StrictModel):
    available: bool
    error: str | None
    sampled_at_unix_ms: int = Field(ge=0)
    pid: int = Field(gt=0)
    name: str | None
    state: str | None
    virtual_memory_bytes: int | None = Field(ge=0)
    resident_memory_bytes: int | None = Field(ge=0)
    peak_resident_memory_bytes: int | None = Field(ge=0)
    proportional_set_size_bytes: int | None = Field(ge=0)
    unique_set_size_bytes: int | None = Field(ge=0)
    private_dirty_bytes: int | None = Field(ge=0)
    anonymous_huge_pages_bytes: int | None = Field(ge=0)
    anonymous_memory_bytes: int | None = Field(ge=0)
    file_memory_bytes: int | None = Field(ge=0)
    shared_memory_bytes: int | None = Field(ge=0)
    data_memory_bytes: int | None = Field(ge=0)
    swap_bytes: int | None = Field(ge=0)
    cpu_time_us: int | None = Field(ge=0)
    start_time_ticks: int | None = Field(ge=0)
    thread_count: int | None = Field(ge=0)
    file_descriptor_count: int | None = Field(ge=0)
    io_read_bytes: int | None = Field(ge=0)
    io_write_bytes: int | None = Field(ge=0)
    read_syscalls: int | None = Field(ge=0)
    write_syscalls: int | None = Field(ge=0)
    voluntary_context_switches: int | None = Field(ge=0)
    involuntary_context_switches: int | None = Field(ge=0)
    cgroup_memberships: list[str] = Field(max_length=4096)
    cgroup_path: str | None
    warnings: list[str] = Field(max_length=16)
    runtime_config: DaemonRuntimeConfig
    runtime_usage: DaemonRuntimeUsage
    ownership: DaemonOwnership
    lifecycle: DaemonLifecycle
    allocator: DaemonAllocator
    diagnostics: DaemonDiagnostics


class CgroupTopology(StrictModel):
    schema_version: Literal[2]
    available: bool
    source: str | None
    error: str | None
    truncated: bool
    warnings: list[str] = Field(max_length=16)
    workspaces: list[TopologyWorkspace] = Field(max_length=4096)
    daemon: TopologyDaemon | None = None

    @model_validator(mode="after")
    def validate_availability(self) -> "CgroupTopology":
        if self.available == (self.error is not None):
            raise ValueError("cgroup topology availability and error disagree")
        return self


class CgroupView(StrictModel):
    view: Literal["cgroup"]
    scope: Literal["sandbox"]
    availability: Literal["available", "partial"]
    errors: list[str]
    series: list[CgroupSample] = Field(max_length=4096)
    topology: CgroupTopology

    @model_validator(mode="after")
    def validate_series(self) -> "CgroupView":
        if (self.availability == "available") != (not self.errors):
            raise ValueError("cgroup availability and errors disagree")
        if self.availability == "available" and not self.series:
            raise ValueError("available cgroup response has no samples")
        previous: CgroupSample | None = None
        for sample in self.series:
            expected_interval = None if previous is None else sample.ts - previous.ts
            if expected_interval is not None and expected_interval < 0:
                raise ValueError("cgroup timestamps regressed")
            if sample.sample_delta_ms != expected_interval:
                raise ValueError("cgroup sample interval is inconsistent")
            if previous is not None:
                for metric, delta in (
                    ("cpu_usec", "cpu_usec"),
                    ("io_rbytes", "io_rbytes"),
                    ("io_wbytes", "io_wbytes"),
                ):
                    current_value = getattr(sample.metrics, metric)
                    previous_value = getattr(previous.metrics, metric)
                    expected_delta = (
                        None
                        if current_value is None or previous_value is None
                        else max(0, current_value - previous_value)
                    )
                    if getattr(sample.deltas, delta) != expected_delta:
                        raise ValueError("cgroup counter delta is inconsistent")
            previous = sample
        return self


class SnapshotMetrics(StrictModel):
    cpu_usec: int | None = Field(default=None, ge=0)
    mem_cur: int | None = Field(default=None, ge=0)
    mem_max: int | None = Field(default=None, ge=0)
    mem_max_unlimited: bool | None = None
    cgroup_available: bool | None = None
    cgroup_error: str | None = None
    disk_bytes: int | None = Field(default=None, ge=0)
    disk_allocated_bytes: int | None = Field(default=None, ge=0)
    files: int | None = Field(default=None, ge=0)
    disk_truncated: bool | None = None
    record_truncated_bytes: int | None = Field(default=None, alias="_truncated", ge=0)


class SnapshotDeltas(StrictModel):
    cpu_usec: int | None = Field(default=None, ge=0)


class SnapshotSample(StrictModel):
    ts: int = Field(ge=0)
    sample_delta_ms: int | None = None
    metrics: SnapshotMetrics
    deltas: SnapshotDeltas


class SnapshotResources(StrictModel):
    latest: SnapshotSample | None
    history: list[SnapshotSample]


class SnapshotLayers(StrictModel):
    base_root_hash: str | None
    layer_count: int | None = Field(ge=0)


class NamespaceExecution(StrictModel):
    namespace_execution_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    lifecycle_state: Literal["running"]
    command: str | None = None


class SnapshotWorkspace(StrictModel):
    workspace_id: str = Field(min_length=1)
    lifecycle_state: Literal["active"]
    finalization_state: Literal["active", "finalizing", "finalize_failed"] | None = None
    network_profile: str = Field(min_length=1)
    finalize_policy: str = Field(min_length=1)
    layers: SnapshotLayers
    namespace_fd_count: int | None = Field(ge=0)
    resources: SnapshotResources
    active_namespace_executions: list[NamespaceExecution]


class SnapshotStack(StrictModel):
    layer_count: int = Field(ge=0)
    layers_bytes: int | None = Field(ge=0)
    layers_allocated_bytes: int | None = Field(ge=0)
    storage_allocated_bytes: int | None = Field(ge=0)
    staging_entry_count: int | None = Field(ge=0)
    active_leases: int = Field(ge=0)
    route: LayerstackRoute
    resources: LayerstackResources


class EventStoreStats(StrictModel):
    dropped_storage: int = Field(ge=0)
    dropped_oversized: int = Field(ge=0)
    truncated_records: int = Field(ge=0)


class SnapshotDaemon(StrictModel):
    daemon_pid: int = Field(gt=0)
    runtime_dir: str = Field(min_length=1)
    event_store: EventStoreStats


class SnapshotView(StrictModel):
    sandbox_id: str = Field(min_length=1)
    lifecycle_state: Literal["ready"]
    availability: Literal["available", "partial"]
    sampled_at_unix_ms: int = Field(ge=0)
    errors: list[str]
    daemon: SnapshotDaemon
    resources: SnapshotResources
    workspaces: list[SnapshotWorkspace] = Field(max_length=4096)
    stack: SnapshotStack | None

    @model_validator(mode="after")
    def validate_availability(self) -> "SnapshotView":
        if (self.availability == "available") != (not self.errors):
            raise ValueError("snapshot availability and errors disagree")
        if not self.daemon.runtime_dir.startswith("/"):
            raise ValueError("snapshot runtime directory is not absolute")
        if self.resources.history or any(item.resources.history for item in self.workspaces):
            raise ValueError("snapshot unexpectedly contains history")
        identities = [item.workspace_id for item in self.workspaces]
        if len(identities) != len(set(identities)):
            raise ValueError("snapshot workspace identities are duplicated")
        return self


class Layer(StrictModel):
    layer_id: str = Field(min_length=1)
    bytes: int | None = Field(ge=0)
    allocated_bytes: int | None = Field(ge=0)
    leased_by_workspaces: int = Field(ge=0)
    booked_by: list[str]


class LayerstackRoute(StrictModel):
    schema_version: Literal[1]
    observation_epoch: int = Field(ge=0)
    configured_mode: Literal["legacy"]
    write_authority: Literal["legacy_v1"]
    read_authority: Literal["legacy_v1"]
    fallback_count: int = Field(ge=0)
    fallback_reason_counts: list[int] = Field(max_length=0)
    mismatch_count: int = Field(ge=0)
    shadow_comparison_count: int = Field(ge=0)
    shadow_completed_count: int = Field(ge=0)
    bytes_scanned: int = Field(ge=0)
    bytes_read: int = Field(ge=0)
    bytes_written: int = Field(ge=0)
    bytes_hashed: int = Field(ge=0)
    bytes_reused: int = Field(ge=0)
    bytes_newly_retained: int = Field(ge=0)
    last_quiescence_epoch: int = Field(ge=0)
    counter_saturated: bool


class LayerstackResources(StrictModel):
    schema_version: Literal[1]
    observation_epoch: int = Field(ge=0)
    live_owned_bytes: int = Field(ge=0)
    high_water_owned_bytes: int = Field(ge=0)
    active_operations: int = Field(ge=0)
    high_water_active_operations: int = Field(ge=0)
    active_publications: int = Field(ge=0)
    high_water_active_publications: int = Field(ge=0)
    active_buffers: int = Field(ge=0)
    high_water_active_buffers: int = Field(ge=0)
    active_tasks: int = Field(ge=0)
    high_water_active_tasks: int = Field(ge=0)
    active_workers: int = Field(ge=0)
    high_water_active_workers: int = Field(ge=0)
    queued_items: int = Field(ge=0)
    high_water_queued_items: int = Field(ge=0)
    queued_bytes: int = Field(ge=0)
    high_water_queued_bytes: int = Field(ge=0)
    byte_permits_in_use: int = Field(ge=0)
    high_water_byte_permits_in_use: int = Field(ge=0)
    active_leases: int = Field(ge=0)
    high_water_active_leases: int = Field(ge=0)
    open_transactions: int = Field(ge=0)
    high_water_open_transactions: int = Field(ge=0)
    staging_owners: int = Field(ge=0)
    high_water_staging_owners: int = Field(ge=0)
    cache_entries: int = Field(ge=0)
    high_water_cache_entries: int = Field(ge=0)
    registry_entries: int = Field(ge=0)
    high_water_registry_entries: int = Field(ge=0)
    open_file_descriptors: int | None = Field(ge=0)
    high_water_open_file_descriptors: int | None = Field(ge=0)
    mapped_bytes: int | None = Field(ge=0)
    high_water_mapped_bytes: int | None = Field(ge=0)
    logical_cleanup_complete: bool
    quiescence_ms: int | None = Field(ge=0)
    counter_saturated: bool


class LayerstackView(StrictModel):
    view: Literal["layerstack"]
    manifest_version: int = Field(ge=0)
    root_hash: str = Field(min_length=1)
    active_lease_count: int = Field(ge=0)
    total_bytes: int | None = Field(ge=0)
    total_allocated_bytes: int | None = Field(ge=0)
    storage_logical_bytes: int | None = Field(ge=0)
    storage_allocated_bytes: int | None = Field(ge=0)
    staging_entry_count: int | None = Field(ge=0)
    route: LayerstackRoute
    resources: LayerstackResources
    layers: list[Layer]

    @model_validator(mode="after")
    def validate_totals(self) -> "LayerstackView":
        identities = [item.layer_id for item in self.layers]
        if len(identities) != len(set(identities)):
            raise ValueError("layerstack identities are duplicated")
        if self.route.observation_epoch != self.resources.observation_epoch:
            raise ValueError("layerstack observation epochs disagree")
        if self.active_lease_count != self.resources.active_leases:
            raise ValueError("layerstack lease gauges disagree")
        if any(self.route.fallback_reason_counts):
            raise ValueError("legacy route unexpectedly contains fallback reasons")
        for total, field in (
            (self.total_bytes, "bytes"),
            (self.total_allocated_bytes, "allocated_bytes"),
        ):
            values = [getattr(item, field) for item in self.layers]
            expected = None if any(value is None for value in values) else sum(values)
            if total != expected:
                raise ValueError("layerstack total is inconsistent")
        return self


class TraceSpan(StrictModel):
    ts: int
    trace: str = Field(min_length=1)
    span: str = Field(min_length=1)
    parent: str | None = None
    name: str = Field(min_length=1)
    dur_ms: float = Field(ge=0)
    status: Literal["completed", "error", "cancelled", "timed_out"]
    attrs: dict[str, Any]


class TraceEvent(StrictModel):
    ts: int
    trace: str = Field(min_length=1)
    parent: str | None = None
    name: str = Field(min_length=1)
    attrs: dict[str, Any]


class TraceEventNode(StrictModel):
    offset_ms: float = Field(ge=0)
    event: TraceEvent


class TraceNode(StrictModel):
    span: TraceSpan
    offset_ms: float = Field(ge=0)
    children: list["TraceNode"]
    events: list[TraceEventNode]


class TraceView(StrictModel):
    view: Literal["trace"]
    trace: str = Field(min_length=1)
    spans: list[TraceNode]


def parse_cgroup(value: Any) -> CgroupView:
    return _parse(CgroupView, value, "cgroup")


def parse_snapshot(value: Any, sandbox_id: str) -> SnapshotView:
    result = _parse(SnapshotView, value, "snapshot")
    if result.sandbox_id != sandbox_id:
        raise ObservabilityError("snapshot sandbox identity mismatch")
    return result


def parse_layerstack(value: Any) -> LayerstackView:
    return _parse(LayerstackView, value, "layerstack")


def parse_trace(value: Any, request_id: str) -> TraceView:
    result = _parse(TraceView, value, "trace")
    if result.trace != request_id:
        raise ObservabilityError("trace request identity mismatch")
    seen: set[str] = set()
    remaining = 8192

    def visit(node: TraceNode, parent: str | None) -> None:
        nonlocal remaining
        remaining -= 1
        if (
            remaining < 0
            or node.span.trace != request_id
            or node.span.parent != parent
            or node.span.span in seen
            or any(
                event.event.trace != request_id
                or event.event.parent != node.span.span
                for event in node.events
            )
        ):
            raise ObservabilityError("trace tree contract failed")
        seen.add(node.span.span)
        for child in node.children:
            visit(child, node.span.span)

    for root in result.spans:
        visit(root, None)
    return result


def _parse(model: type[StrictModel], value: Any, label: str) -> Any:
    try:
        return model.model_validate(value)
    except ValidationError as error:
        raise ObservabilityError(f"product {label} response schema is invalid") from error
