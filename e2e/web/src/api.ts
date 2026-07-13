import type { CatalogPage, Health, Preview, RunProjection, RunsPage, Workspaces } from "./types";

type Envelope<T> = { schema_version: 1; data?: T; error?: { code: string; message: string; retryable: boolean; request_id: string; field?: string } };

export class ApiError extends Error {
  constructor(public readonly response: NonNullable<Envelope<never>["error"]>, public readonly status: number) {
    super(response.message);
  }
}

export class ControlRoomClient {
  private nonce: string | null = null;

  async health(): Promise<Health> {
    const health = await this.request<Health>("/health");
    this.nonce = health.nonce;
    return health;
  }

  catalog(params: URLSearchParams): Promise<CatalogPage> {
    const suffix = params.size ? `?${params}` : "";
    return this.request(`/catalog${suffix}`);
  }

  runs(params = new URLSearchParams()): Promise<RunsPage> {
    const suffix = params.size ? `?${params}` : "";
    return this.request(`/runs${suffix}`);
  }

  run(runId: string): Promise<RunProjection> {
    return this.request(`/runs/${encodeURIComponent(runId)}`);
  }

  workspaces(): Promise<Workspaces> {
    return this.request("/workspaces");
  }

  refreshCatalog(): Promise<Record<string, unknown>> {
    return this.mutate("/catalog/refresh");
  }

  prepareTemplate(): Promise<Record<string, unknown>> {
    return this.mutate("/workspaces/template/prepare");
  }

  purgeWorkspace(workspaceId: string): Promise<Record<string, unknown>> {
    return this.mutate(`/workspaces/${encodeURIComponent(workspaceId)}/purge`);
  }

  cancelRun(runId: string): Promise<Record<string, unknown>> {
    return this.mutate(`/runs/${encodeURIComponent(runId)}/cancel`);
  }

  purgeRun(runId: string): Promise<Record<string, unknown>> {
    return this.mutate(`/runs/${encodeURIComponent(runId)}/purge`);
  }

  preview(selection: Record<string, unknown>): Promise<Preview> {
    return this.mutate("/previews", { selection });
  }

  admit(preview: Preview): Promise<{ run_id: string }> {
    if (!preview.admission_token) throw new Error("The reviewed run cannot be admitted without its controller token.");
    return this.mutate("/runs", { preview_id: preview.preview_id, admission_token: preview.admission_token, idempotency_key: crypto.randomUUID() });
  }

  private async mutate<T>(path: string, body?: Record<string, unknown>): Promise<T> {
    if (!this.nonce) await this.health();
    return this.request(path, {
      method: "POST",
      headers: { "X-E2E-Nonce": this.nonce ?? "" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const headers = new Headers(init?.headers);
    if (init?.body) headers.set("Content-Type", "application/json");
    const response = await fetch(`/api/v1${path}`, { ...init, headers, credentials: "same-origin" });
    let envelope: Envelope<T>;
    try {
      envelope = (await response.json()) as Envelope<T>;
    } catch {
      throw invalidResponse(response.status);
    }
    if (!envelope || envelope.schema_version !== 1 || !response.ok || envelope.error || envelope.data === undefined) {
      throw new ApiError(envelope.error ?? { code: "invalid_response", message: "The controller returned an invalid response.", retryable: false, request_id: "unknown" }, response.status);
    }
    return envelope.data;
  }
}

function invalidResponse(status: number): ApiError {
  return new ApiError({ code: "invalid_response", message: "The controller returned an invalid response.", retryable: false, request_id: "unknown" }, status);
}

export const api = new ControlRoomClient();
