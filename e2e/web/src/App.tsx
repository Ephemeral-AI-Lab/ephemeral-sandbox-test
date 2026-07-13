import {
  AppShell,
  Button,
  Card,
  Checkbox,
  Divider,
  Drawer,
  Group,
  Loader,
  Modal,
  Paper,
  Progress,
  SimpleGrid,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useDisclosure, useMediaQuery } from "@mantine/hooks";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, NavLink, Navigate, Route, Routes, useLocation, useNavigate, useParams, useSearchParams } from "react-router";
import { createContext, type FormEvent, type ReactNode, useContext, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "./api";
import { RuntimeResources } from "./RuntimeResources";
import { AsyncStateNotice } from "./state-copy";
import type { CatalogCase, CatalogPage, EvidenceRecord, Health, Json, Preview, RunProjection, RunsPage } from "./types";

const catalogKey = (search: URLSearchParams) => ["catalog", search.toString()] as const;
const terminalStates = new Set(["passed", "failed", "error", "cancelled"]);
type ControllerConnection = "connecting" | "live" | "reconnecting" | "stale" | "disconnected";
type PreviewSelectionClause = { case: { test_id: string; case_id: string } } | { query: Record<string, string | string[]> };
const ControllerConnectionContext = createContext<ControllerConnection>("connecting");

type IconName = "activity" | "alert" | "archive" | "catalog" | "check" | "chevron" | "clock" | "database" | "filter" | "folder" | "health" | "history" | "info" | "refresh" | "run" | "search" | "shield" | "terminal" | "x";

function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  let content: ReactNode;
  switch (name) {
    case "activity": content = <><path d="M3 12h4l2.2-6 4.1 12L16 12h5" /></>; break;
    case "alert": content = <><path d="M12 3 2.8 20h18.4L12 3Z" /><path d="M12 9v4.5" /><path d="M12 17h.01" /></>; break;
    case "archive": content = <><rect x="3" y="5" width="18" height="15" rx="2" /><path d="M3 9h18M9 13h6" /></>; break;
    case "catalog": content = <><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M8 7h8M8 11h8M8 15h5" /></>; break;
    case "check": content = <path d="m5 12 4 4L19 6" />; break;
    case "chevron": content = <path d="m9 18 6-6-6-6" />; break;
    case "clock": content = <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>; break;
    case "database": content = <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7" /></>; break;
    case "filter": content = <path d="M3 5h18l-7 8v6l-4 2v-8L3 5Z" />; break;
    case "folder": content = <path d="M3 6.5h7l2 2h9v10.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6.5Z" />; break;
    case "health": content = <><path d="M12 21s-8-4.5-8-11a4.5 4.5 0 0 1 8-2.8A4.5 4.5 0 0 1 20 10c0 6.5-8 11-8 11Z" /><path d="M8 12h2l1-2 2 5 1-3h2" /></>; break;
    case "history": content = <><path d="M4 5v5h5" /><path d="M5.5 17.5A9 9 0 1 0 4 10" /><path d="M12 7v5l3 2" /></>; break;
    case "info": content = <><circle cx="12" cy="12" r="9" /><path d="M12 11v6M12 7h.01" /></>; break;
    case "refresh": content = <><path d="M20 7v5h-5" /><path d="M4 17v-5h5" /><path d="M6.1 8A7 7 0 0 1 18.5 6.5L20 12M4 12l1.5 5.5A7 7 0 0 0 17.9 16" /></>; break;
    case "run": content = <><circle cx="12" cy="12" r="9" /><path d="m10 8 6 4-6 4V8Z" /></>; break;
    case "search": content = <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></>; break;
    case "shield": content = <><path d="M12 3 4.5 6v5.5c0 4.7 3 8 7.5 9.5 4.5-1.5 7.5-4.8 7.5-9.5V6L12 3Z" /><path d="m9 12 2 2 4-5" /></>; break;
    case "terminal": content = <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="m7 9 3 3-3 3M13 15h4" /></>; break;
    case "x": content = <path d="m6 6 12 12M18 6 6 18" />; break;
    default: content = <path d="M4 12h16" />;
  }
  return <svg className="line-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{content}</svg>;
}

function humanize(value: string | undefined): string {
  return (value || "Unknown").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function statusIcon(state: string): IconName {
  if (["passed", "ready", "complete", "retained", "live"].includes(state)) return "check";
  if (["failed", "error", "blocked", "degraded", "unavailable", "purged", "invalid", "partial", "unsupported"].includes(state)) return "alert";
  if (["running", "checking", "preparing", "recovering", "cancelling", "reconnecting"].includes(state)) return "activity";
  if (["queued", "pending", "not_run", "skipped", "stale", "cancelled"].includes(state)) return "clock";
  return "info";
}

function Status({ value, inverse = false }: { value: string | undefined; inverse?: boolean }) {
  const state = value || "unknown";
  return <span className={`status status-${state}${inverse ? " status-inverse" : ""}`}><Icon name={statusIcon(state)} size={14} /><span>{humanize(state)}</span></span>;
}

function Identity({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={`identity ${className}`.trim()}>{children}</span>;
}

function FactGap({ children }: { children: ReactNode }) {
  return <Text className="fact-gap" size="sm"><Icon name="info" size={16} /> <span>{children}</span></Text>;
}

function formatTime(value: string | undefined): string {
  if (!value) return "Not published";
  return value.replace("T", " · ").replace(/(\d\d:\d\d):\d\d(?:\.\d+)?Z$/, "$1 UTC");
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "Invalid byte count";
  if (value < 1024) return `${value} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let size = value;
  let unit = -1;
  do { size /= 1024; unit += 1; } while (size >= 1024 && unit < units.length - 1);
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(size)} ${units[unit]}`;
}

function jsonFact(value: Json): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function useControllerEvents() {
  const location = useLocation();
  const queryClient = useQueryClient();
  const source = useRef<EventSource | null>(null);
  const recovery = useRef({ path: "", pending: false });
  const [connection, setConnection] = useState<ControllerConnection>("connecting");
  const [streamVersion, setStreamVersion] = useState(0);

  useEffect(() => {
    const runId = /^\/e2e\/runs\/([^/]+)$/.exec(location.pathname)?.[1];
    if (recovery.current.path !== location.pathname) recovery.current = { path: location.pathname, pending: false };
    const after = runId ? Number(queryClient.getQueryData<RunProjection>(["run", runId])?.applied_through_seq ?? 0) : 0;
    const path = runId ? `/api/v1/events?run_id=${encodeURIComponent(runId)}&after=${after}` : "/api/v1/events";
    source.current?.close();
    const stream = new EventSource(path);
    source.current = stream;
    let disposed = false;
    let heartbeatReceived = false;
    let staleTimer: ReturnType<typeof setTimeout> | undefined;
    let disconnectedTimer: ReturnType<typeof setTimeout> | undefined;
    const armDisconnectedTimer = () => {
      if (disconnectedTimer) clearTimeout(disconnectedTimer);
      disconnectedTimer = setTimeout(() => setConnection("disconnected"), 5_000);
    };
    const confirmFreshStream = () => {
      if (disconnectedTimer) clearTimeout(disconnectedTimer);
      setConnection("live");
    };
    const armStaleTimer = () => {
      if (staleTimer) clearTimeout(staleTimer);
      staleTimer = setTimeout(() => {
        setConnection("stale");
        if (runId) void queryClient.refetchQueries({ queryKey: ["run", runId], type: "active" });
      }, 15_000);
    };
    const refreshSnapshot = () => {
      if (recovery.current.pending) return;
      recovery.current.pending = true;
      heartbeatReceived = false;
      setConnection("reconnecting");
      armDisconnectedTimer();
      if (runId) void queryClient.refetchQueries({ queryKey: ["run", runId], type: "active" });
      void queryClient.invalidateQueries({ queryKey: ["health"] });
    };
    const restartAfterSnapshot = () => {
      if (!runId) { refreshSnapshot(); return; }
      if (recovery.current.pending) return;
      recovery.current.pending = true;
      heartbeatReceived = false;
      setConnection("reconnecting");
      armDisconnectedTimer();
      if (staleTimer) clearTimeout(staleTimer);
      stream.close();
      void queryClient.invalidateQueries({ queryKey: ["health"] });
      void api.run(runId).then((snapshot) => {
        if (disposed) return;
        queryClient.setQueryData(["run", runId], snapshot);
        const nextAfter = Number(snapshot.applied_through_seq ?? 0);
        if (Number.isFinite(nextAfter) && nextAfter > after) setStreamVersion((value) => value + 1);
        else setConnection("disconnected");
      }).catch(() => {
        if (!disposed) setConnection("disconnected");
      });
    };
    setConnection("connecting");
    armDisconnectedTimer();
    stream.onopen = () => { heartbeatReceived = false; if (!recovery.current.pending) setConnection("connecting"); armDisconnectedTimer(); };
    stream.onerror = () => {
      if (heartbeatReceived) { heartbeatReceived = false; return; }
      refreshSnapshot();
    };
    stream.onmessage = (event) => {
      recovery.current.pending = false;
      confirmFreshStream();
      armStaleTimer();
      if (runId) void queryClient.invalidateQueries({ queryKey: ["run", runId] });
      if (event.lastEventId) void queryClient.invalidateQueries({ queryKey: ["runs"] });
      void queryClient.invalidateQueries({ queryKey: ["health"] });
    };
    stream.addEventListener("catalog.revision", () => {
      void queryClient.refetchQueries({ queryKey: ["catalog"], type: "active" });
      void queryClient.invalidateQueries({ queryKey: ["health"] });
    });
    stream.addEventListener("stream.heartbeat", () => {
      recovery.current.pending = false;
      heartbeatReceived = true;
      confirmFreshStream();
      armStaleTimer();
      void queryClient.invalidateQueries({ queryKey: ["health"] });
    });
    stream.addEventListener("stream.gap", restartAfterSnapshot);
    return () => { disposed = true; if (staleTimer) clearTimeout(staleTimer); if (disconnectedTimer) clearTimeout(disconnectedTimer); stream.close(); };
  }, [location.pathname, queryClient, streamVersion]);
  return connection;
}

const navItems: Array<{ label: string; to: string; icon: IconName }> = [
  { label: "Catalog", to: "/e2e/catalog", icon: "catalog" },
  { label: "Runs", to: "/e2e/runs", icon: "history" },
  { label: "Workspaces", to: "/e2e/workspaces", icon: "folder" },
];

function PrimaryNav({ compact = false }: { compact?: boolean }) {
  const location = useLocation();
  const navigation = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!compact || !navigation.current) return;
    const current = navigation.current.querySelector<HTMLElement>('[aria-current="page"]');
    if (!current) return;
    navigation.current.scrollLeft = Math.max(0, current.offsetLeft - (navigation.current.clientWidth - current.clientWidth) / 2);
  }, [compact, location.pathname]);
  return <nav ref={navigation} aria-label="Primary" className={compact ? "primary-nav primary-nav-horizontal" : "primary-nav"}>
    {navItems.map((item) => <Button key={item.label} component={NavLink} to={item.to} end variant="subtle" className="nav-link" leftSection={<Icon name={item.icon} />}><span>{item.label}</span></Button>)}
  </nav>;
}

