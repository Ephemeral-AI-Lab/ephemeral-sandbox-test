import {
  AppShell,
  Badge,
  Button,
  Card,
  Checkbox,
  Divider,
  Drawer,
  Group,
  Loader,
  Modal,
  Paper,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
  Tooltip,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, NavLink, Navigate, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from "react-router";
import { type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { ApiError, api } from "./api";
import { AsyncStateNotice } from "./state-copy";
import type { CatalogCase, CatalogPage, Preview, RunProjection } from "./types";

const catalogKey = (search: URLSearchParams) => ["catalog", search.toString()] as const;

function humanize(value: string | undefined): string {
  return (value || "Unknown").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function Status({ value }: { value: string | undefined }) {
  const state = value || "unknown";
  return <Badge className={`status status-${state}`} variant="light">{humanize(state)}</Badge>;
}

function useControllerEvents() {
  const location = useLocation();
  const queryClient = useQueryClient();
  const source = useRef<EventSource | null>(null);
  const [connection, setConnection] = useState<"live" | "reconnecting">("live");

  useEffect(() => {
    const runId = /^\/e2e\/runs\/([^/]+)$/.exec(location.pathname)?.[1];
    const after = runId ? Number(queryClient.getQueryData<RunProjection>(["run", runId])?.applied_through_seq ?? 0) : 0;
    const path = runId ? `/api/v1/events?run_id=${encodeURIComponent(runId)}&after=${after}` : "/api/v1/events";
    source.current?.close();
    const stream = new EventSource(path);
    source.current = stream;
    let resynced = false;
    const refreshSnapshot = () => {
      if (resynced) return;
      resynced = true;
      setConnection("reconnecting");
      if (runId) void queryClient.refetchQueries({ queryKey: ["run", runId], type: "active" });
    };
    stream.onopen = () => { resynced = false; setConnection("live"); };
    stream.onerror = () => refreshSnapshot();
    stream.onmessage = (event) => {
      resynced = false;
      setConnection("live");
      if (runId) queryClient.invalidateQueries({ queryKey: ["run", runId] });
      if (event.lastEventId && runId) queryClient.invalidateQueries({ queryKey: ["runs"] });
    };
    stream.addEventListener("catalog.revision", () => void queryClient.refetchQueries({ queryKey: ["catalog"], type: "active" }));
    stream.addEventListener("stream.gap", refreshSnapshot);
    return () => stream.close();
  }, [location.pathname, queryClient]);
  return connection;
}

function Shell({ children }: { children: ReactNode }) {
  const [healthOpened, healthControls] = useDisclosure(false);
  const health = useQuery({ queryKey: ["health"], queryFn: () => api.health() });
  const connection = useControllerEvents();
  return (
    <AppShell header={{ height: { base: 168, sm: 64 } }} padding="md">
      <AppShell.Header className="control-header">
        <Group h="100%" justify="space-between" px="md" wrap="nowrap">
          <Group gap="md" wrap="nowrap">
            <Title order={1} size="h3">E2E Control Room</Title>
            <nav aria-label="Primary">
              <Group gap="xs" wrap="nowrap">
                <Button component={NavLink} to="/e2e/catalog" variant="subtle" size="compact-md">Catalog</Button>
                <Button component={NavLink} to="/e2e/runs" variant="subtle" size="compact-md">Runs</Button>
                <Button component={NavLink} to="/e2e/workspaces" variant="subtle" size="compact-md">Workspaces</Button>
              </Group>
            </nav>
          </Group>
          <Button onClick={healthControls.open} variant="light" aria-label="Open runner health">Health <span aria-hidden="true">●</span></Button>
        </Group>
      </AppShell.Header>
      <nav aria-label="Skip links"><a className="skip-link" href="#main-content">Skip to main content</a></nav>
      <AppShell.Main><div id="main-content" tabIndex={-1}>{connection === "reconnecting" && <AsyncStateNotice state="reconnecting" />}{children}</div></AppShell.Main>
      <Drawer opened={healthOpened} onClose={healthControls.close} title="Runner health" position="right" size="lg">
        {health.isPending ? <Loading label="Loading runner health…" /> : health.isError ? <Failure error={health.error} headline="Runner health is unavailable." /> : <HealthPanel health={health.data} />}
      </Drawer>
    </AppShell>
  );
}

function HealthPanel({ health }: { health: Awaited<ReturnType<typeof api.health>> }) {
  return <Stack>
    <Card withBorder><Text fw={700}>Can I browse?</Text><Status value={String(health.catalog.state ?? "ready")} /><Text size="sm">Current revision: {String(health.catalog.current_revision ?? "unavailable")}</Text></Card>
    <Card withBorder><Text fw={700}>Can I start?</Text><Text size="sm">{health.lane.active_run_id ? `Run ${health.lane.active_run_id} owns the execution lane.` : "No run owns the execution lane."}</Text></Card>
    <Card withBorder><Text fw={700}>Is storage safe?</Text><Stack gap="xs">{Object.entries(health.roots).map(([name, value]) => <Text key={name} className="identity" size="xs">{name}: {value}</Text>)}</Stack></Card>
    <Button component={Link} to="/e2e/catalog?kind=harness&runnable=true" variant="light">Review runnable harness diagnostics</Button>
  </Stack>;
}

function Loading({ label, detail }: { label: string; detail?: string }) {
  return <Stack align="center" py="xl" aria-live="polite"><Loader /><Text>{label}</Text>{detail && <Text size="sm" c="dimmed">{detail}</Text>}</Stack>;
}

function Failure({ error, headline }: { error: unknown; headline: string }) {
  const detail = error instanceof ApiError ? `${error.response.message} Request ${error.response.request_id}.` : "The controller could not be read safely.";
  return <Paper className="failure" role="status" p="md"><Title order={2} size="h4">{headline}</Title><Text>{detail}</Text></Paper>;
}

function CatalogPage() {
  const [params, setParams] = useSearchParams();
  const [searchInput, setSearchInput] = useState(params.get("q") ?? "");
  const [selected, setSelected] = useState<Map<string, CatalogCase>>(new Map());
  const [selectionRevision, setSelectionRevision] = useState<string | null>(null);
  const [reviewOpened, reviewControls] = useDisclosure(false);
  const search = new URLSearchParams(params);
  const catalog = useQuery({ queryKey: catalogKey(search), queryFn: () => api.catalog(search) });
  const refresh = useMutation({ mutationFn: () => api.refreshCatalog(), retry: false, onSuccess: () => catalog.refetch() });

  useEffect(() => {
    if (catalog.data && selectionRevision && selectionRevision !== catalog.data.catalog_revision) {
      setSelected(new Map());
      setSelectionRevision(null);
    }
  }, [catalog.data, selectionRevision]);

  function setFilter(name: string, value?: string) {
    const next = new URLSearchParams(params);
    value ? next.set(name, value) : next.delete(name);
    if (name !== "cursor") next.delete("cursor");
    setParams(next);
  }

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    setFilter("q", searchInput.trim() || undefined);
  }

  function toggleCase(item: CatalogCase) {
    const identity = `${item.test_id}:${item.case_id}`;
    setSelected((prior) => {
      const next = new Map(prior);
      next.has(identity) ? next.delete(identity) : next.set(identity, item);
      return next;
    });
    setSelectionRevision(catalog.data?.catalog_revision ?? null);
  }

  if (catalog.isPending) return <Page title="Catalog"><Loading label="Loading test catalog…" detail="Reading the current published revision." /></Page>;
  if (catalog.isError) return <Page title="Catalog"><Failure error={catalog.error} headline="Test catalog is unavailable." /><Button onClick={() => catalog.refetch()}>Refresh catalog</Button></Page>;
  const data = catalog.data;
  return <Page title="Catalog" description="Find, understand, select, and review cases from the current published catalog.">
    <Group justify="space-between" align="end" mb="md">
      <form onSubmit={submitSearch} className="search-form"><TextInput label="Search" placeholder="Search behavior, purpose, feature, validation, owner, or ID" value={searchInput} onChange={(event) => setSearchInput(event.currentTarget.value)} /><Button type="submit">Search</Button></form>
      <Button variant="light" loading={refresh.isPending} onClick={() => refresh.mutate()}>{refresh.isPending ? "Updating test catalog — inputs changed." : "Refresh catalog"}</Button>
    </Group>
    {refresh.isPending && <Text role="status" size="sm">Recollecting now; revision {data.catalog_revision} remains the last verified catalog.</Text>}
    {refresh.isError && <AsyncStateNotice state="catalog_invalid_last_good" detail="The controller rejected the update. Admission is blocked until a valid refresh succeeds." />}
    {selectionRevision && selectionRevision !== data.catalog_revision && <Paper className="failure" p="sm">Review is out of date. The catalog revision changed, so the prior scope was cleared.</Paper>}
    <CatalogTopology data={data} activeDomain={params.get("domain_id")} activeFamily={params.get("family_id")} onFilter={setFilter} />
    <div role="group" aria-label="Applied filters"><Group gap="xs" my="md">{["q", "domain_id", "family_id", "kind"].map((name) => params.get(name) && <Button key={name} variant="light" size="compact-md" onClick={() => setFilter(name)}>{humanize(name)}: {params.get(name)} ×</Button>)}</Group></div>
    {data.items.length === 0 ? <EmptyCatalog onClear={() => setParams(new URLSearchParams())} /> : <CatalogResults data={data} selected={selected} onToggle={toggleCase} onPage={(cursor) => setFilter("cursor", cursor ?? undefined)} />}
    <SelectionTray selected={selected} revision={data.catalog_revision} onReview={reviewControls.open} />
    <ReviewDialog opened={reviewOpened} onClose={reviewControls.close} revision={data.catalog_revision} selected={selected} />
  </Page>;
}

function Page({ title, description, children }: { title: string; description?: string; children: ReactNode }) {
  const location = useLocation();
  const heading = useRef<HTMLElement | null>(null);
  useEffect(() => { heading.current?.focus(); }, [location.pathname, title]);
  return <Stack className="page" gap="md"><header ref={heading} tabIndex={-1}><Title order={2}>{title}</Title>{description && <Text c="dimmed">{description}</Text>}</header>{children}</Stack>;
}

function EmptyCatalog({ onClear }: { onClear: () => void }) {
  return <Paper withBorder p="xl"><Title order={3}>No cases match these filters.</Title><Text>Clear a filter or change the search.</Text><Button mt="sm" onClick={onClear}>Clear filters</Button></Paper>;
}

function CatalogTopology({ data, activeDomain, activeFamily, onFilter }: { data: CatalogPage; activeDomain: string | null; activeFamily: string | null; onFilter: (name: string, value?: string) => void }) {
  const domains = Object.entries(data.facets.domain_id).sort(([left], [right]) => left.localeCompare(right));
  const families = Object.entries(data.facets.family_id).sort(([left], [right]) => left.localeCompare(right));
  const isHarness = (item: CatalogCase) => item.kind === "harness";
  const primary = domains.filter(([domain]) => data.items.some((item) => item.domain_id === domain && !isHarness(item)));
  const harnessCount = data.items.filter(isHarness).length;
  return <Stack gap="xs"><Text fw={700}>Domains</Text><SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }}>{primary.map(([domain, count]) => <Card key={domain} withBorder className={activeDomain === domain ? "active-card" : ""}><Button fullWidth variant="subtle" onClick={() => onFilter("domain_id", domain)}>{humanize(domain)} · {count} cases</Button></Card>)}</SimpleGrid>
    {harnessCount > 0 && <Card withBorder className="subordinate"><Button fullWidth variant="subtle" onClick={() => onFilter("kind", "harness")}>Harness Diagnostics · {harnessCount} cases</Button></Card>}
    <Group gap="xs" aria-label="Families">{families.map(([family, count]) => <Button key={family} variant={activeFamily === family ? "filled" : "light"} size="compact-md" onClick={() => onFilter("family_id", family)}>{humanize(family)} ({count})</Button>)}</Group>
  </Stack>;
}

