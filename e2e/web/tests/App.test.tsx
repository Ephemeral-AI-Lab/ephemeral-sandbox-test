import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import axe from "axe-core";
import { act, render, screen, waitFor } from "@testing-library/react";
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