function routeContext(pathname: string): string {
  if (/^\/e2e\/runs\/[^/]+$/.test(pathname)) return "Run evidence";
  if (pathname === "/e2e/runs") return "Run history";
  if (pathname === "/e2e/workspaces") return "Workspace safety";
  return "Test catalog";
}

function Shell({ children }: { children: ReactNode }) {
  const [healthOpened, healthControls] = useDisclosure(false);
  const compactNavigation = useMediaQuery("(max-width: 900px)") ?? false;
  const health = useQuery({ queryKey: ["health"], queryFn: () => api.health() });
  const location = useLocation();
  const connection = useControllerEvents();
  const catalogSearch = new URLSearchParams(location.pathname === "/e2e/catalog" ? location.search : "");
  const shellCatalog = useQuery({ queryKey: catalogKey(catalogSearch), queryFn: () => api.catalog(catalogSearch) });
  const activeRunId = health.data?.lane.active_run_id;
  const domains = Object.entries(shellCatalog.data?.facets.domain_id ?? {}).sort(([left], [right]) => left.localeCompare(right));
  const healthState = health.isPending ? "checking" : health.isError ? "unavailable" : String(health.data.catalog.state ?? "unknown");

  return <ControllerConnectionContext.Provider value={connection}><AppShell
    header={{ height: { base: 112, md: 58 } }}
    navbar={{ width: { base: 210, xl: 236 }, breakpoint: 900, collapsed: { mobile: true } }}
    padding={0}
  >
    <AppShell.Navbar className="control-sidebar">
      {!compactNavigation && <><div className="brand-block"><span className="brand-mark"><Icon name="activity" size={22} /></span><div><Text fw={750}>EphemeralOS</Text><Text size="xs">E2E Control Room</Text></div></div>
        <PrimaryNav />
        <Divider />
        <section className="sidebar-domains" aria-label="Catalog domains">
          <Text className="sidebar-label">Catalog domains</Text>
          {shellCatalog.isPending && <Text size="xs">Loading counts…</Text>}
          {domains.map(([domain, count]) => <Link className="domain-count" key={domain} to={`/e2e/catalog?domain_id=${encodeURIComponent(domain)}`} aria-label={`${humanize(domain)} · ${count} cases`} aria-current={location.pathname === "/e2e/catalog" && catalogSearch.get("domain_id") === domain ? "page" : undefined}><span className="domain-glyph" aria-hidden="true" /><span>{humanize(domain)}</span><strong>{count}</strong></Link>)}
          {!shellCatalog.isPending && !domains.length && <Text size="xs">Counts load with Catalog.</Text>}
        </section>
        <div className="sidebar-runner">
          <Group gap={8} wrap="nowrap"><Status value={healthState} /><Text size="xs">Runner</Text></Group>
          <Text size="xs" mt={8}>{activeRunId ? <>Lane owned by <Identity>{activeRunId}</Identity></> : "Serial lane available"}</Text>
        </div></>}
    </AppShell.Navbar>

    <AppShell.Header className="control-header">
      {compactNavigation && <div className="mobile-primary"><div className="mobile-brand"><span className="brand-mark"><Icon name="activity" size={18} /></span><span>EphemeralOS</span></div><PrimaryNav compact /></div>}
      <div className="context-row">
        <div className="context-copy"><Text size="xs">Control room</Text><strong>{routeContext(location.pathname)}</strong></div>
        <Group gap="sm" wrap="nowrap"><span className={`connection connection-${connection}`}><Icon name={connection === "live" ? "activity" : "refresh"} size={14} />{humanize(connection)}</span><Button onClick={healthControls.open} variant="subtle" className="health-button" aria-label="Open runner health" leftSection={<Icon name="health" />}>Health</Button></Group>
      </div>
    </AppShell.Header>

    <nav aria-label="Skip links"><a className="skip-link" href="#main-content">Skip to main content</a></nav>
    <AppShell.Main><main id="main-content" tabIndex={-1} className="route-main">{connection === "reconnecting" && <AsyncStateNotice state="reconnecting" />}{connection === "stale" && <AsyncStateNotice state="stream_stale" detail="No controller heartbeat arrived within 15 seconds; displayed data remains the last verified projection." />}{connection === "disconnected" && <AsyncStateNotice state="disconnected" detail="The live event stream did not recover; displayed data remains the last verified controller projection." />}{children}</main></AppShell.Main>
    <Drawer opened={healthOpened} onClose={healthControls.close} title="Runner health" position="right" size="min(100%, 560px)" classNames={{ root: "health-drawer", header: "sheet-header", body: "sheet-body" }}>
      {health.isPending ? <Loading label="Loading runner health…" /> : health.isError ? <Failure error={health.error} headline="Runner health is unavailable." /> : <HealthPanel health={health.data} onNavigate={healthControls.close} />}
    </Drawer>
  </AppShell></ControllerConnectionContext.Provider>;
}

function HealthPanel({ health, onNavigate }: { health: Health; onNavigate: () => void }) {
  const catalogState = String(health.catalog.state ?? "unknown");
  return <Stack gap="md" className="health-panel">
    <HealthGroup icon="catalog" title="Can I browse?" state={catalogState}><Text size="sm">Current revision</Text><Identity>{String(health.catalog.current_revision ?? "Not published")}</Identity></HealthGroup>
    <HealthGroup icon="run" title="Can I start?" state={health.lane.active_run_id ? "blocked" : "ready"}><Text size="sm">{health.lane.active_run_id ? <>Run <Identity>{health.lane.active_run_id}</Identity> owns the serial execution lane.</> : "No run owns the serial execution lane."}</Text></HealthGroup>
    <HealthGroup icon="shield" title="Which boundaries work?"><FactGap>Boundary readiness is not published by this controller.</FactGap></HealthGroup>
    <HealthGroup icon="database" title="Is storage safe?"><Stack gap={7}>{Object.entries(health.roots).map(([name, value]) => <div key={name}><Text size="xs" c="dimmed">{humanize(name)}</Text><Identity>{value}</Identity></div>)}</Stack><FactGap>Owned roots are published; capacity and store-safety state are not.</FactGap></HealthGroup>
    <HealthGroup icon="alert" title="What needs attention?" state={catalogState === "ready" && !health.lane.active_run_id ? "ready" : catalogState}><Text size="sm">{catalogState === "ready" ? "No catalog diagnostic is currently projected." : "Review the catalog state before admission."}</Text></HealthGroup>
    <Button component={Link} to="/e2e/catalog?kind=harness&runnable=true" onClick={onNavigate} variant="light" leftSection={<Icon name="shield" />}>Review runnable harness diagnostics</Button>
  </Stack>;
}

function HealthGroup({ icon, title, state, children }: { icon: IconName; title: string; state?: string; children: ReactNode }) {
  return <Card className="health-group"><Group justify="space-between" align="flex-start" wrap="nowrap"><Group gap="sm" wrap="nowrap"><span className="feature-icon"><Icon name={icon} /></span><Title order={3}>{title}</Title></Group>{state && <Status value={state} />}</Group><div className="health-content">{children}</div></Card>;
}

function Loading({ label, detail }: { label: string; detail?: string }) {
  return <Stack align="center" py="xl" aria-live="polite" className="loading-state"><Loader color="slate" /><Text fw={650}>{label}</Text>{detail && <Text size="sm" c="dimmed">{detail}</Text>}</Stack>;
}

function Failure({ error, headline }: { error: unknown; headline: string }) {
  const detail = error instanceof ApiError ? `${error.response.message} Request ${error.response.request_id}.` : "The controller could not be read safely.";
  return <Paper className="failure" role="status" p="md"><Group gap="sm" align="flex-start" wrap="nowrap"><Icon name="alert" /><div><Title order={2} size="h4">{headline}</Title><Text>{detail}</Text></div></Group></Paper>;
}

function Page({ eyebrow, title, description, actions, children, className = "" }: { eyebrow?: string; title: string; description?: string; actions?: ReactNode; children: ReactNode; className?: string }) {
  const location = useLocation();
  const heading = useRef<HTMLElement | null>(null);
  useEffect(() => { heading.current?.focus(); }, [location.pathname, title]);
  return <Stack className={`page ${className}`.trim()} gap="xl"><header ref={heading} tabIndex={-1} className="page-heading"><div className="heading-copy">{eyebrow && <Text className="eyebrow">{eyebrow}</Text>}<Title order={1}>{title}</Title>{description && <Text className="page-purpose">{description}</Text>}</div>{actions && <div className="page-actions">{actions}</div>}</header>{children}</Stack>;
}