function CatalogResults({ data, selected, onToggle, onPage }: { data: CatalogPage; selected: Map<string, CatalogCase>; onToggle: (item: CatalogCase) => void; onPage: (cursor: string | null) => void }) {
  const [detail, setDetail] = useState<CatalogCase | null>(data.items[0] ?? null);
  useEffect(() => setDetail(data.items[0] ?? null), [data.items]);
  return <div className="catalog-layout"><section aria-label="Catalog cases"><Text fw={700} mb="xs">{data.total} matching cases</Text><Stack gap="xs">{data.items.map((item) => {
    const identity = `${item.test_id}:${item.case_id}`;
    return <Card key={identity} withBorder className={detail?.test_id === item.test_id && detail.case_id === item.case_id ? "case-row active-card" : "case-row"}>
      <Group align="flex-start" wrap="nowrap"><Checkbox aria-label={`Select ${item.title}`} checked={selected.has(identity)} onChange={() => onToggle(item)} mt={3} /><button className="case-open" type="button" onClick={() => setDetail(item)}><Text fw={700}>{item.title}</Text><Text size="sm">{item.purpose ?? item.description ?? "No purpose was published."}</Text><Text size="xs" c="dimmed">{[item.domain_id, item.family_id, item.group_id].filter(Boolean).map(humanize).join(" · ")} · {item.validations?.length ?? 0} validations · {item.execution_surface ? `${humanize(item.execution_surface)} boundary` : "Harness diagnostic — no product boundary claimed"}</Text></button><Status value={item.runnable ? "ready" : "blocked"} /></Group>
    </Card>;
  })}</Stack><Group justify="space-between" mt="sm"><Text size="sm">Page limit {data.page.limit}</Text><Button disabled={!data.page.next_cursor} onClick={() => onPage(data.page.next_cursor)}>Next page</Button></Group></section><CatalogDetail item={detail} /></div>;
}

