import { Button, Paper, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useMemo, useState } from "react";
import { api } from "./api";
import type { EvidenceRecord, EvidenceResponse, Json } from "./types";

type ObjectValue = { [key: string]: Json };
type ScopeSummary = {
  sandbox_id?: string;
  scope?: { kind?: string; id?: string };
  sample_count?: number;
  cpu_peak_cores?: number;
  cpu_time_seconds?: number;
  memory_peak_bytes?: number;
  memory_limit_bytes?: number;
  memory_limit_unlimited?: boolean;
  io_read_bytes?: number;
  io_write_bytes?: number;
  disk_peak_bytes?: number;
  disk_allocated_peak_bytes?: number;
  file_peak?: number;
  disk_truncated?: boolean;
};

type RuntimeRecord = {
  kind: "metadata" | "sample" | "operation" | "gap";
  offset_ms: number;
  phase?: string;
  operation?: string;
  edge?: string;
  reason_code?: string;
  message?: string;
  scope?: { kind?: string; id?: string };
  metrics?: ObjectValue;
  derived?: ObjectValue;
};

type Timeline = { records: RuntimeRecord[]; error?: string };

const objectValue = (value: Json | undefined): ObjectValue | undefined =>
  value && typeof value === "object" && !Array.isArray(value) ? value as ObjectValue : undefined;

const numberValue = (value: Json | undefined): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

function parseScopes(record: EvidenceRecord): ScopeSummary[] {
  const summary = objectValue(record.summary);
  const scopes = summary?.scopes;
  if (!Array.isArray(scopes)) return [];
  return scopes.flatMap((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const value = item as ObjectValue;
    const scope = objectValue(value.scope);
    if (!scope || typeof scope.kind !== "string" || typeof scope.id !== "string") return [];
    const result: ScopeSummary = {
      sandbox_id: typeof value.sandbox_id === "string" ? value.sandbox_id : undefined,
      scope: { kind: scope.kind, id: scope.id },
    };
    for (const name of ["sample_count", "cpu_peak_cores", "cpu_time_seconds", "memory_peak_bytes", "memory_limit_bytes", "io_read_bytes", "io_write_bytes", "disk_peak_bytes", "disk_allocated_peak_bytes", "file_peak"] as const) {
      const metric = numberValue(value[name]);
      if (metric !== undefined) result[name] = metric;
    }
    result.memory_limit_unlimited = value.memory_limit_unlimited === true;
    result.disk_truncated = value.disk_truncated === true;
    return [result];
  });
}

function parseTimeline(response: EvidenceResponse | undefined): Timeline | undefined {
  if (!response) return undefined;
  if (response.kind !== "content" || response.text === undefined) return { records: [], error: "Runtime evidence was not returned as text." };
  const records: RuntimeRecord[] = [];
  for (const [index, line] of response.text.split("\n").entries()) {
    if (!line) continue;
    try {
      const value = JSON.parse(line) as unknown;
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("record is not an object");
      const record = value as Record<string, unknown>;
      if (!["metadata", "sample", "operation", "gap"].includes(String(record.kind))) throw new Error("record kind is invalid");
      if (record.kind !== "metadata" && (typeof record.offset_ms !== "number" || !Number.isFinite(record.offset_ms))) throw new Error("offset is invalid");
      records.push({ ...(record as RuntimeRecord), offset_ms: record.kind === "metadata" ? 0 : record.offset_ms as number });
    } catch {
      return { records, error: `Runtime evidence contains an invalid line at ${index + 1}.` };
    }
  }
  return records.length ? { records } : { records: [], error: "Runtime evidence contains no complete records." };
}

const formatNumber = (value: number | undefined, unit: string, digits = 2) =>
  value === undefined ? "Not published" : `${value.toLocaleString(undefined, { maximumFractionDigits: digits })}${unit}`;

function formatBytes(value: number | undefined): string {
  if (value === undefined) return "Not published";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1; }
  return `${amount.toLocaleString(undefined, { maximumFractionDigits: unit ? 2 : 0 })} ${units[unit]}`;
}

const scopeKey = (scope: ScopeSummary) => `${scope.sandbox_id ?? "sandbox"}:${scope.scope?.kind ?? "unknown"}:${scope.scope?.id ?? "unknown"}`;
const scopeLabel = (scope: ScopeSummary) => scope.scope?.kind === "all" ? "All scopes" : `${scope.scope?.kind === "workspace" ? "Workspace" : "Sandbox"} · ${scope.scope?.id ?? "unknown"}`;