function CatalogPage() {
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [searchInput, setSearchInput] = useState(params.get("q") ?? "");
  const [selected, setSelected] = useState<Map<string, CatalogCase>>(new Map());
  const [selectionRevision, setSelectionRevision] = useState<string | null>(null);
  const [reviewSelection, setReviewSelection] = useState<PreviewSelectionClause[]>([]);
  const [reviewRevision, setReviewRevision] = useState<string | null>(null);
  const [detail, setDetail] = useState<CatalogCase | null>(null);
  const [reviewOpened, reviewControls] = useDisclosure(false);
  const [filtersOpened, filterControls] = useDisclosure(false);
  const [detailOpened, detailControls] = useDisclosure(false);
  const detailIsSheet = useMediaQuery("(max-width: 1180px)") ?? false;
  const detailIsFullscreen = useMediaQuery("(max-width: 900px)") ?? false;
  const search = new URLSearchParams(params);
  const catalog = useQuery({ queryKey: catalogKey(search), queryFn: () => api.catalog(search) });
  const fullCatalog = useQuery({ queryKey: catalogKey(new URLSearchParams()), queryFn: () => api.catalog(new URLSearchParams()) });
  const refresh = useMutation({
    mutationFn: () => api.refreshCatalog(),
    retry: false,
    onSuccess: () => {
      void catalog.refetch();
      void fullCatalog.refetch();
      void queryClient.invalidateQueries({ queryKey: ["health"] });
    },
  });

  const selectionStale = Boolean(catalog.data && selectionRevision && selectionRevision !== catalog.data.catalog_revision);
  useEffect(() => { setSearchInput(params.get("q") ?? ""); }, [params]);
  useEffect(() => { if (selectionStale && reviewOpened) reviewControls.close(); }, [reviewControls, reviewOpened, selectionStale]);
  useEffect(() => { setDetail(catalog.data?.items[0] ?? null); }, [catalog.data?.items]);

  function setFilter(name: string, value?: string) {
    const next = new URLSearchParams(params);
    value ? next.set(name, value) : next.delete(name);
    if (name !== "cursor") next.delete("cursor");
    setParams(next);
  }
  function submitSearch(event: FormEvent) { event.preventDefault(); setFilter("q", searchInput.trim() || undefined); }
  function toggleCase(item: CatalogCase) {
    const identity = `${item.test_id}:${item.case_id}`;
    if (selectionStale) {
      setSelected(new Map([[identity, item]]));
      setSelectionRevision(catalog.data?.catalog_revision ?? null);
      return;
    }
    setSelected((prior) => { const next = new Map(prior); next.has(identity) ? next.delete(identity) : next.set(identity, item); return next; });
    setSelectionRevision(catalog.data?.catalog_revision ?? null);
  }
  function clearSelection() { setSelected(new Map()); setSelectionRevision(null); setReviewSelection([]); setReviewRevision(null); reviewControls.close(); }
  function reviewSelected() {
    setReviewSelection([...selected.values()].map((item) => ({ case: { test_id: item.test_id, case_id: item.case_id } })));
    setReviewRevision(catalog.data?.catalog_revision ?? null);
    reviewControls.open();
  }
  function reviewQuery(query: Record<string, string | string[]>, revision: string) {
    setSelected(new Map());
    setSelectionRevision(null);
    setReviewSelection([{ query }]);
    setReviewRevision(revision);
    reviewControls.open();
  }
  function reviewFacet(field: string, value: string) {
    const query = Object.fromEntries([...params.entries()].filter(([name]) => name !== "cursor" && name !== "limit"));
    query[field] = value;
    reviewQuery(query, catalog.data?.catalog_revision ?? "");
  }
  function openDetail(item: CatalogCase) { setDetail(item); if (detailIsSheet) detailControls.open(); }

  if (catalog.isPending) return <Page eyebrow="Catalog" title="Know what the system can prove"><Loading label="Loading test catalog…" detail="Reading the current published revision." /></Page>;
  if (catalog.isError) return <Page eyebrow="Catalog" title="Know what the system can prove"><Failure error={catalog.error} headline="Test catalog is unavailable." /><Button onClick={() => catalog.refetch()} leftSection={<Icon name="refresh" />}>Refresh catalog</Button></Page>;
  const data = catalog.data;
  const hasFilters = [...params.keys()].some((key) => key !== "cursor");
  const fullData = fullCatalog.data ?? (!hasFilters ? data : null);
  const fullCatalogKinds = Object.keys(fullData?.facets.kind ?? {});

  return <Page eyebrow="Test catalog" title="Know what the system can prove" description="Find a behavior, inspect its declared boundary and evidence contract, then freeze an exact revision-qualified scope for review." className="catalog-page">
    <section className="catalog-command" aria-label="Catalog search and actions">
      <form onSubmit={submitSearch} className="search-form"><TextInput aria-label="Search catalog" placeholder="Search behavior, purpose, feature, validation, owner, or ID" value={searchInput} onChange={(event) => setSearchInput(event.currentTarget.value)} leftSection={<Icon name="search" />} /><Button type="submit">Search</Button></form>
      <div className="catalog-actions"><Button loading={!fullData} disabled={!fullData || !fullCatalogKinds.length || refresh.isError} onClick={() => fullData && reviewQuery({ kind: fullCatalogKinds }, fullData.catalog_revision)} leftSection={<Icon name="run" />}>{fullData ? `Run all ${fullData.total} ${fullData.total === 1 ? "case" : "cases"}` : "Loading full catalog"}</Button><Button variant="light" loading={refresh.isPending} onClick={() => refresh.mutate()} leftSection={<Icon name="refresh" />}>{refresh.isPending ? "Updating catalog" : "Refresh catalog"}</Button></div>
    </section>
    {refresh.isPending && <Text role="status" size="sm">Recollecting now; revision <Identity>{data.catalog_revision}</Identity> remains the last verified catalog.</Text>}
    {refresh.isError && <AsyncStateNotice state="catalog_invalid_last_good" detail="The controller rejected the update. Admission is blocked until a valid refresh succeeds." />}
    {selectionStale && <Paper className="failure stale-selection" p="sm"><div><Text fw={750}>Review is out of date.</Text><Text size="sm">The selected cases remain pinned to <Identity>{selectionRevision}</Identity>; the published catalog is now <Identity>{data.catalog_revision}</Identity>. Clear and reselect before review.</Text></div><Button variant="default" onClick={clearSelection}>Clear selection</Button></Paper>}
    {!hasFilters && <CatalogOverview data={data} onFilter={setFilter} />}
    <section className="catalog-browser" aria-labelledby="catalog-browser-title">
      <div className="section-heading"><div><Text className="eyebrow">Browse declarations</Text><Title order={2} id="catalog-browser-title">Inspect exact cases</Title></div><Button className="filter-trigger" variant="light" onClick={filterControls.open} leftSection={<Icon name="filter" />}>Filters</Button></div>
      <AppliedFilters params={params} onClear={setFilter} />
      <div className="catalog-workspace">
        <div className="catalog-rail-desktop"><CatalogFilterRail data={data} params={params} onFilter={setFilter} onRun={reviewFacet} runDisabled={refresh.isError} onClearAll={() => setParams(new URLSearchParams())} /></div>
        {data.items.length === 0 ? <EmptyCatalog onClear={() => setParams(new URLSearchParams())} /> : <CatalogResults data={data} selected={selected} detail={detail} onToggle={toggleCase} onDetail={openDetail} onPage={(cursor) => setFilter("cursor", cursor ?? undefined)} />}
        {!detailIsSheet && <CatalogDetail item={detail} />}
      </div>
    </section>
    <SelectionTray selected={selected} revision={selectionRevision ?? data.catalog_revision} onReview={reviewSelected} disabledReason={selectionStale ? "Clear and reselect against the current catalog revision." : refresh.isError ? "Admission is blocked until catalog refresh succeeds." : undefined} />
    <ReviewDialog opened={reviewOpened} onClose={reviewControls.close} revision={reviewRevision ?? data.catalog_revision} selection={reviewSelection} />
    <Drawer opened={filtersOpened} onClose={filterControls.close} title="Filter catalog" position="left" size="100%" classNames={{ root: "catalog-filter-sheet", header: "sheet-header", body: "sheet-body" }}><CatalogFilterRail data={data} params={params} onFilter={(name, value) => { setFilter(name, value); filterControls.close(); }} onRun={(field, value) => { reviewFacet(field, value); filterControls.close(); }} runDisabled={refresh.isError} onClearAll={() => { setParams(new URLSearchParams()); filterControls.close(); }} /></Drawer>
    <Drawer opened={detailOpened} onClose={detailControls.close} title="Case detail" position="right" size={detailIsFullscreen ? "100%" : "min(100%, 560px)"} classNames={{ root: "catalog-detail-sheet", header: "sheet-header", body: "sheet-body" }}><CatalogDetail item={detail} embedded /></Drawer>
  </Page>;
}

function CatalogOverview({ data, onFilter }: { data: CatalogPage; onFilter: (name: string, value?: string) => void }) {
  const domains = Object.entries(data.facets.domain_id ?? {}).sort(([left], [right]) => left.localeCompare(right));
  const primary = domains.filter(([domain]) => domain !== "harness");
  const harnessCount = data.facets.kind?.harness ?? data.facets.domain_id?.harness ?? 0;
  const runnableOnPage = data.items.filter((item) => item.runnable).length;
  const validationsOnPage = data.items.reduce((sum, item) => sum + (item.validations?.length ?? 0), 0);
  return <Stack gap="lg" className="catalog-overview">
    <SimpleGrid cols={{ base: 1, xs: 2, lg: 4 }} className="metric-grid">
      <Metric label="Matching cases" value={data.total} note="Server-counted declarations" />
      <Metric label="Non-harness domains" value={primary.length} note="Dynamic catalog facets" />
      <Metric label="Runnable on page" value={runnableOnPage} note={`Of ${data.items.length} visible cases`} />
      <Metric label="Validations on page" value={validationsOnPage} note="Declared, not latest results" />
    </SimpleGrid>
    {harnessCount > 0 && <Paper className="catalog-warning" role="note"><Icon name="alert" /><div><Text fw={700}>Harness diagnostics are not product coverage.</Text><Text size="sm">{harnessCount} diagnostic {harnessCount === 1 ? "case remains" : "cases remain"} separately queryable and make no product-boundary claim.</Text></div><Button variant="subtle" aria-label={`Harness Diagnostics · ${harnessCount} cases`} onClick={() => onFilter("kind", "harness")}>Review diagnostics</Button></Paper>}
    <section aria-labelledby="domain-heading"><div className="section-heading"><div><Text className="eyebrow">Coverage topology</Text><Title order={2} id="domain-heading">Coverage domains</Title></div></div><div className="domain-grid">{primary.map(([domain, count]) => {
      const familyCount = new Set(data.items.filter((item) => item.domain_id === domain).map((item) => item.family_id).filter(Boolean)).size;
      return <button key={domain} type="button" className="domain-card" onClick={() => onFilter("domain_id", domain)} aria-label={`${humanize(domain)} · ${count} cases`}><span className="domain-card-icon"><Icon name="catalog" size={21} /></span><span><strong>{humanize(domain)}</strong><small>{count} {count === 1 ? "case" : "cases"} · {familyCount} visible {familyCount === 1 ? "family" : "families"}</small></span><Icon name="chevron" /></button>;
    })}</div></section>
  </Stack>;
}

function Metric({ label, value, note }: { label: string; value: number | string; note: string }) {
  return <Card className="metric-card"><Text className="metric-label">{label}</Text><Text className="metric-value">{value}</Text><Text size="xs">{note}</Text></Card>;
}

function AppliedFilters({ params, onClear }: { params: URLSearchParams; onClear: (name: string, value?: string) => void }) {
  const active = [...params.keys()].filter((name) => name !== "cursor" && params.get(name));
  if (!active.length) return null;
  return <div role="group" aria-label="Applied filters" className="filter-chips">{active.map((name) => <Button key={name} variant="light" size="compact-md" onClick={() => onClear(name)} rightSection={<Icon name="x" size={14} />}>{humanize(name)}: {params.get(name)}</Button>)}</div>;
}

