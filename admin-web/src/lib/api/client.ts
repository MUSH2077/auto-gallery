const BASE = "";

/** Public unified-search contract: backend accepts at most 100 rows per page. */
export const SEARCH_PAGE_SIZE = 100;

export type ApiErrorKind = "network" | "http" | "business";

export class ApiError extends Error {
  status: number;
  detail?: unknown;
  code?: string;
  kind: ApiErrorKind;
  body?: unknown;

  constructor(status: number, message: string, detail?: unknown, kind: ApiErrorKind = "http", body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.kind = kind;
    this.body = body;
    if (detail && typeof detail === "object" && "code" in detail && typeof detail.code === "string") {
      this.code = detail.code;
    }
  }
}

export function clearAuthOn401() {
  if (typeof window === "undefined") return;
  try {
    localStorage.removeItem("ag_token");
    sessionStorage.removeItem("danbooru_batch_job");
    document.cookie = "ag_token=; path=/; max-age=0";
  } catch {}
  // Redirect to login unless already on login page
  if (!window.location.pathname.startsWith("/admin/login")) {
    window.location.replace("/admin/login");
  }
}

async function apiErrorFromResponse(res: Response): Promise<ApiError> {
  const text = await res.text().catch(() => "");
  let body: unknown = text;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      // A plain response body remains useful error detail.
    }
  }
  const detail = body && typeof body === "object" && "detail" in body
    ? (body as { detail?: unknown }).detail
    : body || undefined;
  const message = typeof detail === "string"
    ? detail
    : detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string"
      ? detail.message
      : `${res.status} ${res.statusText}`;
  const kind: ApiErrorKind = detail && typeof detail === "object" && !Array.isArray(detail)
    ? "business"
    : "http";
  return new ApiError(res.status, message, detail, kind, body || undefined);
}

async function fetchApiResponse(path: string, options: RequestInit | undefined, jsonContentType: boolean): Promise<Response> {
  const headers = new Headers(options?.headers);
  if (jsonContentType && !headers.has("Content-Type") && !(options?.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  // Attach JWT token if present in localStorage
  if (typeof window !== "undefined") {
    const token = localStorage.getItem("ag_token");
    if (token && !headers.has("Authorization")) {
      headers.set("Authorization", `Bearer ${token}`);
    }
  }

  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      ...options,
      headers,
    });
  } catch (error) {
    if (error instanceof ApiError) throw error;
    const message = error instanceof TypeError
      ? "Network error"
      : error instanceof Error && error.message
        ? error.message
        : "Network error";
    throw new ApiError(0, message, error, "network");
  }
  if (!res.ok) {
    const error = await apiErrorFromResponse(res);
    // Global protected-route 401 handler: preserve the server rejection while
    // clearing auth state and redirecting. Auth endpoints avoid redirect loops.
    if (res.status === 401 && !path.startsWith("/api/v1/auth/")) clearAuthOn401();
    throw error;
  }
  return res;
}

export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetchApiResponse(path, options, true);
  if (res.status === 204) return undefined as T;
  return res.json();
}

export interface BlobResponse {
  blob: Blob;
  contentDisposition: string | null;
  contentType: string | null;
}

export async function requestBlob(path: string, options?: RequestInit): Promise<BlobResponse> {
  const res = await fetchApiResponse(path, options, false);
  return {
    blob: await res.blob(),
    contentDisposition: res.headers.get("Content-Disposition"),
    contentType: res.headers.get("Content-Type"),
  };
}

export async function assertBackupArchiveResponse(response: BlobResponse): Promise<BlobResponse> {
  const contentType = response.contentType?.split(";", 1)[0].trim().toLowerCase();
  if (contentType === "application/json" || contentType?.endsWith("+json")) {
    let message = "Backup download returned a diagnostic response";
    try {
      const body = JSON.parse(await response.blob.text()) as unknown;
      if (body && typeof body === "object" && "message" in body && typeof body.message === "string" && body.message.trim()) message = body.message;
      else if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string" && body.detail.trim()) message = body.detail;
    } catch {
      // Keep the safe fallback for malformed diagnostic JSON.
    }
    throw new ApiError(200, message, message, "business");
  }
  if (contentType !== "application/gzip") {
    const received = contentType || "missing";
    throw new ApiError(200, `Unexpected backup archive content type: ${received}`, received, "business");
  }
  return response;
}