function CatalogDetail({ item }: { item: CatalogCase | null }) {
  if (!item) return null;
  return <aside aria-label="Case detail"><Paper withBorder p="md"><Title order={3}>{item.title}</Title><Text mt="xs">{item.purpose ?? item.description ?? "No purpose was published."}</Text><Divider my="sm" /><Text fw={700}>Validations</Text><Stack gap={4}>{item.validations?.map((validation) => <Text key={validation.id} size="sm">{validation.required === false ? "Optional" : "Required"} · {validation.id}{validation.phase ? ` · ${validation.phase}` : ""}</Text>) ?? <Text size="sm">No named validations were published.</Text>}</Stack><Divider my="sm" />{item.kind === "harness" ? <Text>Harness diagnostic — no product boundary claimed</Text> : <Text>Product coverage: {(item.effective_features ?? []).join(", ") || "No feature metadata"}</Text>}<Text className="identity" size="xs" mt="sm">{item.test_id} · {item.case_id}</Text></Paper></aside>;
}

function SelectionTray({ selected, revision, onReview }: { selected: Map<string, CatalogCase>; revision: string; onReview: () => void }) {
  return <Paper className="selection-tray" withBorder p="sm"><Group justify="space-between"><Text>{selected.size} selected cases · exact catalog revision {revision.slice(0, 14)}…</Text><Tooltip label={selected.size ? "Review the exact selected scope" : "Select at least one case before reviewing"}><span><Button onClick={onReview} disabled={!selected.size}>Review run</Button></span></Tooltip></Group></Paper>;
}