function CatalogFilterRail({ data, params, onFilter, onRun, runDisabled, onClearAll }: { data: CatalogPage; params: URLSearchParams; onFilter: (name: string, value?: string) => void; onRun: (name: string, value: string) => void; runDisabled: boolean; onClearAll: () => void }) {
  const groups: Array<[string, Record<string, number>]> = [
    ["domain_id", data.facets.domain_id ?? {}],
    ["family_id", data.facets.family_id ?? {}],
    ["kind", data.facets.kind ?? {}],
  ];
  return <aside className="filter-rail" aria-label="Catalog filters"><Group justify="space-between"><Text fw={750}>Filter catalog</Text>{[...params.keys()].length > 0 && <Button variant="subtle" size="compact-sm" onClick={onClearAll}>Clear</Button>}</Group>{groups.map(([field, values]) => <section key={field} className="filter-group"><Text className="filter-label">{humanize(field)}</Text><Stack gap={2}>{Object.entries(values).sort(([left], [right]) => left.localeCompare(right)).map(([value, count]) => { const active = params.get(field) === value; const label = humanize(value); const groupLabel = field === "domain_id" ? "domain" : field === "family_id" ? "family" : "kind"; return <div className="facet-row" key={value}><Button variant={active ? "filled" : "subtle"} className="facet-button" aria-label={`${label} (${count})`} aria-pressed={active} onClick={() => onFilter(field, active ? undefined : value)}><span>{label}</span><strong>{count}</strong></Button><Button variant="light" className="facet-run" disabled={runDisabled} aria-label={`Run all ${count} ${count === 1 ? "case" : "cases"} in ${label} ${groupLabel}`} title={`Run this ${groupLabel} filter`} onClick={() => onRun(field, value)}>Run</Button></div>; })}</Stack></section>)}</aside>;
}

function EmptyCatalog({ onClear }: { onClear: () => void }) {
  return <Paper className="empty-state"><span className="feature-icon"><Icon name="search" /></span><Title order={3}>No cases match these filters.</Title><Text>Clear a filter or change the search. The published catalog remains available.</Text><Button onClick={onClear}>Clear filters</Button></Paper>;
}

function CatalogResults({ data, selected, detail, onToggle, onDetail, onPage }: { data: CatalogPage; selected: Map<string, CatalogCase>; detail: CatalogCase | null; onToggle: (item: CatalogCase) => void; onDetail: (item: CatalogCase) => void; onPage: (cursor: string | null) => void }) {
  return <section aria-label="Catalog cases" className="catalog-results"><Group justify="space-between" align="end"><div><Text fw={750}>{data.total} matching cases</Text><Text size="xs">Published revision <Identity>{data.catalog_revision}</Identity></Text></div><Text size="xs">{data.items.length} on this page</Text></Group><Stack gap={8}>{data.items.map((item) => {
    const identity = `${item.test_id}:${item.case_id}`;
    const isActive = detail?.test_id === item.test_id && detail.case_id === item.case_id;
    return <Card key={identity} className={`case-row${isActive ? " active-card" : ""}`}>
      <div className="case-row-grid"><Checkbox aria-label={`Select ${item.title}`} checked={selected.has(identity)} onChange={() => onToggle(item)} /><button className="case-open" type="button" onClick={() => onDetail(item)} aria-current={isActive ? "true" : undefined}><Text fw={750}>{item.title}</Text><Text size="sm" className="case-purpose">{item.purpose ?? item.description ?? "No purpose was published."}</Text><Text size="xs" className="case-meta">{[item.domain_id, item.family_id, item.group_id].filter(Boolean).map((part) => humanize(String(part))).join(" · ")} · {item.validations?.length ?? 0} validations · {item.execution_surface ? `${humanize(item.execution_surface)} boundary` : "No product boundary claimed"}</Text></button><Status value={item.runnable ? "ready" : "blocked"} /><button className="row-chevron" type="button" onClick={() => onDetail(item)} aria-label={`Open details for ${item.title}`}><Icon name="chevron" /></button></div>
    </Card>;
  })}</Stack><Group justify="space-between" className="pagination-row"><Text size="sm">Page limit {data.page.limit}</Text><Button disabled={!data.page.next_cursor} onClick={() => onPage(data.page.next_cursor)}>Next page</Button></Group></section>;
}

function CatalogDetail({ item, embedded = false }: { item: CatalogCase | null; embedded?: boolean }) {
  if (!item) return <aside className="catalog-detail"><FactGap>Select a case to inspect its published declaration.</FactGap></aside>;
  return <aside aria-label="Case detail" className={`catalog-detail${embedded ? " catalog-detail-embedded" : ""}`}>
    <div className="detail-kicker"><Status value={item.runnable ? "ready" : "blocked"} /><Text size="xs">Catalog declaration</Text></div>
    <Title order={2}>{item.title}</Title>
    <Identity>{item.test_id} · {item.case_id}</Identity>
    <DetailSection title="Purpose"><Text>{item.purpose ?? item.description ?? "No purpose was published."}</Text></DetailSection>
    <DetailSection title="Validations"><Stack gap={6}>{item.validations?.length ? item.validations.map((validation) => <div className="validation-row" key={validation.id}><Status value="declared" /><div><Text fw={650}>{validation.id}</Text><Text size="xs">{validation.required === false ? "Optional" : "Required"}{validation.phase ? ` · ${humanize(validation.phase)}` : ""}</Text></div></div>) : <FactGap>No named validations were published.</FactGap>}</Stack></DetailSection>
    <DetailSection title="Product coverage">{item.kind === "harness" ? <Text>Harness diagnostic — no product boundary claimed.</Text> : (item.effective_features?.length ? <div className="plain-list">{item.effective_features.map((feature) => <Identity key={feature}>{feature}</Identity>)}</div> : <FactGap>No effective feature metadata was published.</FactGap>)}</DetailSection>
    {item.compound && <DetailSection title="Compound context"><CompoundContext item={item} /></DetailSection>}
    <DetailSection title="Execution boundary"><Text>{item.execution_surface ? humanize(item.execution_surface) : "No product execution surface is claimed."}</Text>{item.execution_label_ids?.length ? <Text size="sm">Labels: {item.execution_label_ids.map(humanize).join(", ")}</Text> : null}</DetailSection>
    <DetailSection title="Run policy"><Text size="sm">{item.runnable ? "Runnable in the current published catalog." : "Admission is blocked for this declaration."}</Text></DetailSection>
    <DetailSection title="History"><FactGap>Recent execution history is not published with catalog records.</FactGap></DetailSection>
    <DetailSection title="Source"><Stack gap={4}>{item.source && <Identity>{item.source}</Identity>}{item.pytest_nodeid && <Identity>{item.pytest_nodeid}</Identity>}{!item.source && !item.pytest_nodeid && <FactGap>Source identity was not published.</FactGap>}</Stack></DetailSection>
  </aside>;
}

function CompoundContext({ item, compact = false }: { item: CatalogCase; compact?: boolean }) {
  const compound = item.compound;
  if (!compound) return null;
  return <div className={`compound-context${compact ? " compound-context-compact" : ""}`}>
    <Text size="sm">Complexity: <Identity>{compound.complexity_id}</Identity></Text>
    <Text size="sm">Subject domains: {compound.subject_domain_ids.map(humanize).join(", ") || "None published"}</Text>
    <div className="compound-components">{compound.components.length ? compound.components.map((component) => <Text size="sm" key={`${component.id}:${component.role}`}><Identity>{component.id}</Identity> · {humanize(component.role)}</Text>) : <FactGap>No compound components were published.</FactGap>}</div>
    <Text size="sm">Workspace: {compound.shared_workspace ? "Shared" : "Not shared"}</Text>
    <Text size="sm">Teardown: <Identity>{compound.teardown_contract}</Identity></Text>
  </div>;
}

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return <section className="detail-section"><Text className="detail-label">{title}</Text>{children}</section>;
}

function SelectionTray({ selected, revision, onReview, disabledReason }: { selected: Map<string, CatalogCase>; revision: string; onReview: () => void; disabledReason?: string }) {
  const reason = disabledReason ?? (!selected.size ? "Select at least one case before reviewing." : undefined);
  return <Paper className="selection-tray"><div><Text fw={700}>{selected.size} selected {selected.size === 1 ? "case" : "cases"}</Text><Text size="xs">Exact catalog revision <Identity>{revision}</Identity></Text>{reason && <Text size="xs" className="selection-reason">{reason}</Text>}</div><Button onClick={onReview} disabled={Boolean(reason)} rightSection={<Icon name="chevron" />}>Review run</Button></Paper>;
}

