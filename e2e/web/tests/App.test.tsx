import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import axe from "axe-core";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter } from "react-router";
import { beforeEach, expect, it } from "vitest";
import { App } from "../src/App";
import { asyncStateFixture, catalog, catalogCases, health, run } from "./fixtures";
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
  FixtureEventSource.instances[0].emit("stream.gap");
  await screen.findByRole("heading", { name: "Reconnecting to live updates…" });
  await waitFor(() => expect(runFetches).toHaveLength(2));
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