function ReviewDialog({ opened, onClose, revision, selected }: { opened: boolean; onClose: () => void; revision: string; selected: Map<string, CatalogCase> }) {
  const navigate = useNavigate();
  const selection = [...selected.values()].map((item) => ({ case: { test_id: item.test_id, case_id: item.case_id } }));
  const selectionKey = selection.map((item) => `${item.case.test_id}:${item.case.case_id}`).join("|");
  const preview = useMutation({ mutationFn: () => api.preview({ schema_version: 1, catalog_revision: revision, include: selection, exclude: [] }), retry: false });
  const admit = useMutation({ mutationFn: (value: Preview) => api.admit(value), retry: false, onSuccess: (run) => { onClose(); navigate(`/e2e/runs/${run.run_id}`); } });
  useEffect(() => { if (!opened) return; preview.reset(); preview.mutate(); }, [opened, revision, selectionKey]);
  const current = preview.data;
  return <Modal opened={opened} onClose={onClose} title="Review run" fullScreen={false} size="lg"><Stack>{preview.isPending && <Loading label="Checking run readiness…" detail="Start is unavailable while readiness is verified." />}{preview.isError && <Failure error={preview.error} headline="Readiness check failed." />}{current && <><Title order={3}>{current.state === "ready" ? `Ready to start ${current.case_count} exact cases.` : `Run blocked — ${current.blockers?.[0]?.reason_code ?? current.state}.`}</Title><Text>Scope is frozen at revision {current.catalog_revision} until {current.expires_at ?? "the review expires"}.</Text><Card withBorder><Text fw={700}>Boundaries and preflight</Text>{current.preflight?.map((check, index) => <Text key={index} size="sm">{check.state === "ready" ? "✓" : "!"} {check.label ?? "Check"}: {check.message ?? check.state}</Text>)}</Card>{admit.isPending && <AsyncStateNotice state="admission_pending" detail={`The reviewed ${current.case_count} cases are being admitted; duplicate Start is disabled.`} />}<Group justify="flex-end"><Button variant="default" onClick={onClose}>Cancel</Button><Tooltip label={current.state === "ready" ? "Admit the exact reviewed scope" : current.blockers?.[0]?.message ?? "Start is unavailable while readiness is checked"}><span><Button loading={admit.isPending} disabled={current.state !== "ready"} onClick={() => admit.mutate(current)}>Start run</Button></span></Tooltip></Group>{admit.isError && <Failure error={admit.error} headline="Starting one run failed." />}</>}</Stack></Modal>;
}

