import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import axe from "axe-core";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter } from "react-router";
import { beforeEach, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { asyncStateFixture, catalog, catalogCases, health, preview, run } from "./fixtures";
import { evidenceFetches, FixtureEventSource, refreshBodies, runFetches, server, templateBodies } from "./setup";
import { HttpResponse, http } from "msw";

function renderRoom(path = "/e2e/catalog") {
  window.history.pushState({}, "", path);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<MantineProvider defaultColorScheme="dark"><QueryClientProvider client={client}><BrowserRouter><App /></BrowserRouter></QueryClientProvider></MantineProvider>);
}

beforeEach(() => { document.body.innerHTML = ""; });

it("renders catalog taxonomy directly from records and supports keyboard review/admission", async () => {
  const user = userEvent.setup();
  renderRoom();
  await screen.findByRole("button", { name: "Manager · 1 cases" });
  for (const domain of ["Manager", "Runtime", "Observability", "Compound", "Translation"]) expect(screen.getByRole("button", { name: new RegExp(`^${domain} ·`) })).toBeTruthy();
  expect(screen.getByRole("button", { name: /^Harness Diagnostics ·/ })).toBeTruthy();
  for (const family of ["command", "file", "daemon_http", "network_isolation", "reserved_paths", "shell_security", "workspace_session", "snapshot", "trace", "events", "cgroup", "layerstack", "catalog", "runner", "reducer", "storage", "api", "ui"]) expect(screen.getByRole("button", { name: new RegExp(`^${family.replaceAll("_", " ")} \\(`, "i") })).toBeTruthy();
  const selection = screen.getByRole("checkbox", { name: `Select ${catalogCases[0].title}` });
  selection.focus();
  await user.keyboard(" ");
  expect((selection as HTMLInputElement).checked).toBe(true);
  const review = screen.getByRole("button", { name: "Review run" });
  review.focus();
  await user.keyboard("{Enter}");
  await screen.findByText("Ready to start 1 exact cases.");
  const start = screen.getByRole("button", { name: "Start run" });
  start.focus();
  await user.keyboard("{Enter}");
  await screen.findByRole("heading", { name: `Run ${run.run_id}` });
  expect(document.activeElement?.closest("header")?.textContent).toContain(`Run ${run.run_id}`);
});

it("reviews the full catalog and every filter through server-side query selections", async () => {
  const previewSelections: Array<Record<string, unknown>> = [];
  server.use(http.post("*/api/v1/previews", async ({ request }) => {
    const body = JSON.parse(await request.text()) as { selection: Record<string, unknown> };
    previewSelections.push(body.selection);
    return HttpResponse.json({ schema_version: 1, data: { ...preview, case_count: catalog.total, cases: catalog.items, ordered_cases: catalog.items } });
  }));
  const user = userEvent.setup();
  renderRoom();

  await user.click(await screen.findByRole("button", { name: `Run all ${catalog.total} cases` }));
  await waitFor(() => expect(previewSelections).toHaveLength(1));
  expect(previewSelections[0]).toMatchObject({
    catalog_revision: catalog.catalog_revision,
    include: [{ query: { kind: Object.keys(catalog.facets.kind) } }],
    exclude: [],
  });
  await user.click(await screen.findByRole("button", { name: "Cancel" }));

  for (const [field, values] of Object.entries(catalog.facets)) {
    for (const [value, count] of Object.entries(values)) {
      const label = value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
      const groupLabel = field === "domain_id" ? "domain" : field === "family_id" ? "family" : "kind";
      expect(screen.getByRole("button", { name: `Run all ${count} ${count === 1 ? "case" : "cases"} in ${label} ${groupLabel}` })).toBeTruthy();
    }
  }

  await user.click(screen.getByRole("button", { name: "Run all 1 case in Manager domain" }));
  await waitFor(() => expect(previewSelections).toHaveLength(2));
  expect(previewSelections[1]).toMatchObject({ include: [{ query: { domain_id: "manager" } }] });
});