function ReviewDialog({ opened, onClose, revision, selection }: { opened: boolean; onClose: () => void; revision: string; selection: PreviewSelectionClause[] }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const isMobile = useMediaQuery("(max-width: 680px)") ?? false;
  const admissionKey = useRef<{ previewId: string; value: string } | null>(null);
  const admissionHealthCheck = useRef(0);
  const [, setExpiryVersion] = useState(0);
  const [admissionResponseAt, setAdmissionResponseAt] = useState<number | null>(null);
  const [admissionHealth, setAdmissionHealth] = useState<{ checked: boolean; runId: string | null }>({ checked: false, runId: null });
  const selectionKey = JSON.stringify(selection);
  const preview = useMutation({ mutationFn: () => api.preview({ schema_version: 1, catalog_revision: revision, include: selection, exclude: [] }), retry: false });
  const admit = useMutation({
    mutationFn: ({ value, idempotencyKey }: { value: Preview; idempotencyKey: string }) => api.admit(value, idempotencyKey),
    retry: false,
    onError: (error) => {
      setAdmissionResponseAt(Date.now());
      const checksLane = error instanceof ApiError && (error.response.code === "active_run_conflict" || (error.status === 409 && error.response.code === "admission_blocked"));
      if (!checksLane) return;
      const check = ++admissionHealthCheck.current;
      setAdmissionHealth({ checked: false, runId: null });
      void api.health().then((freshHealth) => {
        if (check !== admissionHealthCheck.current) return;
        queryClient.setQueryData(["health"], freshHealth);
        setAdmissionHealth({ checked: true, runId: freshHealth.lane.active_run_id });
      }).catch(() => {
        if (check !== admissionHealthCheck.current) return;
        setAdmissionHealth({ checked: true, runId: null });
        void queryClient.invalidateQueries({ queryKey: ["health"] });
      });
    },
    onSuccess: (run) => {
      void queryClient.invalidateQueries({ queryKey: ["health"] });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      onClose();
      navigate(`/e2e/runs/${run.run_id}`);
    },
  });
  useEffect(() => {
    if (!opened) return;
    admissionHealthCheck.current += 1;
    admissionKey.current = null;
    setAdmissionResponseAt(null);
    setAdmissionHealth({ checked: false, runId: null });
    admit.reset();
    preview.reset();
    preview.mutate();
  }, [opened, revision, selectionKey]);
  const current = preview.data;
  const admissionBlockError = admit.error instanceof ApiError && (admit.error.response.code === "active_run_conflict" || (admit.error.status === 409 && admit.error.response.code === "admission_blocked")) ? admit.error : null;
  const conflictRunId = admissionBlockError ? admissionHealth.runId : null;
  const admissionConflict = Boolean(admissionBlockError && (admissionBlockError.response.code === "active_run_conflict" || conflictRunId));
  const admissionConflictError = admissionConflict ? admissionBlockError : null;
  const resolvingAdmissionBlock = Boolean(admissionBlockError?.response.code === "admission_blocked" && !admissionHealth.checked);
  const conflictRun = useQuery({
    queryKey: ["run", conflictRunId ?? "admission-conflict"],
    queryFn: () => api.run(conflictRunId ?? ""),
    enabled: Boolean(admissionConflict && conflictRunId),
    retry: false,
  });
  const expiresAt = current ? Date.parse(current.expires_at) : Number.NaN;
  const expiryInvalid = Boolean(current?.state === "ready" && !Number.isFinite(expiresAt));
  const expiredByClock = Boolean(current?.state === "ready" && Number.isFinite(expiresAt) && expiresAt <= Date.now());
  const refreshable = Boolean(current && (expiredByClock || expiryInvalid || current.state === "expired" || current.state === "stale"));
  const canStart = Boolean(current?.state === "ready" && current.admission_token && !expiredByClock && !expiryInvalid);
  const startAllowed = canStart && !admissionConflict && !resolvingAdmissionBlock;
  const blockedReason = expiryInvalid
    ? "The preview expiry could not be verified. Refresh the preview before starting."
    : expiredByClock
      ? "This preview expired before admission. Refresh it to verify readiness again."
      : current?.blockers[0]?.message ?? (current ? `Preview is ${humanize(current.state).toLowerCase()}.` : "Start is unavailable while readiness is checked.");
  useEffect(() => {
    if (!opened || current?.state !== "ready" || !Number.isFinite(expiresAt)) return;
    const remaining = expiresAt - Date.now();
    if (remaining <= 0) return;
    const timer = setTimeout(() => setExpiryVersion((value) => value + 1), Math.min(remaining + 10, 2_147_483_647));
    return () => clearTimeout(timer);
  }, [current?.expires_at, current?.preview_id, current?.state, expiresAt, opened]);
  const orderedCases = current?.ordered_cases ?? [];
  const boundaries = useMemo(() => {
    if (!current) return [];
    return [...new Set(current.ordered_cases.map((item) => item.execution_surface).filter((value): value is string => Boolean(value)))];
  }, [current]);
  const validations = orderedCases.reduce((sum, item) => sum + (item.validations?.filter((validation) => validation.required !== false).length ?? 0), 0);
  const evidencePolicies = Object.entries(current?.policies ?? {}).filter(([key]) => /evidence|cleanup|retention|purge/i.test(key));
  const previewHeadline = !current ? "Checking run readiness…" : expiredByClock ? "This review has expired." : expiryInvalid ? "Preview expiry is invalid." : current.state === "ready" ? `Ready to start ${current.case_count} exact cases.` : current.state === "stale" ? "Review is out of date." : current.state === "expired" ? "This review has expired." : current.state === "checking" ? "Checking run readiness…" : `Run blocked — ${current.blockers[0]?.reason_code ?? current.state}.`;

  function requestPreview() {
    admissionKey.current = null;
    setAdmissionResponseAt(null);
    admit.reset();
    preview.reset();
    preview.mutate();
  }

  function startRun() {
    if (!current || !startAllowed) return;
    setAdmissionResponseAt(null);
    if (admissionKey.current?.previewId !== current.preview_id) admissionKey.current = { previewId: current.preview_id, value: crypto.randomUUID() };
    admit.mutate({ value: current, idempotencyKey: admissionKey.current.value });
  }

  return <Modal opened={opened} onClose={onClose} title="Review run" fullScreen={isMobile} size="min(920px, calc(100vw - 48px))" classNames={{ root: "review-dialog", header: "review-header", body: "review-body" }}>
    <Stack gap="lg">{preview.isPending && <Loading label="Checking run readiness…" detail="Start is unavailable while readiness is verified." />}{preview.isError && <Failure error={preview.error} headline="Readiness check failed." />}{current && <><div className="review-summary"><Text className="eyebrow">Exact admission preview</Text><Title order={2}>{previewHeadline}</Title><Text>Created <Identity>{formatTime(current.created_at)}</Identity> · expires <Identity>{formatTime(current.expires_at)}</Identity>.</Text></div>
      <ReviewSection number="01" title="Scope"><Text fw={700}>{current.case_count} exact {current.case_count === 1 ? "case" : "cases"} · {validations} required validation {validations === 1 ? "contract" : "contracts"}</Text><Text size="sm">Catalog <Identity>{current.catalog_revision}</Identity>{current.parent_run_id ? <> · parent run <Identity>{current.parent_run_id}</Identity></> : null}</Text><Stack gap={4}>{orderedCases.map((item) => <div key={`${item.test_id}:${item.case_id}`} className="review-case"><span>{item.title}</span><Identity>{item.test_id}:{item.case_id}</Identity>{item.compound && <CompoundContext item={item} compact />}</div>)}</Stack></ReviewSection>
      <ReviewSection number="02" title="Boundaries">{boundaries.length ? <div className="plain-list">{boundaries.map((value) => <span key={value}>{humanize(value)}</span>)}</div> : <FactGap>No product execution boundary is claimed by this scope.</FactGap>}</ReviewSection>
      <ReviewSection number="03" title="Execution"><Text>{orderedCases.every((item) => item.runnable !== false) ? "Every selected declaration is runnable in this preview." : "The preview includes a declaration that is not runnable."}</Text><div className="input-facts">{Object.entries(current.policies).map(([key, value]) => <div key={key}><Text size="xs">Policy · {humanize(key)}</Text><Identity>{jsonFact(value)}</Identity></div>)}</div>{!Object.keys(current.policies).length && <FactGap>No execution policies were published.</FactGap>}</ReviewSection>
      <ReviewSection number="04" title="Evidence and cleanup"><Text>{validations} required validation {validations === 1 ? "contract" : "contracts"} in scope.</Text>{evidencePolicies.length ? <div className="input-facts">{evidencePolicies.map(([key, value]) => <div key={key}><Text size="xs">{humanize(key)}</Text><Identity>{jsonFact(value)}</Identity></div>)}</div> : <FactGap>No evidence, cleanup, retention, or purge policy key was published in this preview.</FactGap>}</ReviewSection>
      <ReviewSection number="05" title="Inputs"><div className="input-facts"><div><Text size="xs">Frozen source revision</Text><Identity>{current.source_revision}</Identity></div><div><Text size="xs">Workspace template</Text><Identity>{current.workspace_template}</Identity></div><div><Text size="xs">Estimated run storage</Text><Identity>{formatBytes(current.disk_estimate)} · {current.disk_estimate} bytes</Identity></div><div><Text size="xs">Controller bundle</Text><Identity>{current.controller_bundle_digest}</Identity></div><div><Text size="xs">Runner bundle</Text><Identity>{current.runner_bundle_digest}</Identity></div><div><Text size="xs">Preview identity</Text><Identity>{current.preview_id}</Identity></div><div><Text size="xs">Preview digest</Text><Identity>{current.preview_digest}</Identity></div>{Object.entries(current.product_builds).map(([name, value]) => <div key={name}><Text size="xs">Product build · {humanize(name)}</Text><Identity>{jsonFact(value)}</Identity></div>)}</div>{!Object.keys(current.product_builds).length && <FactGap>The controller published no product-build identities.</FactGap>}</ReviewSection>
      <ReviewSection number="06" title="Preflight">{current.preflight.length ? <Stack gap={9}>{current.preflight.map((check) => <div className="preflight-row" key={check.id}><Status value={check.state} /><div><Text fw={650}>{humanize(check.id)}</Text><Text size="sm">{check.message}</Text><Text size="xs"><Identity>{check.reason_code}</Identity> · observed <Identity>{formatTime(check.observed_at)}</Identity></Text>{Object.keys(check.evidence_summary).length ? <Text size="xs">Evidence: <Identity>{jsonFact(check.evidence_summary)}</Identity></Text> : null}</div></div>)}</Stack> : <FactGap>No named preflight checks were published.</FactGap>}{current.blockers.map((blocker) => <Paper className="review-message review-blocker" key={`${blocker.reason_code}:${blocker.message}`}><Status value="blocked" /><div><Identity>{blocker.reason_code}</Identity><Text size="sm">{blocker.message}</Text></div></Paper>)}{current.warnings.map((warning, index) => <Paper className="review-message" key={index}><Status value="degraded" /><Identity>{jsonFact(warning)}</Identity></Paper>)}</ReviewSection>
      {admit.isPending && <AsyncStateNotice state="admission_pending" detail={`The reviewed ${current.case_count} cases are being admitted; duplicate Start is disabled.`} />}
      {admit.isError && (resolvingAdmissionBlock ? <Loading label="Verifying the admission blocker…" detail="Refreshing lane ownership before offering a recovery action." /> : admissionConflict ? <Paper className="admission-conflict" role="status" aria-live="polite"><Status value="blocked" /><div><Title order={3}>Another run owns the execution lane.</Title><Text>{conflictRunId ? conflictRun.data ? <>Run <Identity>{conflictRunId}</Identity> is {humanize(conflictRun.data.state).toLowerCase()}; your reviewed scope was not started.</> : <>Run <Identity>{conflictRunId}</Identity> owns the lane; your reviewed scope was not started. Its state is being verified.</> : "The controller reported an active-run conflict, but the current lane owner is not available from health."}</Text>{admissionResponseAt && admissionConflictError && <Text size="xs">Conflict observed <Identity>{formatTime(new Date(admissionResponseAt).toISOString())}</Identity>. Request <Identity>{admissionConflictError.response.request_id}</Identity>.</Text>}{conflictRunId && <Button component={Link} to={`/e2e/runs/${conflictRunId}`} variant="light" leftSection={<Icon name="run" />}>Open active run</Button>}</div></Paper> : <Failure error={admit.error} headline="Starting one run failed." />)}
      <div className="dialog-actions"><Button variant="default" onClick={onClose}>Cancel</Button><div className="start-action"><Text size="xs" role="status">{resolvingAdmissionBlock ? "Start is disabled while current lane ownership is verified." : admissionConflict ? "Start is disabled because another run owns the serial lane; this reviewed scope remains unchanged." : startAllowed ? "Creates one run from this exact frozen preview." : current.state === "ready" && !current.admission_token ? "The ready preview did not include an admission token." : blockedReason}</Text>{refreshable && <Button variant="subtle" onClick={requestPreview} leftSection={<Icon name="refresh" />}>Refresh preview</Button>}<Button loading={admit.isPending} disabled={admit.isPending || !startAllowed} onClick={startRun} leftSection={<Icon name="run" />}>Start run</Button></div></div>
    </>}</Stack>
  </Modal>;
}

function ReviewSection({ number, title, children }: { number: string; title: string; children: ReactNode }) {
  return <section className="review-section"><div className="review-section-title"><span>{number}</span><Title order={3}>{title}</Title></div><div>{children}</div></section>;
}