function aggregateScopes(scopes: ScopeSummary[]): ScopeSummary | undefined {
  if (scopes.length < 2) return undefined;
  const maximum = (name: keyof ScopeSummary) => {
    const values = scopes.map((scope) => scope[name]).filter((value): value is number => typeof value === "number");
    return values.length ? Math.max(...values) : undefined;
  };
  const total = (name: keyof ScopeSummary) => {
    const values = scopes.map((scope) => scope[name]).filter((value): value is number => typeof value === "number");
    return values.length ? values.reduce((sum, value) => sum + value, 0) : undefined;
  };
  return {
    scope: { kind: "all", id: "all" },
    sample_count: total("sample_count"),
    cpu_peak_cores: maximum("cpu_peak_cores"),
    cpu_time_seconds: total("cpu_time_seconds"),
    memory_peak_bytes: maximum("memory_peak_bytes"),
    memory_limit_bytes: maximum("memory_limit_bytes"),
    memory_limit_unlimited: scopes.some((scope) => scope.memory_limit_unlimited),
    io_read_bytes: total("io_read_bytes"),
    io_write_bytes: total("io_write_bytes"),
    disk_peak_bytes: maximum("disk_peak_bytes"),
    disk_allocated_peak_bytes: maximum("disk_allocated_peak_bytes"),
    file_peak: maximum("file_peak"),
    disk_truncated: scopes.some((scope) => scope.disk_truncated),
  };
}

function metric(record: RuntimeRecord, group: "metrics" | "derived", name: string): number | undefined {
  const values = group === "metrics" ? record.metrics : record.derived;
  return numberValue(values?.[name]);
}

type Point = { x: number; value: number; label: string };

function reducePoints(points: Point[], limit = 240): Point[] {
  if (points.length <= limit) return points;
  const buckets = Math.max(1, Math.floor(limit / 2));
  const result: Point[] = [];
  for (let bucket = 0; bucket < buckets; bucket += 1) {
    const start = Math.floor((bucket * points.length) / buckets);
    const end = Math.max(start + 1, Math.floor(((bucket + 1) * points.length) / buckets));
    const slice = points.slice(start, end);
    const minimum = slice.reduce((candidate, point) => point.value < candidate.value ? point : candidate, slice[0]);
    const maximum = slice.reduce((candidate, point) => point.value > candidate.value ? point : candidate, slice[0]);
    result.push(...(minimum.x <= maximum.x ? [minimum, maximum] : [maximum, minimum]));
  }
  return result;
}

function paths(points: Point[], gaps: number[], x: (value: number) => number, y: (value: number) => number): string[] {
  if (!points.length) return [];
  const segments: Point[][] = [[points[0]]];
  for (let index = 1; index < points.length; index += 1) {
    const prior = points[index - 1];
    const current = points[index];
    let low = 0;
    let high = gaps.length;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (gaps[middle] <= prior.x) low = middle + 1;
      else high = middle;
    }
    if (low < gaps.length && gaps[low] <= current.x) segments.push([]);
    segments[segments.length - 1].push(current);
  }
  return segments.filter((segment) => segment.length).map((segment) => segment.map((point) => `${x(point.x)},${y(point.value)}`).join(" "));
}

function boundedOffsets(values: number[], limit = 160): number[] {
  if (values.length <= limit) return values;
  return Array.from(
    { length: limit },
    (_, index) => values[Math.floor((index * (values.length - 1)) / (limit - 1))],
  );
}