function RunsPage() {
  const runs = useQuery({ queryKey: ["runs"], queryFn: () => api.runs() });
  if (runs.isPending) return <Page title="Runs"><Loading label="Loading run history…" /></Page>;
  if (runs.isError) return <Page title="Runs"><Failure error={runs.error} headline="Run history is unavailable." /></Page>;
  return <Page title="Runs" description="Current and historical run records.">{runs.data.history_state === "partial" && <Paper className="failure" p="sm">History is partial. {runs.data.corrupt_records} run records could not be read; visible rows remain verified.</Paper>}{runs.data.items.length ? <Table striped highlightOnHover><Table.Thead><Table.Tr><Table.Th>Run</Table.Th><Table.Th>Result</Table.Th><Table.Th>Created</Table.Th><Table.Th>Evidence</Table.Th></Table.Tr></Table.Thead><Table.Tbody>{runs.data.items.map((run) => <Table.Tr key={run.run_id}><Table.Td><Link to={`/e2e/runs/${run.run_id}`}>{run.run_id}</Link></Table.Td><Table.Td><Status value={run.state} /></Table.Td><Table.Td>{run.created_at ?? "—"}</Table.Td><Table.Td>{run.evidence_health ?? "—"}</Table.Td></Table.Tr>)}</Table.Tbody></Table> : <Paper withBorder p="xl"><Title order={3}>No runs yet.</Title><Text>Start from Catalog to create the first run.</Text><Button component={Link} to="/e2e/catalog" mt="sm">Open Catalog</Button></Paper>}</Page>;
}

function RunPage() {
  const { runId = "" } = useParams();
  const queryClient = useQueryClient();
  const run = useQuery({ queryKey: ["run", runId], queryFn: () => api.run(runId) });
  const cancel = useMutation({ mutationFn: () => api.cancelRun(runId), retry: false, onSuccess: () => queryClient.invalidateQueries({ queryKey: ["run", runId] }) });
  const purge = useMutation({ mutationFn: () => api.purgeRun(runId), retry: false, onSuccess: () => queryClient.invalidateQueries({ queryKey: ["run", runId] }) });
  if (run.isPending) return <Page title="Run"><Loading label="Loading verified run state…" /></Page>;
  if (run.isError) return <Page title="Run"><Failure error={run.error} headline="Run data is unavailable." /></Page>;
  const data = run.data;
  const failure = data.failures?.find((item) => item.id === data.first_failure_id) ?? data.failures?.[0];
  const terminal = ["passed", "failed", "error", "cancelled"].includes(data.state);
  const terminalState = data.state === "passed" ? "terminal_passed" : data.state === "failed" ? "terminal_failed" : data.state === "error" ? "terminal_error" : null;
  const truncation = data.evidence_summary?.truncation;
  return <Page title={`Run ${data.run_id}`}><Group justify="space-between"><Group><Status value={data.state} /><Text>{Object.entries(data.case_counts ?? {}).map(([state, count]) => `${count} ${humanize(state).toLowerCase()}`).join(" · ")}</Text></Group><Group><Button disabled={terminal} loading={cancel.isPending} onClick={() => cancel.mutate()}>Cancel</Button><Button disabled={!terminal} loading={purge.isPending} variant="light" onClick={() => purge.mutate()}>Purge retained evidence</Button></Group></Group>{terminalState && <AsyncStateNotice state={terminalState} detail={data.state === "failed" ? `First failure: ${failure?.message ?? "unknown"}. Primary cause: ${data.primary_failure_id ?? "unknown"}. Cleanup and evidence remain explicit.` : undefined} />}{failure && <Paper className="failure" p="md" aria-live="polite"><Title order={3}>First failure</Title><Text>{failure.message ?? "A named failure was recorded."}</Text><Text size="sm">Severity: {failure.severity ?? "unknown"}</Text></Paper>}{data.evidence_health === "degraded" && <AsyncStateNotice state="evidence_degraded" detail="Evidence retained with the degraded reason shown below." />}<Paper withBorder p="md"><Group justify="space-between"><Text fw={700}>Evidence health: {humanize(data.evidence_health)}</Text><Text>Stream verified through sequence {data.applied_through_seq ?? 0}</Text></Group>{truncation && <Text role="status" size="sm">Evidence capped: retained {truncation.retained_bytes ?? "unknown"} bytes; {truncation.omitted_bytes ?? "unknown"} bytes and {truncation.omitted_lines ?? "unknown"} lines omitted.</Text>}</Paper><Stack gap="xs">{data.cases?.map((item, index) => <Card withBorder key={`${item.test_id}:${item.case_id}`}><Group justify="space-between"><Text fw={700}>{index + 1}. {item.title ?? item.test_id}</Text><Status value={item.state} /></Group>{item.state === "not_run" && <Text size="sm">Not run · fail-fast</Text>}{Object.entries(item.validations ?? {}).map(([name, validation]) => <Text key={name} size="sm">Validation: {name} · {humanize(validation.state)}</Text>)}{Object.entries(item.cleanup ?? {}).map(([name, cleanup]) => <Text key={name} size="sm">Cleanup: {name} · {humanize(cleanup.state)}</Text>)}</Card>)}</Stack>{cancel.isError && <Failure error={cancel.error} headline="Cancellation could not be requested." />}{purge.isError && <Failure error={purge.error} headline="Purge incomplete." />}</Page>;
}