it("publishes pressed facet state and exact compound facts in detail and Review scope", async () => {
  const compound = catalogCases.find((item) => item.domain_id === "compound");
  if (!compound) throw new Error("Compound fixture is missing.");
  server.use(http.post("*/api/v1/previews", () => HttpResponse.json({
    schema_version: 1,
    data: { ...preview, cases: [compound], ordered_cases: [compound] },
  })));
  const user = userEvent.setup();
  renderRoom();
  let runtimeFacet = await screen.findByRole("button", { name: "Runtime (7)" });
  expect(runtimeFacet.getAttribute("aria-pressed")).toBe("false");
  await user.click(runtimeFacet);
  runtimeFacet = await screen.findByRole("button", { name: "Runtime (7)" });
  expect(runtimeFacet.getAttribute("aria-pressed")).toBe("true");

  await user.click(screen.getByRole("button", { name: `Open details for ${compound.title}` }));
  const detail = screen.getByText("Compound context").closest("section");
  expect(detail?.textContent).toContain("manager.management · Subject");
  expect(detail?.textContent).toContain("runtime.command · Subject");
  expect(detail?.textContent).toContain("Workspace: Shared");
  expect(detail?.textContent).toContain("pytest fixture teardown");

  await user.click(screen.getByRole("checkbox", { name: `Select ${compound.title}` }));
  await user.click(screen.getByRole("button", { name: "Review run" }));
  await screen.findByText("Ready to start 1 exact cases.");
  const reviewCase = screen.getAllByText(compound.title).at(-1)?.closest(".review-case");
  expect(reviewCase?.textContent).toContain("manager.management · Subject");
  expect(reviewCase?.textContent).toContain("runtime.command · Subject");
  expect(reviewCase?.textContent).toContain("Workspace: Shared");
  expect(reviewCase?.textContent).toContain("pytest fixture teardown");
});

it("reuses one admission idempotency key when Start is retried for the same preview", async () => {
  const bodies: Array<Record<string, string>> = [];
  let attempts = 0;
  server.use(http.post("*/api/v1/runs", async ({ request }) => {
    bodies.push(JSON.parse(await request.text()) as Record<string, string>);
    attempts += 1;
    if (attempts === 1) return HttpResponse.json({ schema_version: 1, error: { code: "temporarily_unavailable", message: "admission result was not confirmed", retryable: true, request_id: "retry-fixture" } }, { status: 503 });
    return HttpResponse.json({ schema_version: 1, data: { run_id: run.run_id } });
  }));
  const user = userEvent.setup();
  renderRoom();
  await user.click(await screen.findByRole("checkbox", { name: `Select ${catalogCases[0].title}` }));
  await user.click(screen.getByRole("button", { name: "Review run" }));
  const start = await screen.findByRole("button", { name: "Start run" });
  await user.click(start);
  await screen.findByRole("heading", { name: "Starting one run failed." });
  await user.click(screen.getByRole("button", { name: "Start run" }));
  await screen.findByRole("heading", { name: `Run ${run.run_id}` });
  expect(bodies).toHaveLength(2);
  expect(bodies[0].idempotency_key).toBeTruthy();
  expect(bodies[1].idempotency_key).toBe(bodies[0].idempotency_key);
});

it("disables Start when a ready preview expires and offers a verified refresh", async () => {
  const expiresAt = new Date(Date.now() + 500).toISOString();
  server.use(http.post("*/api/v1/previews", () => HttpResponse.json({ schema_version: 1, data: { ...preview, expires_at: expiresAt } })));
  const user = userEvent.setup();
  renderRoom();
  await user.click(await screen.findByRole("checkbox", { name: `Select ${catalogCases[0].title}` }));
  await user.click(screen.getByRole("button", { name: "Review run" }));
  await screen.findByText("Ready to start 1 exact cases.");
  const start = screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement;
  expect(start.disabled).toBe(false);
  await screen.findByRole("heading", { name: "This review has expired." }, { timeout: 2_000 });
  expect(start.disabled).toBe(true);
  expect(screen.getByRole("button", { name: "Refresh preview" })).toBeTruthy();
  expect(screen.getByText("This preview expired before admission. Refresh it to verify readiness again.")).toBeTruthy();
});

