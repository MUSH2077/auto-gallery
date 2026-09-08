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

export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers);
  if (!headers.has("Content-Type") && !(options?.body instanceof FormData)) {
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
  if (res.status === 204) return undefined as T;
  if (!res.ok) {
    const error = await apiErrorFromResponse(res);
    // Global protected-route 401 handler: preserve the server rejection while
    // clearing auth state and redirecting. Auth endpoints avoid redirect loops.
    if (res.status === 401 && !path.startsWith("/api/v1/auth/")) clearAuthOn401();
    throw error;
  }
  return res.json();
}
