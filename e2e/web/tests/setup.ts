import { afterAll, afterEach, beforeAll, vi } from "vitest";
import { cleanup } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { setupServer } from "msw/node";
import { catalog, envelope, health, preview, run, runs, workspaces } from "./fixtures";

export const refreshBodies: string[] = [];
export const templateBodies: string[] = [];
export const runFetches: string[] = [];
export const evidenceFetches: string[] = [];
export const admissionBodies: string[] = [];
export const healthFetches: string[] = [];
export let catalogFixture = catalog;

export const server = setupServer(
  http.get("*/api/v1/health", ({ request }) => {
    healthFetches.push(request.url);
    return HttpResponse.json(envelope(health));
  }),
  http.get("*/api/v1/catalog", () => HttpResponse.json(envelope(catalogFixture))),
  http.post("*/api/v1/catalog/refresh", async ({ request }) => {
    refreshBodies.push(await request.text());
    return HttpResponse.json(envelope({ state: "requested", coalesced: true }));
  }),
  http.post("*/api/v1/previews", () => HttpResponse.json(envelope(preview))),
  http.post("*/api/v1/runs", async ({ request }) => {
    admissionBodies.push(await request.text());
    return HttpResponse.json(envelope({ run_id: run.run_id }));
  }),
  http.get("*/api/v1/runs", () => HttpResponse.json(envelope(runs))),
  http.get(`*/api/v1/runs/${run.run_id}`, ({ request }) => {
    runFetches.push(request.url);
    return HttpResponse.json(envelope(run));
  }),
  http.get(`*/api/v1/runs/${run.run_id}/evidence/log-runtime-file`, ({ request }) => {
    evidenceFetches.push(request.url);
    return new HttpResponse("first bounded log line\nsecond bounded log line\n", { headers: { "Content-Type": "text/plain; charset=utf-8", "X-E2E-Evidence-Retained-Bytes": "1024", "X-E2E-Evidence-Omitted-Bytes": "2048", "X-E2E-Evidence-Omitted-Lines": "16", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox" } });
  }),
  http.post(`*/api/v1/runs/${run.run_id}/cancel`, () => HttpResponse.json(envelope({ run_id: run.run_id, cancellation_seq: 43 }))),
  http.post(`*/api/v1/runs/${run.run_id}/purge`, () => HttpResponse.json(envelope({ run_id: run.run_id, state: "purged" }))),
  http.get("*/api/v1/workspaces", () => HttpResponse.json(envelope(workspaces))),
  http.post("*/api/v1/workspaces/template/prepare", async ({ request }) => {
    templateBodies.push(await request.text());
    return HttpResponse.json(envelope({ state: "requested" }));
  }),
);

export class FixtureEventSource {
  static instances: FixtureEventSource[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  private listeners = new Map<string, Array<(event: MessageEvent) => void>>();
  constructor(readonly url: string) { FixtureEventSource.instances.push(this); }
  addEventListener = vi.fn((type: string, listener: (event: MessageEvent) => void) => {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  });
  close = vi.fn();
  emit(type: string, lastEventId = type === "stream.gap" ? "" : "43") {
    const event = new MessageEvent(type, { lastEventId });
    if (type === "message") this.onmessage?.(event);
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
  fail() { this.onerror?.(new Event("error")); }
}

Object.defineProperty(globalThis, "EventSource", { value: FixtureEventSource, writable: true });
Object.defineProperty(window, "matchMedia", { value: (query: string) => ({ matches: false, media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }), writable: true });

beforeAll(() => {
  document.documentElement.lang = "en";
  document.title = "E2E Control Room";
  server.listen({ onUnhandledRequest: "error" });
});
afterEach(() => {
  cleanup();
  server.resetHandlers();
  refreshBodies.length = 0;
  templateBodies.length = 0;
  runFetches.length = 0;
  evidenceFetches.length = 0;
  admissionBodies.length = 0;
  healthFetches.length = 0;
  catalogFixture = catalog;
  FixtureEventSource.instances.length = 0;
});
afterAll(() => server.close());