function RunsPage() {
  const [params, setParams] = useSearchParams();
  const cursor = params.get("cursor");
  const runParams = new URLSearchParams();
  if (cursor) runParams.set("cursor", cursor);
  const runs = useQuery({ queryKey: ["runs", cursor ?? "first"], queryFn: () => api.runs(runParams) });
  const health = useQuery({ queryKey: ["health"], queryFn: () => api.health() });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  useEffect(() => { if (runs.data?.items.length && !selectedId) setSelectedId(runs.data.items[0].run_id); }, [runs.data?.items, selectedId]);
  if (runs.isPending) return <Page eyebrow="Runs" title="Operational run history"><Loading label="Loading run history…" /></Page>;
  if (runs.isError) return <Page eyebrow="Runs" title="Operational run history"><Failure error={runs.error} headline="Run history is unavailable." /></Page>;
  const data = runs.data;
  const selected = data.items.find((item) => item.run_id === selectedId) ?? data.items[0];
  const stateCounts = data.items.reduce<Record<string, number>>((counts, item) => ({ ...counts, [item.state]: (counts[item.state] ?? 0) + 1 }), {});
  return <Page eyebrow="Run history" title="Operational run history" description="Review the controller’s verified run index, then open a record for structured evidence and failure context." className="runs-page">
    {health.data?.lane.active_run_id && <Paper className="active-run-callout"><Group gap="sm" wrap="nowrap"><span className="feature-icon"><Icon name="activity" /></span><div><Text className="eyebrow">Active serial lane</Text><Text fw={700}>Run <Identity>{health.data.lane.active_run_id}</Identity> owns the active lane.</Text></div></Group><Button component={Link} to={`/e2e/runs/${health.data.lane.active_run_id}`} variant="light">Open active run</Button></Paper>}
    {data.history_state === "partial" && <AsyncStateNotice state="history_partial" detail={`${data.corrupt_records} run records could not be read; visible rows remain verified.`} />}
    {data.items.length ? <><div className="runs-index-bar"><div><Text fw={700}>{data.items.length} verified {data.items.length === 1 ? "record" : "records"} on this page</Text><Text size="xs">All results · this controller publishes pagination, not server-side history filters.</Text></div><div className="state-summary">{Object.entries(stateCounts).map(([state, count]) => <span key={state}><Status value={state} /><strong>{count}</strong></span>)}</div></div><div className="runs-workspace"><div className="runs-list-column"><RunTable data={data} selectedId={selected?.run_id} onSelect={setSelectedId} />{data.page.next_cursor && <Group justify="flex-end" className="pagination-row"><Button onClick={() => { const next = new URLSearchParams(params); next.set("cursor", data.page.next_cursor ?? ""); setParams(next); }}>Next page</Button></Group>}</div><RunIndexDetail run={selected} /></div></> : <Paper className="empty-state"><span className="feature-icon"><Icon name="history" /></span><Title order={3}>{data.history_state === "partial" ? "No readable runs on this page." : cursor ? "No runs on this page." : "No runs yet."}</Title><Text>{data.history_state === "partial" ? "History is partial; unreadable records are reported above." : cursor ? "Return to the first page or use browser history." : "Start from Catalog to create the first run."}</Text><Button component={Link} to={cursor ? "/e2e/runs" : "/e2e/catalog"}>{cursor ? "First page" : "Open Catalog"}</Button></Paper>}
  </Page>;
}

function RunTable({ data, selectedId, onSelect }: { data: RunsPage; selectedId?: string; onSelect: (id: string) => void }) {
  return <section aria-label="Run records" className="run-table-surface"><div className="run-table-desktop"><Table><Table.Thead><Table.Tr><Table.Th>Run</Table.Th><Table.Th>Result</Table.Th><Table.Th>Created</Table.Th><Table.Th>Counts</Table.Th><Table.Th>Evidence</Table.Th><Table.Th>Retention</Table.Th><Table.Th><span className="sr-only">Open</span></Table.Th></Table.Tr></Table.Thead><Table.Tbody>{data.items.map((run) => { const selected = selectedId === run.run_id; return <Table.Tr key={run.run_id} className={selected ? "selected-row" : ""}><Table.Td><button className="table-select" type="button" onClick={() => onSelect(run.run_id)} aria-pressed={selected}><Identity>{run.run_id}</Identity></button></Table.Td><Table.Td><Status value={run.state} /></Table.Td><Table.Td><time dateTime={run.created_at}>{formatTime(run.created_at)}</time></Table.Td><Table.Td>{Object.values(run.case_counts ?? {}).reduce((sum, count) => sum + count, 0)}</Table.Td><Table.Td><Status value={run.evidence_health} /></Table.Td><Table.Td>{humanize(run.retention?.state)}</Table.Td><Table.Td><Button component={Link} to={`/e2e/runs/${run.run_id}`} variant="subtle" aria-label={`Open run ${run.run_id}`}><Icon name="chevron" /></Button></Table.Td></Table.Tr>; })}</Table.Tbody></Table></div><div className="run-cards-mobile">{data.items.map((run) => { const selected = selectedId === run.run_id; return <Card key={run.run_id} className={selected ? "run-record-card selected-row" : "run-record-card"}><button type="button" className="run-card-select" onClick={() => onSelect(run.run_id)} aria-pressed={selected}><Group justify="space-between" wrap="nowrap"><Identity>{run.run_id}</Identity><Status value={run.state} /></Group><dl><dt>Created</dt><dd><time dateTime={run.created_at}>{formatTime(run.created_at)}</time></dd><dt>Counts</dt><dd>{Object.values(run.case_counts ?? {}).reduce((sum, count) => sum + count, 0)} cases</dd><dt>Evidence</dt><dd>{humanize(run.evidence_health)}</dd><dt>Retention</dt><dd>{humanize(run.retention?.state)}</dd></dl></button><Button component={Link} to={`/e2e/runs/${run.run_id}`} variant="light" fullWidth>Open run</Button></Card>; })}</div></section>;
}

function RunIndexDetail({ run }: { run: RunsPage["items"][number] | undefined }) {
  if (!run) return null;
  return <aside className="run-index-detail"><Text className="eyebrow">Selected record</Text><Title order={2}>Run detail</Title><Identity>{run.run_id}</Identity><Divider my="md" /><DetailSection title="Verdict"><Status value={run.state} /></DetailSection><DetailSection title="Selection"><Text>{Object.values(run.case_counts ?? {}).reduce((sum, count) => sum + count, 0)} recorded cases</Text><div className="count-list">{Object.entries(run.case_counts ?? {}).map(([state, count]) => <span key={state}><strong>{count}</strong> {humanize(state)}</span>)}</div></DetailSection><DetailSection title="Created"><time dateTime={run.created_at}>{formatTime(run.created_at)}</time></DetailSection><DetailSection title="Source revision">{run.source_revision ? <Identity>{run.source_revision}</Identity> : <FactGap>Not published in this history row.</FactGap>}</DetailSection><DetailSection title="Evidence and retention"><Group gap="xs"><Status value={run.evidence_health} /><Status value={run.retention?.state} /></Group></DetailSection><Button component={Link} to={`/e2e/runs/${run.run_id}`} fullWidth rightSection={<Icon name="chevron" />}>Open full run</Button></aside>;
}