it("preserves Review and links the verified lane owner after the controller reports a lane admission block", async () => {
  let laneOwned = false;
  server.use(
    http.get("*/api/v1/health", () => HttpResponse.json({ schema_version: 1, data: { ...health, lane: { active_run_id: laneOwned ? run.run_id : null } } })),
    http.post("*/api/v1/runs", () => {
      laneOwned = true;
      return HttpResponse.json({ schema_version: 1, error: { code: "admission_blocked", message: "Another nonterminal run owns the serial lane.", retryable: true, request_id: "conflict-fixture" } }, { status: 409 });
    }),
    http.get(`*/api/v1/runs/${run.run_id}`, () => HttpResponse.json({ schema_version: 1, data: { ...run, state: "running" } })),
  );
  const user = userEvent.setup();
  renderRoom();
  await user.click(await screen.findByRole("checkbox", { name: `Select ${catalogCases[0].title}` }));
  await user.click(screen.getByRole("button", { name: "Review run" }));
  await user.click(await screen.findByRole("button", { name: "Start run" }));
  const heading = await screen.findByRole("heading", { name: "Another run owns the execution lane." });
  const conflict = heading.closest(".admission-conflict");
  await waitFor(() => expect(conflict?.textContent).toContain(`Run ${run.run_id} is running; your reviewed scope was not started.`));
  expect(conflict?.textContent).toContain("Request conflict-fixture");
  expect(screen.getByRole("link", { name: "Open active run" }).getAttribute("href")).toBe(`/e2e/runs/${run.run_id}`);
  expect(screen.getByRole("dialog", { name: "Review run" })).toBeTruthy();
  expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).disabled).toBe(true);
});

it("does not mislabel a non-lane admission block when fresh health has no lane owner", async () => {
  server.use(http.post("*/api/v1/runs", () => HttpResponse.json({
    schema_version: 1,
    error: { code: "admission_blocked", message: "Insufficient free bytes for the estimated run and finalization reserve.", retryable: true, request_id: "disk-fixture" },
  }, { status: 409 })));
  const user = userEvent.setup();
  renderRoom();
  await user.click(await screen.findByRole("checkbox", { name: `Select ${catalogCases[0].title}` }));
  await user.click(screen.getByRole("button", { name: "Review run" }));
  await user.click(await screen.findByRole("button", { name: "Start run" }));
  const failure = await screen.findByRole("heading", { name: "Starting one run failed." });
  expect(failure.closest("section")?.textContent).toContain("Insufficient free bytes");
  expect(screen.queryByRole("heading", { name: "Another run owns the execution lane." })).toBeNull();
});

it("does not infer ready when catalog health omits its state", async () => {
  server.use(http.get("*/api/v1/health", () => HttpResponse.json({ schema_version: 1, data: { ...health, catalog: {} } })));
  renderRoom();
  await screen.findByRole("heading", { name: "Know what the system can prove" });
  const runner = document.querySelector(".sidebar-runner");
  await waitFor(() => expect(runner?.textContent).toContain("UnknownRunner"));
  expect(runner?.textContent).not.toContain("ReadyRunner");
});