function WorkspacesPage() {
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: () => api.workspaces() });
  const queryClient = useQueryClient();
  const prepare = useMutation({ mutationFn: () => api.prepareTemplate(), retry: false, onSuccess: () => queryClient.invalidateQueries({ queryKey: ["workspaces"] }) });
  const purge = useMutation({ mutationFn: (id: string) => api.purgeWorkspace(id), retry: false, onSuccess: () => queryClient.invalidateQueries({ queryKey: ["workspaces"] }) });
  if (workspaces.isPending) return <Page title="Workspaces"><Loading label="Loading workspace ownership…" /></Page>;
  if (workspaces.isError) return <Page title="Workspaces"><Failure error={workspaces.error} headline="Workspace records are unavailable." /></Page>;
  return <Page title="Workspaces" description="Monitoring and narrow safe actions for controller-owned workspace state."><SimpleGrid cols={{ base: 1, md: 2 }}><Card withBorder><Title order={3}>Template</Title><Text>{workspaces.data.template.length ? "Verified template records are available." : "No valid template is available."}</Text><Button mt="sm" loading={prepare.isPending} disabled={Boolean(workspaces.data.template.length)} onClick={() => prepare.mutate()}>Prepare template</Button></Card><Card withBorder><Title order={3}>Active attempts</Title>{workspaces.data.active_attempts.length ? workspaces.data.active_attempts.map((item) => <Text key={item.workspace_id}>{item.workspace_id} · Run {item.run_id ?? "—"}</Text>) : <Text>No active attempts.</Text>}</Card><Card withBorder><Title order={3}>Quarantine</Title>{workspaces.data.quarantine.length ? workspaces.data.quarantine.map((item) => <Group key={item.workspace_id} justify="space-between"><Text>{item.workspace_id} · Run {item.run_id ?? "—"}</Text><Button size="xs" variant="light" onClick={() => purge.mutate(item.workspace_id)}>Purge</Button></Group>) : <Text>No quarantined attempts.</Text>}</Card><Card withBorder><Title order={3}>Recent purges</Title>{workspaces.data.recent_purges.length ? workspaces.data.recent_purges.map((item) => <Text key={item.workspace_id}>{item.workspace_id}: {item.state}</Text>) : <Text>No retained workspaces were purged.</Text>}</Card></SimpleGrid>{purge.isError && <Failure error={purge.error} headline="Purge incomplete." />}</Page>;
}

export function App() {
  return <Shell><Routes><Route path="/e2e/catalog" element={<CatalogPage />} /><Route path="/e2e/runs" element={<RunsPage />} /><Route path="/e2e/runs/:runId" element={<RunPage />} /><Route path="/e2e/workspaces" element={<WorkspacesPage />} /><Route path="*" element={<Navigate to="/e2e/catalog" replace />} /></Routes></Shell>;
}