function RunPage() {
  const { runId = "" } = useParams();
  const hero = useRef<HTMLElement | null>(null);
  const connection = useContext(ControllerConnectionContext);
  const run = useQuery({ queryKey: ["run", runId], queryFn: () => api.run(runId) });
  const health = useQuery({ queryKey: ["health"], queryFn: () => api.health() });
  const [selectedIdentity, setSelectedIdentity] = useState<string | null>(null);
  const [selectedEvidence, setSelectedEvidence] = useState<{ caseIdentity: string; record: EvidenceRecord } | null>(null);
  useEffect(() => { if (run.data) hero.current?.focus(); }, [run.data?.run_id, runId]);
  if (run.isPending) return <Page eyebrow="Run" title="Verified run state"><Loading label="Loading verified run state…" /></Page>;
  if (run.isError) return <Page eyebrow="Run" title="Verified run state"><Failure error={run.error} headline="Run data is unavailable." /></Page>;
  const data = run.data;
  const failure = data.failures?.find((item) => item.id === data.first_failure_id) ?? data.failures?.[0];
  const primaryFailure = data.failures?.find((item) => item.id === data.primary_failure_id);
  const terminal = terminalStates.has(data.state);
  const ownsLane = health.data?.lane.active_run_id === data.run_id;
  const live = !terminal && ownsLane && connection === "live";
  const terminalState = data.state === "passed" ? "terminal_passed" : data.state === "failed" ? "terminal_failed" : data.state === "error" ? "terminal_error" : null;
  const terminalDetail = data.state === "passed" ? `Required checks and cleanup passed. Evidence health: ${humanize(data.evidence_health)}.` : data.state === "failed" ? `First failure: ${failure?.message ?? "unknown"}. Primary cause: ${primaryFailure?.message ?? data.primary_failure_id ?? "unknown"}. Cleanup and evidence remain explicit.` : data.state === "error" ? `Infrastructure or contract failure: ${failure?.message ?? "no named cause was recorded"}. Product assertions are shown separately.` : undefined;
  const evidencePurged = data.retention?.state === "purged";
  const recoveryMismatch = data.recovery_bundle_match === "mismatch";
  const recoveryBlocked = recoveryMismatch || Boolean(data.recovery?.blocker);
  const safetyReason = recoveryMismatch ? "Actions are disabled because the controller bundle does not match this run." : data.recovery?.blocker ? "Actions are disabled because the run projection publishes a recovery blocker." : null;
  const actionReason = safetyReason ?? (evidencePurged ? "The controller reports that retained evidence is already purged." : "This run projection does not publish mutation permissions, so no Cancel or Purge action is exposed.");
  const cases = data.cases ?? [];
  const defaultCase = cases.find((item) => ["failed", "error"].includes(item.state)) ?? cases[0];
  const selectedCase = cases.find((item) => `${item.test_id}:${item.case_id}` === selectedIdentity) ?? defaultCase;
  const total = Object.values(data.case_counts ?? {}).reduce((sum, count) => sum + count, 0) || cases.length;
  const completed = Object.entries(data.case_counts ?? {}).filter(([state]) => !["queued", "running", "preparing"].includes(state)).reduce((sum, [, count]) => sum + count, 0);
  const progress = total ? Math.min(100, (completed / total) * 100) : 0;
  const logRecords = cases.flatMap((item) => (item.evidence ?? []).filter((record) => record.type === "log.recorded").map((record) => ({ caseIdentity: `${item.test_id}:${item.case_id}`, title: item.title ?? item.test_id, record })));
  const nonterminalDetail = !terminal && !live ? health.isError ? "Runner health is unavailable, so lane ownership cannot be verified. This is the last persisted projection." : ownsLane ? `Run owns the serial lane, but the event stream is ${humanize(connection).toLowerCase()}; this is the last persisted projection.` : health.data?.lane.active_run_id ? `Run ${health.data.lane.active_run_id} owns the serial lane; this record is a persisted nonterminal projection.` : "No run owns the serial lane; this record is a persisted nonterminal projection." : null;

  return <Stack className="page run-page" gap="xl">
    <div className="run-overview">
      <header ref={hero} tabIndex={-1} className="run-hero"><div><Text className="eyebrow">{terminal ? "Historical verdict" : live ? "Live controller projection" : "Persisted nonterminal projection"}</Text><Title order={1}>Run {data.run_id}</Title><Group gap="sm" mt="sm"><Status value={data.state} inverse /><Text fw={750}>{terminal ? humanize(data.state) : live ? "Execution in progress" : "Live status not verified"}</Text></Group><Text className="run-hero-purpose">Exact evidence and cleanup state for the admitted, revision-qualified selection.</Text></div><div className="hero-identity"><span>Selection</span><Identity>{total} exact {total === 1 ? "case" : "cases"}</Identity><span>Created</span><Identity>{formatTime(data.created_at)}</Identity><span>Last event</span><Identity>{formatTime(data.last_event_at ?? undefined)}</Identity></div></header>
      {failure && <Paper className="first-failure" role="status" aria-live="polite" aria-atomic="true"><div className="failure-icon"><Icon name="alert" /></div><div><Text className="eyebrow">First failure</Text><Title order={3}>{failure.message}</Title><Text size="sm">Severity: {humanize(failure.severity)} · <Identity>{failure.id}</Identity></Text>{primaryFailure && primaryFailure.id !== failure.id && <Paper className="primary-failure"><Text className="eyebrow">Primary verdict cause</Text><Text fw={700}>{primaryFailure.message}</Text><Text size="xs">The controller identifies this separate failure as the primary verdict cause. <Identity>{primaryFailure.id}</Identity></Text></Paper>}</div></Paper>}
      <section className="progress-strip" aria-label="Run progress"><div><Text className="progress-number">{completed}/{total}</Text><Text size="xs">Completed cases</Text></div><Progress value={progress} aria-label={`${Math.round(progress)} percent complete`} /><div className="progress-counts">{Object.entries(data.case_counts ?? {}).map(([state, count]) => <span key={state}><strong>{count}</strong> {humanize(state)}</span>)}</div><div className="freshness"><Icon name="activity" size={15} /><span>Sequence <Identity>{data.applied_through_seq ?? 0}</Identity></span><span>Last event <Identity>{formatTime(data.last_event_at ?? undefined)}</Identity></span><span>{live ? "Fresh stream and active lane verified" : terminal ? "Durable historical projection" : "Live state unverified"}</span></div></section>
      <div className="run-actions">{evidencePurged && <div className="run-action-buttons"><Button disabled variant="light">Evidence purged</Button></div>}<Text size="xs" className="run-action-reason">{actionReason}</Text></div>
    </div>
    {nonterminalDetail && <Paper className="snapshot-note" role="status"><Icon name="clock" /><Text>{nonterminalDetail}</Text></Paper>}
    {data.state === "queued" && <AsyncStateNotice state="run_queued" detail={`Run ${data.run_id} is waiting for the controller-owned serial lane.`} />}
    {data.state === "preparing" && <AsyncStateNotice state="preparing" detail="The controller is preparing owned workspace state; product execution has not started." />}
    {data.state === "cancelling" && <AsyncStateNotice state="cancelling" detail="Product work is stopping while mandatory cleanup continues." />}
    {data.state === "recovering" && !recoveryBlocked && <AsyncStateNotice state="recovering" detail="The controller is reconciling the interrupted run from its durable journal." />}
    {recoveryBlocked && <AsyncStateNotice state="recovery_blocked" detail={safetyReason ?? "Recovery is blocked by a published controller fact; no mutation is available."} />}
    {terminalState && <AsyncStateNotice state={terminalState} detail={terminalDetail} />}
    {evidencePurged && <AsyncStateNotice state="evidence_purged" />}
    {data.evidence_health === "degraded" && <AsyncStateNotice state="evidence_degraded" detail="At least one producer reported partial or unsupported evidence; inspect record availability below." />}
    {data.evidence_health === "unavailable" && <AsyncStateNotice state="evidence_unavailable" detail="At least one evidence producer reported unavailable content; verdict source and record identity remain visible." />}
    {data.evidence_health === "invalid" && <Paper className="evidence-gap evidence-invalid" role="status"><Icon name="alert" /><div><Title order={3}>Evidence is invalid.</Title><Text>At least one producer reported invalid evidence. The UI does not map this state to success.</Text></div></Paper>}
    {data.journal_health === "truncated" && <Paper className="evidence-gap" role="status"><Icon name="alert" /><div><Title order={3}>Run journal ended with a partial record.</Title><Text>The projection includes only complete, verified events through sequence <Identity>{data.applied_through_seq ?? 0}</Identity>.</Text></div></Paper>}
    <section className="evidence-identity"><div><Text className="eyebrow">Evidence identity</Text><Title order={3}>Durable controller facts</Title></div><div><Text size="xs">Catalog revision</Text>{data.catalog_revision ? <Identity>{data.catalog_revision}</Identity> : <Text size="sm">Not published</Text>}</div><div><Text size="xs">Source revision</Text>{data.source_revision ? <Identity>{data.source_revision}</Identity> : <Text size="sm">Not published</Text>}</div><div><Text size="xs">Evidence health</Text><Status value={data.evidence_health} /></div><div><Text size="xs">Retention</Text><Status value={data.retention?.state} /></div></section>
    <div className="live-layout"><section className="case-tree" aria-label="Run cases"><div className="section-heading compact"><div><Text className="eyebrow">Execution tree</Text><Title order={2}>Cases</Title></div></div>{cases.length ? <Stack gap={4}>{cases.map((item, index) => {
      const identity = `${item.test_id}:${item.case_id}`;
      const selected = Boolean(selectedCase && identity === `${selectedCase.test_id}:${selectedCase.case_id}`);
      return <button type="button" key={identity} className={`tree-item${selected ? " selected" : ""}`} onClick={() => setSelectedIdentity(identity)} aria-pressed={selected}><span className="tree-index">{String(index + 1).padStart(2, "0")}</span><span className="tree-copy"><strong>{item.title ?? item.test_id}</strong><Identity>{item.test_id}:{item.case_id}</Identity></span><Status value={item.state} /></button>;
    })}</Stack> : <FactGap>No case projections were published.</FactGap>}</section><RunCaseDetail runId={data.run_id} item={selectedCase} failures={data.failures ?? []} evidencePurged={evidencePurged} onEvidence={(record) => selectedCase && setSelectedEvidence({ caseIdentity: `${selectedCase.test_id}:${selectedCase.case_id}`, record })} /></div>
    <section className="logs-panel"><div className="section-heading compact"><div><Text className="eyebrow">Bounded output</Text><Title order={2}>Log records</Title></div><Status value={logRecords.length ? "retained" : "unavailable"} /></div>{logRecords.length ? <Stack gap={6}>{logRecords.map(({ caseIdentity, title, record }, index) => <button type="button" className="evidence-row evidence-button" key={`${caseIdentity}:${record.seq}:${index}`} onClick={() => setSelectedEvidence({ caseIdentity, record })}><Icon name="terminal" /><div><Text fw={700}>{title}</Text><Identity>{record.evidence_id ?? `Log event at sequence ${record.seq}`}</Identity><Text size="xs">{record.availability ? humanize(record.availability) : "Availability not published"}</Text></div><Icon name="chevron" /></button>)}</Stack> : <FactGap>No log records were published for any case.</FactGap>}</section>
    <EvidenceDrawer runId={data.run_id} selected={selectedEvidence} evidencePurged={evidencePurged} onClose={() => setSelectedEvidence(null)} />
  </Stack>;
}

function RunCaseDetail({ runId, item, failures, evidencePurged, onEvidence }: { runId: string; item: NonNullable<RunProjection["cases"]>[number] | undefined; failures: NonNullable<RunProjection["failures"]>; evidencePurged: boolean; onEvidence: (record: EvidenceRecord) => void }) {
  if (!item) return <section className="run-case-detail"><FactGap>Select a case to inspect its projection.</FactGap></section>;
  const evidence = item.evidence ?? [];
  const caseFailures = failures.filter((failure) => failure.test_id === item.test_id && failure.case_id === item.case_id);
  const runtime = evidence.find((record) => record.type === "artifact.recorded" && record.kind === "runtime_observability");
  return <section className="run-case-detail" aria-label="Selected case detail"><div className="detail-hero"><div><Text className="eyebrow">Selected case</Text><Title order={2}>{item.title ?? item.test_id}</Title><Identity>{item.test_id}:{item.case_id}</Identity></div><Status value={item.state} /></div>{item.state === "not_run" && <Paper className="not-run-note"><Icon name="clock" /><Text>Not run · reason not published by controller.</Text></Paper>}<div className="case-detail-grid"><DetailSection title="Phases">{Object.keys(item.phases ?? {}).length ? <Stack gap={7}>{Object.entries(item.phases ?? {}).map(([name, state]) => <div key={name} className="validation-row"><Status value={state} /><Text>Phase: {name} · {humanize(state)}</Text></div>)}</Stack> : <FactGap>No phase projection was published for this case.</FactGap>}</DetailSection><DetailSection title="Validations">{Object.keys(item.validations ?? {}).length ? <Stack gap={7}>{Object.entries(item.validations ?? {}).map(([name, state]) => <div key={name} className="validation-row"><Status value={state} /><Text>Validation: {name} · {humanize(state)}</Text></div>)}</Stack> : <FactGap>No validation projection was published for this case.</FactGap>}</DetailSection><DetailSection title="Cleanup">{Object.keys(item.cleanup ?? {}).length ? <Stack gap={7}>{Object.entries(item.cleanup ?? {}).map(([name, state]) => <div key={name} className="validation-row"><Status value={state} /><Text>Cleanup: {name} · {humanize(state)}</Text></div>)}</Stack> : <FactGap>No cleanup projection was published for this case.</FactGap>}</DetailSection><DetailSection title="Surfaces">{item.surfaces?.length ? <Stack gap={6}>{item.surfaces.map((surface, index) => <Identity key={index}>{jsonFact(surface)}</Identity>)}</Stack> : <FactGap>No surface proof metadata was published for this case.</FactGap>}</DetailSection></div><RuntimeResources runId={runId} record={runtime} purged={evidencePurged} onEvidence={onEvidence} /><DetailSection title="Evidence">{evidencePurged ? <FactGap>Evidence was purged; verdict and validation lineage remain.</FactGap> : evidence.length ? <Stack gap={6}>{evidence.map((record, index) => <button type="button" className="evidence-row evidence-button" key={`${item.test_id}:${item.case_id}:${record.seq}:${index}`} onClick={() => onEvidence(record)}><Icon name={record.type === "log.recorded" ? "terminal" : "archive"} /><div><Text fw={700}>{humanize(record.type.replace(".recorded", ""))}</Text><Identity>{record.evidence_id ?? `Event at sequence ${record.seq}`}</Identity><Text size="xs">{record.availability ? humanize(record.availability) : "Availability not published"}</Text></div><Icon name="chevron" /></button>)}</Stack> : <FactGap>No case-level evidence records were published.</FactGap>}</DetailSection>{caseFailures.length ? <DetailSection title="Case failures"><Stack gap={6}>{caseFailures.map((caseFailure) => <Paper className="failure-line" key={caseFailure.id}><Text fw={700}>{caseFailure.message}</Text><Text size="xs">{humanize(caseFailure.severity)} · <Identity>{caseFailure.id}</Identity></Text></Paper>)}</Stack></DetailSection> : null}</section>;
}