it("renders every domain and the full harness count from facets on a paged catalog", async () => {
  server.use(http.get("*/api/v1/catalog", () => HttpResponse.json({
    schema_version: 1,
    data: { ...catalog, items: catalog.items.slice(0, 1), page: { ...catalog.page, next_cursor: "next" } },
  })));
  renderRoom();
  await screen.findByRole("button", { name: "Manager · 1 cases" });
  expect(screen.getByRole("button", { name: "Runtime · 7 cases" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Harness Diagnostics · 6 cases" })).toBeTruthy();
});

it("links sidebar domain counts to the matching catalog filter", async () => {
  const user = userEvent.setup();
  renderRoom("/e2e/runs");
  const runtime = await screen.findByRole("link", { name: "Runtime · 7 cases" });
  expect(runtime.getAttribute("href")).toBe("/e2e/catalog?domain_id=runtime");
  runtime.focus();
  await user.keyboard("{Enter}");
  await waitFor(() => expect(`${window.location.pathname}${window.location.search}`).toBe("/e2e/catalog?domain_id=runtime"));
  expect((await screen.findByRole("link", { name: "Runtime · 7 cases" })).getAttribute("aria-current")).toBe("page");
  expect(screen.getByRole("button", { name: "Runtime (7)" }).getAttribute("aria-pressed")).toBe("true");
});

it("uses a bodyless refresh request, exposes empty workspaces, and has no serious axe violations", async () => {
  const user = userEvent.setup();
  renderRoom();
  await user.click(await screen.findByRole("button", { name: "Refresh catalog" }));
  await waitFor(() => expect(refreshBodies).toEqual([""]));
  const results = await axe.run(document, { rules: { "color-contrast": { enabled: false } } });
  expect(results.violations.filter((item) => ["critical", "serious"].includes(item.impact ?? "")).map((item) => item.id)).toEqual([]);
  const workspaces = screen.getByRole("link", { name: "Workspaces" });
  workspaces.focus();
  await user.keyboard("{Enter}");
  await screen.findByRole("heading", { name: "Workspaces" });
  expect(screen.getByText("No active attempts.")).toBeTruthy();
  expect(screen.getByText("No quarantined attempts.")).toBeTruthy();
  const prepare = screen.getByRole("button", { name: "Prepare template" });
  prepare.focus();
  await user.keyboard("{Enter}");
  await waitFor(() => expect(templateBodies).toEqual([""]));
});

it("keeps the complete async-state fixture matrix available for view-contract tests", () => {
  expect(asyncStateFixture).toHaveLength(40);
  expect(asyncStateFixture).toContain("recovery_blocked");
  expect(asyncStateFixture).toContain("evidence_purged");
});

it("refreshes exactly one run snapshot for one SSE gap and gives the operator a reconnecting notice", async () => {
  renderRoom(`/e2e/runs/${run.run_id}`);
  await screen.findByRole("heading", { name: `Run ${run.run_id}` });
  expect(runFetches).toHaveLength(1);
  expect(FixtureEventSource.instances).toHaveLength(1);
  const initialStream = FixtureEventSource.instances[0];
  expect(initialStream.url).toContain("after=0");
  initialStream.emit("stream.gap");
  await screen.findByRole("heading", { name: "Reconnecting to live updates…" });
  await waitFor(() => expect(runFetches).toHaveLength(2));
  await waitFor(() => expect(FixtureEventSource.instances).toHaveLength(2));
  expect(initialStream.close).toHaveBeenCalled();
  expect(FixtureEventSource.instances[1].url).toContain("after=42");
  FixtureEventSource.instances[1].onopen?.(new Event("open"));
  FixtureEventSource.instances[1].fail();
  await new Promise((resolve) => setTimeout(resolve, 25));
  expect(runFetches).toHaveLength(2);
});

it("refreshes exactly one run snapshot for one SSE disconnect", async () => {
  renderRoom(`/e2e/runs/${run.run_id}`);
  await screen.findByRole("heading", { name: `Run ${run.run_id}` });
  FixtureEventSource.instances[0].fail();
  await screen.findByRole("heading", { name: "Reconnecting to live updates…" });
  await waitFor(() => expect(runFetches).toHaveLength(2));
  expect(screen.getAllByRole("heading", { name: "Reconnecting to live updates…" })).toHaveLength(1);
});

it("treats a heartbeat-terminated SSE response as a healthy reconnect cycle", async () => {
  renderRoom(`/e2e/runs/${run.run_id}`);
  await screen.findByRole("heading", { name: `Run ${run.run_id}` });
  FixtureEventSource.instances[0].emit("stream.heartbeat");
  FixtureEventSource.instances[0].fail();
  await new Promise((resolve) => setTimeout(resolve, 25));
  expect(screen.queryByRole("heading", { name: "Reconnecting to live updates…" })).toBeNull();
  expect(runFetches).toHaveLength(1);
});

it("marks a stream stale after the specified 15-second freshness window", async () => {
  renderRoom(`/e2e/runs/${run.run_id}`);
  await screen.findByRole("heading", { name: `Run ${run.run_id}` });
  vi.useFakeTimers();
  try {
    act(() => FixtureEventSource.instances[0].emit("stream.heartbeat"));
    await act(async () => vi.advanceTimersByTimeAsync(14_999));
    expect(screen.queryByRole("heading", { name: "Live updates delayed." })).toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByRole("heading", { name: "Live updates delayed." })).toBeTruthy();
    expect(screen.getByText("No controller heartbeat arrived within 15 seconds; displayed data remains the last verified projection.")).toBeTruthy();
  } finally {
    vi.useRealTimers();
  }
});

it("labels a nonterminal run live only when both lane ownership and a fresh stream are verified", async () => {
  server.use(
    http.get("*/api/v1/health", () => HttpResponse.json({ schema_version: 1, data: { ...health, lane: { active_run_id: run.run_id } } })),
    http.get(`*/api/v1/runs/${run.run_id}`, () => HttpResponse.json({ schema_version: 1, data: { ...run, state: "running" } })),
  );
  renderRoom(`/e2e/runs/${run.run_id}`);
  await screen.findByText("Persisted nonterminal projection");
  FixtureEventSource.instances[0].emit("stream.heartbeat");
  await screen.findByText("Live controller projection");
});

it("keeps lane ownership distinct from verified stream freshness in run history", async () => {
  server.use(http.get("*/api/v1/health", () => HttpResponse.json({ schema_version: 1, data: { ...health, lane: { active_run_id: run.run_id } } })));
  renderRoom("/e2e/runs");
  const callout = (await screen.findByText("Active serial lane")).closest(".active-run-callout");
  expect(callout?.textContent).toContain(`Run ${run.run_id} owns the active lane.`);
  expect(screen.getByRole("link", { name: "Open active run" })).toBeTruthy();
});

it("does not infer a fail-fast cause for not-run cases", async () => {
  const user = userEvent.setup();
  renderRoom(`/e2e/runs/${run.run_id}`);
  await screen.findByRole("heading", { name: `Run ${run.run_id}` });
  await user.click(screen.getByRole("button", { name: /File edit.*Not Run/i }));
  expect(await screen.findByText("Not run · reason not published by controller.")).toBeTruthy();
});

it("renders durable projection states and terminal retention without placeholders", async () => {
  const passed = {
    ...run,
    state: "passed",
    evidence_health: "complete",
    retention: { state: "purged" },
    cases: [{
      test_id: "harness.api.transport-evidence",
      case_id: "default",
      title: "Transport evidence",
      state: "passed",
      validations: { transport: "passed", evidence: "passed" },
      cleanup: { workspace: "passed" },
    }],
  };
  server.use(http.get(`*/api/v1/runs/${run.run_id}`, () => HttpResponse.json({ schema_version: 1, data: passed })));
  renderRoom(`/e2e/runs/${run.run_id}`);
  await screen.findByText("Required checks and cleanup passed. Evidence health: Complete.");
  expect(screen.getByText("Validation: transport · Passed")).toBeTruthy();
  expect(screen.getByText("Cleanup: workspace · Passed")).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Evidence was purged." })).toBeTruthy();
  expect((screen.getByRole("button", { name: "Evidence purged" }) as HTMLButtonElement).disabled).toBe(true);
  expect(document.body.textContent).not.toContain("{health}");
});

it("renders a newly discovered catalog record after an ordinary revision notification", async () => {
  const discovered = { ...catalogCases[0], test_id: "runtime.additive", case_id: "folder", title: "New folder catalog record", family_id: "additive", source: "e2e/runtime/additive/test_case.py" };
  const next = { ...catalog, catalog_revision: "sha256:fixture-catalog-v2", items: [...catalog.items, discovered], total: catalog.total + 1, facets: { ...catalog.facets, family_id: { ...catalog.facets.family_id, additive: 1 } } };
  let requests = 0;
  server.use(http.get("*/api/v1/catalog", () => {
    requests += 1;
    return HttpResponse.json({ schema_version: 1, data: requests === 1 ? catalog : next });
  }));
  renderRoom();
  await screen.findByRole("button", { name: "Manager · 1 cases" });
  expect(FixtureEventSource.instances).toHaveLength(1);
  FixtureEventSource.instances[0].emit("catalog.revision");
  await waitFor(() => expect(screen.getAllByText("New folder catalog record").length).toBeGreaterThan(0));
});

it("does not retry rejected controller actions and keeps response canaries out of the UI", async () => {
  let refreshCalls = 0;
  const secret = "ui-plain-secret-canary";
  const encoded = btoa(secret);
  server.use(
    http.post("*/api/v1/catalog/refresh", () => {
      refreshCalls += 1;
      return HttpResponse.json({ schema_version: 1, error: { code: "blocked", message: "refresh blocked", retryable: false, request_id: "fixture" } }, { status: 409 });
    }),
    http.get(`*/api/v1/runs/${run.run_id}`, () => HttpResponse.json({ schema_version: 1, data: { ...run, internal_secret: `${secret} ${encoded}` } })),
  );
  const user = userEvent.setup();
  renderRoom();
  await user.click(await screen.findByRole("button", { name: "Refresh catalog" }));
  await screen.findByRole("heading", { name: "Catalog update failed — showing the last good revision." });
  await new Promise((resolve) => setTimeout(resolve, 25));
  expect(refreshCalls).toBe(1);
  window.history.pushState({}, "", `/e2e/runs/${run.run_id}`);
  window.dispatchEvent(new PopStateEvent("popstate"));
  const evidenceButtons = await screen.findAllByRole("button", { name: /log-runtime-file/i });
  expect(evidenceButtons).toHaveLength(1);
  expect(screen.queryByRole("heading", { name: "Log records" })).toBeNull();
  await user.click(evidenceButtons[0]);
  await screen.findByText("Response capped: retained 1024 bytes; 2048 bytes and 16 lines omitted.");
  expect(screen.getByText(/first bounded log line/)).toBeTruthy();
  expect(evidenceFetches).toHaveLength(1);
  expect(document.body.textContent).not.toContain(secret);
  expect(document.body.textContent).not.toContain(encoded);
});

it("shows a stable controller error when an API route returns HTML", async () => {
  server.use(http.get("*/api/v1/catalog", () => new HttpResponse("<!doctype html><title>Not the API</title>", { headers: { "Content-Type": "text/html" } })));
  renderRoom();
  await screen.findByRole("heading", { name: "Test catalog is unavailable." });
  expect(screen.getByText("The controller returned an invalid response. Request unknown.")).toBeTruthy();
});

const runtimeArtifact = (overrides: Record<string, unknown> = {}) => ({
  type: "artifact.recorded" as const,
  seq: 50,
  evidence_id: "runtime-fixture",
  kind: "runtime_observability",
  role: "supporting",
  status: "available",
  availability: "available",
  storage_ref: "runtime/fixture.ndjson",
  sha256: "sha256:runtime-fixture",
  media_type: "application/x-ndjson",
  summary: {
    scopes: [
      { sandbox_id: "eos-fixture", scope: { kind: "sandbox", id: "sandbox" }, sample_count: 3, cpu_peak_cores: 1.25, cpu_time_seconds: 0, memory_peak_bytes: 536870912, memory_limit_unlimited: true, io_read_bytes: 0, io_write_bytes: 1048576 },
      { sandbox_id: "eos-fixture", scope: { kind: "workspace", id: "ws-fixture" }, sample_count: 2, disk_peak_bytes: 2147483648, disk_allocated_peak_bytes: 1073741824, file_peak: 42, disk_truncated: true },
    ],
  },
  coverage: { expected_ticks: 4, observed_ticks: 3, missed_ticks: 1, sandbox_count: 1, workspace_count: 1 },
  errors: [{ reason_code: "sampling_late", count: 1, message: "The collector could not maintain its sampling interval." }],
  ...overrides,
});

const runtimeProjection = (artifact = runtimeArtifact(), retention = { state: "retained" }) => ({
  ...run,
  evidence_health: "complete",
  retention,
  cases: [{ test_id: "runtime.command", case_id: "default", title: "Runtime command", state: "passed", phases: { call: "passed" }, validations: { contract: "passed" }, cleanup: { sandbox: "passed" }, surfaces: [], evidence: [artifact] }],
  failures: [],
});

it("loads an accessible multi-scope runtime timeline lazily without a redundant log section", async () => {
  const runtimeLines = [
    { schema_version: 1, kind: "metadata", offset_ms: 0, run_id: run.run_id, test_id: "runtime.command", case_id: "default", attempt_id: "attempt-fixture", started_at: "2026-07-13T00:00:00Z", sample_interval_ms: 1000 },
    { schema_version: 1, kind: "sample", offset_ms: 0, phase: "setup", sandbox_id: "eos-fixture", scope: { kind: "sandbox", id: "sandbox" }, source: "docker_engine", metrics: { cpu_usec: 0, mem_cur: 268435456 } },
    { schema_version: 1, kind: "operation", offset_ms: 500, phase: "call", edge: "start", surface: "cli", operation: "runtime.command" },
    { schema_version: 1, kind: "sample", offset_ms: 1000, phase: "call", sandbox_id: "eos-fixture", scope: { kind: "sandbox", id: "sandbox" }, source: "docker_engine", metrics: { cpu_usec: 1250000, mem_cur: 536870912 }, derived: { cpu_cores: 1.25 } },
    { schema_version: 1, kind: "gap", offset_ms: 1500, reason_code: "sampling_late", message: "The collector could not maintain its sampling interval.", scope: { kind: "sandbox", id: "sandbox" } },
    { schema_version: 1, kind: "operation", offset_ms: 1700, phase: "call", edge: "marker", surface: "pytest", operation: "case_failure" },
    { schema_version: 1, kind: "sample", offset_ms: 2000, phase: "teardown", sandbox_id: "eos-fixture", scope: { kind: "sandbox", id: "sandbox" }, source: "docker_engine", metrics: { cpu_usec: 1600000, mem_cur: 402653184 }, derived: { cpu_cores: 0.35 } },
    { schema_version: 1, kind: "sample", offset_ms: 2100, phase: "teardown", sandbox_id: "eos-fixture", scope: { kind: "workspace", id: "ws-fixture" }, source: "sandbox_daemon", metrics: { disk_bytes: 2147483648, disk_allocated_bytes: 1073741824, files: 42, disk_truncated: true } },
    ...Array.from({ length: 200 }, (_, index) => ({ schema_version: 1, kind: "gap", offset_ms: 3000 + index, reason_code: "sampling_late", message: "The collector could not maintain its sampling interval.", scope: { kind: "sandbox", id: "sandbox" } })),
  ].map((record) => JSON.stringify(record)).join("\n") + "\n";
  server.use(
    http.get(`*/api/v1/runs/${run.run_id}`, () => HttpResponse.json({ schema_version: 1, data: runtimeProjection() })),
    http.get(`*/api/v1/runs/${run.run_id}/evidence/runtime-fixture`, ({ request }) => {
      evidenceFetches.push(request.url);
      return new HttpResponse(runtimeLines, { headers: { "Content-Type": "application/x-ndjson" } });
    }),
  );
  const user = userEvent.setup();
  renderRoom(`/e2e/runs/${run.run_id}`);
  const runtimePanel = (await screen.findByRole("heading", { name: "Runtime resources" })).closest("section");
  if (!runtimePanel) throw new Error("Runtime resources panel is missing.");
  const runtimeView = within(runtimePanel);
  expect(evidenceFetches).toHaveLength(0);
  expect(runtimeView.getByText("1.25 cores")).toBeTruthy();
  expect(runtimeView.getByText("0 s")).toBeTruthy();
  expect(runtimeView.getByText("512 MiB")).toBeTruthy();
  expect(runtimeView.getByText("Memory is unlimited for this scope; absence of a byte limit is not zero.")).toBeTruthy();
  expect(runtimeView.getByText(/Observed 3 of 4 expected ticks · 1 missed · 2 scopes/)).toBeTruthy();
  expect(runtimeView.getByText(/collector could not maintain its sampling interval/i)).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "Log records" })).toBeNull();

  const load = runtimeView.getByRole("button", { name: "Load timeline" });
  load.focus();
  await user.keyboard("{Enter}");
  const chart = await runtimeView.findByRole("img", { name: /Runtime resource timeline for Sandbox/ });
  expect(chart.getAttribute("tabindex")).toBe("0");
  expect(runtimePanel.querySelectorAll(".chart-gap")).toHaveLength(160);
  expect(evidenceFetches).toHaveLength(1);
  const svgFailureMarker = runtimeView.getByRole("img", { name: /call: case_failure marker/ });
  expect(svgFailureMarker.getAttribute("tabindex")).toBe("0");
  const failureMarker = runtimeView.getAllByText("call", { selector: ".runtime-markers strong" }).map((phase) => phase.closest("li")).find((row) => row?.textContent?.includes("case_failure"));
  expect(failureMarker?.textContent).toContain("call · case_failure · marker · 1.7 s");

  const scope = runtimeView.getByRole("combobox", { name: "Scope" });
  expect(within(scope).getByRole("option", { name: "All scopes" })).toBeTruthy();
  await user.selectOptions(scope, "sandbox:all:all");
  expect(await runtimeView.findByRole("img", { name: /Runtime resource timeline for All scopes/ })).toBeTruthy();
  scope.focus();
  await user.selectOptions(scope, "eos-fixture:workspace:ws-fixture");
  expect(runtimeView.getByText("2 GiB")).toBeTruthy();
  expect(runtimeView.getByText("42")).toBeTruthy();
  expect(runtimeView.getByText(/Workspace disk accounting was truncated/)).toBeTruthy();
  expect(await runtimeView.findByRole("img", { name: /Runtime resource timeline for Workspace/ })).toBeTruthy();

  await user.click(runtimeView.getByRole("button", { name: "Raw evidence" }));
  await screen.findByRole("heading", { name: "Artifact" });
  expect(evidenceFetches).toHaveLength(1);
});