function RuntimeChart({ records, scope }: { records: RuntimeRecord[]; scope: ScopeSummary }) {
  const titleId = useId();
  const descriptionId = useId();
  const allScopes = scope.scope?.kind === "all";
  const samples = records.filter((record) => record.kind === "sample" && (allScopes || (record.scope?.kind === scope.scope?.kind && record.scope?.id === scope.scope?.id)));
  const sampleScopes = [...new Set(samples.map((record) => `${record.scope?.kind ?? "unknown"}:${record.scope?.id ?? "unknown"}`))];
  const series = sampleScopes.map((key) => samples.filter((record) => `${record.scope?.kind ?? "unknown"}:${record.scope?.id ?? "unknown"}` === key));
  const cpuSeries = series.map((values) => reducePoints(values.flatMap((record) => { const value = metric(record, "derived", "cpu_cores"); return value === undefined ? [] : [{ x: record.offset_ms, value, label: `${formatNumber(record.offset_ms / 1000, " s")} · ${formatNumber(value, " cores", 3)}` }]; })));
  const memorySeries = series.map((values) => reducePoints(values.flatMap((record) => { const value = metric(record, "metrics", "mem_cur"); return value === undefined ? [] : [{ x: record.offset_ms, value, label: `${formatNumber(record.offset_ms / 1000, " s")} · ${formatBytes(value)}` }]; })));
  const cpu = cpuSeries.flat();
  const memory = memorySeries.flat();
  const gaps = records.filter((record) => record.kind === "gap" && (allScopes || !record.scope || (record.scope.kind === scope.scope?.kind && record.scope.id === scope.scope?.id))).map((record) => record.offset_ms).sort((left, right) => left - right);
  const gapMarkers = boundedOffsets(gaps);
  const markers = records.filter((record) => record.kind === "operation");
  const maximumOffset = Math.max(1, ...samples.map((sample) => sample.offset_ms), ...markers.map((marker) => marker.offset_ms), ...gaps);
  const x = (value: number) => 54 + (value / maximumOffset) * 642;
  const yScale = (points: Point[], top: number) => { const maximum = Math.max(1, ...points.map((point) => point.value)); return (value: number) => top + 58 - (value / maximum) * 52; };
  const cpuY = yScale(cpu, 19);
  const memoryY = yScale(memory, 123);
  const cpuPaths = cpuSeries.flatMap((values) => paths(values, gaps, x, cpuY));
  const memoryPaths = memorySeries.flatMap((values) => paths(values, gaps, x, memoryY));
  const peakCpu = cpu.length ? Math.max(...cpu.map((point) => point.value)) : undefined;
  const endingMemory = [...memory].sort((left, right) => left.x - right.x).at(-1)?.value;
  const accessibleSummary = `Peak CPU ${formatNumber(peakCpu, " cores", 3)}; ending memory ${formatBytes(endingMemory)}; ${samples.length} samples; ${gaps.length} gaps.`;
  return <div className="runtime-chart-wrap">
    <svg className="runtime-chart" viewBox="0 0 720 220" role="img" tabIndex={0} aria-labelledby={`${titleId} ${descriptionId}`}>
      <title id={titleId}>Runtime resource timeline for {scopeLabel(scope)}</title>
      <desc id={descriptionId}>{accessibleSummary} Lines split at recorded gaps; markers identify phase and operation boundaries.</desc>
      <g className="chart-grid"><line x1="54" y1="77" x2="696" y2="77" /><line x1="54" y1="181" x2="696" y2="181" /><line x1="54" y1="19" x2="54" y2="181" /></g>
      <text x="8" y="34">CPU</text><text x="8" y="138">Memory</text><text x="650" y="211">Elapsed</text>
      {cpuPaths.map((line, index) => <polyline key={`cpu-${index}`} className="chart-cpu" points={line} />)}
      {memoryPaths.map((line, index) => <polyline key={`memory-${index}`} className="chart-memory" points={line} />)}
      {gapMarkers.map((gap, index) => <g key={`gap-${index}`} className="chart-gap"><line x1={x(gap)} y1="19" x2={x(gap)} y2="181" strokeDasharray="4 4" /><title>Gap at {formatNumber(gap / 1000, " seconds")}</title></g>)}
      {markers.slice(0, 80).map((marker, index) => { const label = `${marker.phase ?? "unknown phase"}: ${marker.operation ?? "operation"} ${marker.edge ?? "marker"} at ${formatNumber(marker.offset_ms / 1000, " seconds")}`; return <g key={`marker-${index}`} className={marker.operation === "case_failure" ? "chart-marker chart-failure" : "chart-marker"} tabIndex={0} focusable="true" role="img" aria-label={label}><line x1={x(marker.offset_ms)} y1="13" x2={x(marker.offset_ms)} y2="187" /><title>{label}</title></g>; })}
      {cpu.slice(0, 160).map((point, index) => <circle key={`cpu-point-${index}`} className="chart-cpu-point" cx={x(point.x)} cy={cpuY(point.value)} r="3" tabIndex={0}><title>{point.label}</title></circle>)}
      {memory.slice(0, 160).map((point, index) => <circle key={`memory-point-${index}`} className="chart-memory-point" cx={x(point.x)} cy={memoryY(point.value)} r="3" tabIndex={0}><title>{point.label}</title></circle>)}
    </svg>
    {!cpu.length && !memory.length ? <Text size="sm" className="runtime-empty-chart">No plottable CPU or memory values were published for this scope.</Text> : null}
  </div>;
}