function EvidenceDrawer({ runId, selected, evidencePurged, onClose }: { runId: string; selected: { caseIdentity: string; record: EvidenceRecord } | null; evidencePurged: boolean; onClose: () => void }) {
  const record = selected?.record;
  const evidenceId = record?.evidence_id;
  const evidence = useQuery({
    queryKey: ["evidence", runId, evidenceId ?? "metadata-only", record?.storage_ref ?? "inline"],
    queryFn: () => api.evidence(runId, evidenceId ?? "", Boolean(record?.storage_ref)),
    enabled: Boolean(selected && evidenceId && !evidencePurged),
    retry: false,
    staleTime: Infinity,
  });
  const omitted = (evidence.data?.omittedBytes ?? 0) > 0 || (evidence.data?.omittedLines ?? 0) > 0;
  return <Drawer opened={Boolean(selected)} onClose={onClose} title="Evidence detail" position="right" size="min(100%, 640px)" classNames={{ root: "evidence-drawer", header: "sheet-header", body: "sheet-body" }}>
    {selected && <Stack gap="lg"><div className="evidence-viewer-heading"><Text className="eyebrow">Bounded controller evidence</Text><Title order={2}>{humanize(record?.type.replace(".recorded", ""))}</Title><Identity>{evidenceId ?? `Event at sequence ${record?.seq}`}</Identity><Text size="sm">Case <Identity>{selected.caseIdentity}</Identity></Text></div><EvidenceMetadata record={record} />
      {evidencePurged ? <AsyncStateNotice state="evidence_purged" detail="The controller reports this run’s retained evidence was purged; the durable record metadata remains above." /> : !evidenceId ? <FactGap>This record has no evidence identifier, so only projection metadata can be inspected.</FactGap> : evidence.isPending ? <Loading label="Loading bounded evidence…" detail="The controller applies redaction and a 5 MiB response cap." /> : evidence.isError ? <EvidenceFailure error={evidence.error} onRetry={() => evidence.refetch()} /> : <>
        {record?.availability === "partial" && <Paper className="evidence-gap" role="status"><Icon name="info" /><Text>The producer marked this record partial; transport cap facts below are separate.</Text></Paper>}
        {omitted && <Paper className="evidence-gap" role="status"><Icon name="archive" /><Text>Response capped: retained {evidence.data.retainedBytes ?? "unknown"} bytes; {evidence.data.omittedBytes ?? "unknown"} bytes and {evidence.data.omittedLines ?? "unknown"} lines omitted.</Text></Paper>}
        {evidence.data.kind === "record" && evidence.data.record && <pre className="evidence-content">{JSON.stringify(evidence.data.record, null, 2)}</pre>}
        {evidence.data.kind === "content" && evidence.data.text !== undefined && <pre className="evidence-content">{evidence.data.text}</pre>}
        {evidence.data.kind === "content" && evidence.data.text === undefined && <FactGap>Binary content is not embedded. The bounded response retained {evidence.data.retainedBytes ?? "an unpublished number of"} bytes with media type <Identity>{evidence.data.mediaType}</Identity>.</FactGap>}
      </>}
    </Stack>}
  </Drawer>;
}

function EvidenceMetadata({ record }: { record: EvidenceRecord | undefined }) {
  if (!record) return null;
  const facts = Object.entries(record).filter(([key]) => !["type", "evidence_id"].includes(key));
  return <section className="evidence-metadata" aria-label="Evidence metadata">{facts.map(([key, value]) => <div key={key}><Text size="xs">{humanize(key)}</Text><Identity>{jsonFact(value ?? null)}</Identity></div>)}</section>;
}

function EvidenceFailure({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const code = error instanceof ApiError ? error.response.code : "unknown";
  const headline = code === "evidence_purged" ? "Evidence was purged." : code === "evidence_corrupt" ? "Evidence is corrupt." : code === "evidence_not_found" || code === "run_not_found" ? "Evidence was not found." : "Evidence could not be loaded.";
  return <Stack gap="sm"><Failure error={error} headline={headline} />{error instanceof ApiError && error.response.retryable && <Button onClick={onRetry} leftSection={<Icon name="refresh" />}>Retry evidence request</Button>}</Stack>;
}

function WorkspacesPage() {
  const workspaces = useQuery({ queryKey: ["workspaces"], queryFn: () => api.workspaces() });
  const queryClient = useQueryClient();
  const prepare = useMutation({ mutationFn: () => api.prepareTemplate(), retry: false, onSuccess: () => {
    void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    void queryClient.invalidateQueries({ queryKey: ["health"] });
  } });
  if (workspaces.isPending) return <Page eyebrow="Workspace safety" title="Workspaces"><Loading label="Loading workspace ownership…" /></Page>;
  if (workspaces.isError) return <Page eyebrow="Workspace safety" title="Workspaces"><Failure error={workspaces.error} headline="Workspace records are unavailable." /></Page>;
  const data = workspaces.data;
  return <Page eyebrow="Owned workspace safety" title="Workspaces" description="Monitor controller-owned templates, isolated attempts, quarantine, and semantic purge lineage without exposing filesystem controls." className="workspaces-page">
    <section className="workspace-safety"><div className="safety-copy"><span className="feature-icon"><Icon name="shield" /></span><div><Text className="eyebrow">Capacity and store safety</Text><Title order={2}>Ownership is the safety boundary</Title><Text>Only controller-owned semantic workspace identities appear here. Paths remain read-only in Runner health.</Text></div></div><div className="safety-facts"><div><strong>{data.active_attempts.length}</strong><span>Active attempts</span></div><div><strong>{data.quarantine.length}</strong><span>Quarantined</span></div><div><strong>{data.recent_purges.length}</strong><span>Recent purges</span></div><FactGap>Byte capacity is not published by this controller.</FactGap></div></section>
    <section className="template-card"><div><Text className="eyebrow">Template</Text><Title order={2}>{data.template.length ? `${data.template.length} template ${data.template.length === 1 ? "record" : "records"} published` : "No template record published"}</Title><Text>{data.template.length ? "The controller reports these owned records; verification readiness is not included." : "Prepare requests a controller-owned template for future isolated attempts."}</Text>{data.template.map((item, index) => <Group key={item.workspace_id ?? index} gap="xs"><Identity>{item.workspace_id ?? "Unnamed template"}</Identity>{item.state && <Status value={String(item.state)} />}</Group>)}</div>{data.template.length === 0 && <Button loading={prepare.isPending} onClick={() => prepare.mutate()} leftSection={<Icon name="database" />}>Prepare template</Button>}</section>
    {prepare.isError && <Failure error={prepare.error} headline="Template preparation could not be requested." />}
    <section className="lifecycle" aria-labelledby="lifecycle-title"><div><Text className="eyebrow">Lifecycle</Text><Title order={2} id="lifecycle-title">A narrow, auditable path</Title></div><LifecycleStep icon="database" number="01" title="Template" text="A controller-owned base record." /><LifecycleStep icon="folder" number="02" title="Attempt" text="One isolated mutable workspace." /><LifecycleStep icon="archive" number="03" title="Quarantine or purge" text="Uncertain cleanup remains visible." /></section>
    <WorkspaceList title="Active attempts" eyebrow="Mutable now" icon="activity" empty="No active attempts.">{data.active_attempts.map((item) => <WorkspaceRow key={item.workspace_id} item={item} />)}</WorkspaceList>
    <FactGap>Purge eligibility is not included in workspace rows, so no destructive workspace action is exposed.</FactGap>
    <WorkspaceList title="Quarantine" eyebrow="Needs attention" icon="alert" empty="No quarantined attempts.">{data.quarantine.map((item) => <WorkspaceRow key={item.workspace_id} item={item} />)}</WorkspaceList>
    <WorkspaceList title="Recent purges" eyebrow="Durable lineage" icon="history" empty="No retained workspaces were purged.">{data.recent_purges.map((item) => <WorkspaceRow key={item.workspace_id} item={{ ...item, role: item.state }} />)}</WorkspaceList>
  </Page>;
}

function LifecycleStep({ icon, number, title, text }: { icon: IconName; number: string; title: string; text: string }) {
  return <div className="lifecycle-step"><span className="step-number">{number}</span><span className="feature-icon"><Icon name={icon} /></span><div><Text fw={750}>{title}</Text><Text size="sm">{text}</Text></div></div>;
}

function WorkspaceList({ title, eyebrow, icon, empty, children }: { title: string; eyebrow: string; icon: IconName; empty: string; children: ReactNode }) {
  const hasChildren = Array.isArray(children) ? children.length > 0 : Boolean(children);
  return <section className="workspace-list"><div className="section-heading"><Group gap="sm" wrap="nowrap"><span className="feature-icon"><Icon name={icon} /></span><div><Text className="eyebrow">{eyebrow}</Text><Title order={2}>{title}</Title></div></Group></div>{hasChildren ? <Stack gap={8}>{children}</Stack> : <Paper className="workspace-empty"><Text>{empty}</Text></Paper>}</section>;
}

function WorkspaceRow({ item, action }: { item: { workspace_id: string; run_id?: string; role: string }; action?: ReactNode }) {
  return <Card className="workspace-row"><div><Identity>{item.workspace_id}</Identity><Text size="sm">{humanize(item.role)}{item.run_id ? <> · Run <Identity>{item.run_id}</Identity></> : " · No run lineage published"}</Text></div>{action}</Card>;
}

export function App() {
  return <Shell><Routes><Route path="/e2e/catalog" element={<CatalogPage />} /><Route path="/e2e/runs" element={<RunsPage />} /><Route path="/e2e/runs/:runId" element={<RunPage />} /><Route path="/e2e/workspaces" element={<WorkspacesPage />} /><Route path="*" element={<Navigate to="/e2e/catalog" replace />} /></Routes></Shell>;
}
