import type { CatalogPage, EvidenceResponse, Health, Json, Preview, RunProjection, RunsPage, Workspaces } from "./types";

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

  async evidence(runId: string, evidenceId: string, stored: boolean): Promise<EvidenceResponse> {
    const response = await fetch(`/api/v1/runs/${encodeURIComponent(runId)}/evidence/${encodeURIComponent(evidenceId)}`, { credentials: "same-origin" });
    if (!response.ok) {
      let envelope: Envelope<never> | undefined;
      try {
        envelope = (await response.json()) as Envelope<never>;
      } catch {
        throw invalidResponse(response.status);
      }
      throw new ApiError(envelope.error ?? { code: "invalid_response", message: "The controller returned an invalid response.", retryable: false, request_id: "unknown" }, response.status);
    }
    const mediaType = response.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase() || "application/octet-stream";
    if (!stored) {
      let envelope: Envelope<{ run_id: string; evidence: Record<string, Json> }>;
      try {
        envelope = (await response.json()) as Envelope<{ run_id: string; evidence: Record<string, Json> }>;
      } catch {
        throw invalidResponse(response.status);
      }
      if (envelope.schema_version !== 1 || !envelope.data || !envelope.data.evidence) throw invalidResponse(response.status);
      return { kind: "record", runId: envelope.data.run_id, evidenceId, mediaType, record: envelope.data.evidence };
    }
    const bytes = await response.arrayBuffer();
    const isText = mediaType.startsWith("text/") || /(?:json|xml|yaml|javascript|x-sh)$/i.test(mediaType);
    return {
      kind: "content",
      runId,
      evidenceId,
      mediaType,
      text: isText ? new TextDecoder().decode(bytes) : undefined,
      retainedBytes: numberHeader(response.headers, "X-E2E-Evidence-Retained-Bytes"),
      omittedBytes: numberHeader(response.headers, "X-E2E-Evidence-Omitted-Bytes"),
      omittedLines: numberHeader(response.headers, "X-E2E-Evidence-Omitted-Lines"),
    };
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

function numberHeader(headers: Headers, name: string): number | undefined {
  const value = headers.get(name);
  if (value === null || value.trim() === "") return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined;
}

export const api = new ControlRoomClient();