function statusMessage(status: string): string {
  if (status === "not_applicable") return "This case did not own a sandbox, so runtime collection was not applicable.";
  if (status === "unsupported") return "The product observability boundary does not support runtime collection for this case.";
  if (status === "unavailable") return "Runtime collection ran, but no usable samples were available.";
  if (status === "invalid") return "Runtime evidence is invalid and is not presented as resource truth.";
  if (status === "partial") return "Runtime evidence is partial; coverage and gaps remain explicit below.";
  return "Runtime evidence is available.";
}

export function RuntimeResources({ runId, record, purged, onEvidence }: { runId: string; record: EvidenceRecord | undefined; purged: boolean; onEvidence: (record: EvidenceRecord) => void }) {
  const [expanded, setExpanded] = useState(false);
  const recordStatus = record && (typeof record.status === "string" ? record.status : record.availability ?? "invalid");
  const scopes = useMemo(() => record && recordStatus !== "invalid" ? parseScopes(record) : [], [record, recordStatus]);
  const selectableScopes = useMemo(() => { const aggregate = aggregateScopes(scopes); return aggregate ? [aggregate, ...scopes] : scopes; }, [scopes]);
  const [selectedScope, setSelectedScope] = useState("");
  useEffect(() => { setExpanded(false); setSelectedScope(scopes[0] ? scopeKey(scopes[0]) : ""); }, [record?.evidence_id]);
  const activeScope = selectableScopes.find((scope) => scopeKey(scope) === selectedScope) ?? scopes[0];
  const evidenceId = record?.evidence_id;
  const evidence = useQuery({
    queryKey: ["evidence", runId, evidenceId ?? "metadata-only", record?.storage_ref ?? "inline"],
    queryFn: () => api.evidence(runId, evidenceId ?? "", Boolean(record?.storage_ref)),
    enabled: Boolean(expanded && evidenceId && record?.storage_ref && !purged),
    retry: false,
    staleTime: Infinity,
  });
  const timeline = useMemo(() => parseTimeline(evidence.data), [evidence.data]);
  if (!record) return <section className="runtime-resources" aria-labelledby="runtime-resources-title"><div className="runtime-heading"><div><Text className="eyebrow">Supporting evidence</Text><Title id="runtime-resources-title" order={3}>Runtime resources</Title></div><span className="runtime-status">Not published</span></div><Text size="sm">No runtime observability record was published for this case.</Text></section>;
  const status = recordStatus ?? "invalid";
  const coverage = objectValue(record.coverage);
  const errors = Array.isArray(record.errors) ? record.errors : [];
  const terminalWithoutTimeline = ["not_applicable", "unsupported", "unavailable", "invalid"].includes(status);
  return <section className="runtime-resources" aria-labelledby="runtime-resources-title">
    <div className="runtime-heading"><div><Text className="eyebrow">Supporting evidence</Text><Title id="runtime-resources-title" order={3}>Runtime resources</Title></div><span className={`runtime-status runtime-status-${status}`}>{status.replaceAll("_", " ")}</span></div>
    <Text size="sm">{purged ? "Runtime evidence was purged; durable summary metadata remains." : statusMessage(status)}</Text>
    {scopes.length ? <label className="runtime-scope"><span>Scope</span><select value={activeScope ? scopeKey(activeScope) : ""} onChange={(event) => setSelectedScope(event.target.value)}>{selectableScopes.map((scope) => <option key={scopeKey(scope)} value={scopeKey(scope)}>{scopeLabel(scope)}</option>)}</select></label> : status === "available" || status === "partial" ? <Text size="sm" className="runtime-warning">No scope summary was published.</Text> : null}
    {activeScope ? <>
      <dl className="runtime-facts">
        <div><dt>CPU peak</dt><dd>{formatNumber(activeScope.cpu_peak_cores, " cores", 3)}</dd></div><div><dt>CPU time</dt><dd>{formatNumber(activeScope.cpu_time_seconds, " s", 3)}</dd></div>
        <div><dt>Memory peak</dt><dd>{formatBytes(activeScope.memory_peak_bytes)}</dd></div><div><dt>Memory limit</dt><dd>{activeScope.memory_limit_unlimited ? "Unlimited" : formatBytes(activeScope.memory_limit_bytes)}</dd></div>
        <div><dt>Container read</dt><dd>{formatBytes(activeScope.io_read_bytes)}</dd></div><div><dt>Container write</dt><dd>{formatBytes(activeScope.io_write_bytes)}</dd></div>
        <div><dt>Workspace disk</dt><dd>{formatBytes(activeScope.disk_peak_bytes)}</dd></div><div><dt>Allocated disk</dt><dd>{formatBytes(activeScope.disk_allocated_peak_bytes)}</dd></div>
        <div><dt>Workspace files</dt><dd>{formatNumber(activeScope.file_peak, "", 0)}</dd></div><div><dt>Samples</dt><dd>{formatNumber(activeScope.sample_count, "", 0)}</dd></div>
      </dl>
      {activeScope.memory_limit_unlimited ? <Paper className="runtime-warning" role="status">Memory is unlimited for this scope; absence of a byte limit is not zero.</Paper> : null}
      {activeScope.disk_truncated ? <Paper className="runtime-warning" role="status">Workspace disk accounting was truncated; displayed disk and file values are lower bounds.</Paper> : null}
    </> : null}
    <div className="runtime-coverage" aria-label="Runtime coverage"><Text size="xs">Coverage</Text><Text size="sm">Observed {formatNumber(numberValue(coverage?.observed_ticks), "", 0)} of {formatNumber(numberValue(coverage?.expected_ticks), "", 0)} expected ticks · {formatNumber(numberValue(coverage?.missed_ticks), "", 0)} missed · {scopes.length} {scopes.length === 1 ? "scope" : "scopes"}.</Text></div>
    {errors.length ? <div className="runtime-gaps"><Text size="xs">Coverage gaps</Text><ul>{errors.slice(0, 16).map((error, index) => { const value = objectValue(error); return <li key={index}>{typeof value?.message === "string" ? value.message : "An unnamed collection gap was published."}{numberValue(value?.count) !== undefined ? ` (${numberValue(value?.count)})` : ""}</li>; })}</ul></div> : <Text size="sm" className="runtime-no-gaps">No collection gaps were published.</Text>}
    {!terminalWithoutTimeline && !purged ? <div className="runtime-actions"><Button size="xs" variant="light" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>{expanded ? "Hide timeline" : "Load timeline"}</Button>{record.storage_ref && <Button size="xs" variant="subtle" onClick={() => onEvidence(record)}>Raw evidence</Button>}</div> : record.storage_ref && !purged ? <div className="runtime-actions"><Button size="xs" variant="subtle" onClick={() => onEvidence(record)}>Raw evidence</Button></div> : null}
    {purged ? <Text size="sm" className="runtime-warning">The retained NDJSON cannot be loaded because this run is purged.</Text> : null}
    {expanded && evidence.isPending ? <Text size="sm" role="status">Loading runtime timeline…</Text> : null}
    {expanded && evidence.isError ? <Text size="sm" className="runtime-warning" role="alert">Runtime evidence could not be loaded.</Text> : null}
    {expanded && timeline?.error ? <Text size="sm" className="runtime-warning" role="alert">{timeline.error}</Text> : null}
    {expanded && timeline && !timeline.error && activeScope ? <><RuntimeChart records={timeline.records} scope={activeScope} /><div className="runtime-markers"><Text size="xs">Phase, operation, and failure markers</Text><ul>{timeline.records.filter((entry) => entry.kind === "operation").slice(0, 40).map((entry, index) => <li key={index}><strong>{entry.phase ?? "unknown phase"}</strong> · {entry.operation ?? "operation"} · {entry.edge ?? "marker"} · {formatNumber(entry.offset_ms / 1000, " s")}</li>)}</ul></div></> : null}
  </section>;
}