it.each([
  ["not_applicable", "available", "This case did not own a sandbox, so runtime collection was not applicable."],
  ["unsupported", "unsupported", "The product observability boundary does not support runtime collection for this case."],
  ["unavailable", "unavailable", "Runtime collection ran, but no usable samples were available."],
  ["invalid", "invalid", "Runtime evidence is invalid and is not presented as resource truth."],
  ["partial", "partial", "Runtime evidence is partial; coverage and gaps remain explicit below."],
])("renders the %s runtime evidence state without fetching", async (status, availability, message) => {
  const artifact = runtimeArtifact({ status, availability, storage_ref: undefined, summary: { scopes: [] } });
  server.use(http.get(`*/api/v1/runs/${run.run_id}`, () => HttpResponse.json({ schema_version: 1, data: runtimeProjection(artifact) })));
  renderRoom(`/e2e/runs/${run.run_id}`);
  expect(await screen.findByText(message)).toBeTruthy();
  expect(evidenceFetches).toHaveLength(0);
});

it("shows invalid NDJSON and purged runtime evidence without inventing a chart", async () => {
  const user = userEvent.setup();
  server.use(
    http.get(`*/api/v1/runs/${run.run_id}`, () => HttpResponse.json({ schema_version: 1, data: runtimeProjection() })),
    http.get(`*/api/v1/runs/${run.run_id}/evidence/runtime-fixture`, ({ request }) => { evidenceFetches.push(request.url); return new HttpResponse("{not-json}\n", { headers: { "Content-Type": "application/x-ndjson" } }); }),
  );
  const rendered = renderRoom(`/e2e/runs/${run.run_id}`);
  await user.click(await screen.findByRole("button", { name: "Load timeline" }));
  expect(await screen.findByText("Runtime evidence contains an invalid line at 1.")).toBeTruthy();
  expect(screen.queryByRole("img", { name: /Runtime resource timeline/ })).toBeNull();
  rendered.unmount();

  server.use(http.get(`*/api/v1/runs/${run.run_id}`, () => HttpResponse.json({ schema_version: 1, data: runtimeProjection(runtimeArtifact(), { state: "purged" }) })));
  renderRoom(`/e2e/runs/${run.run_id}`);
  expect(await screen.findByText("Runtime evidence was purged; durable summary metadata remains.")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Load timeline" })).toBeNull();
  expect(evidenceFetches).toHaveLength(1);
});
