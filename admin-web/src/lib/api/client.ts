import { csrfToken } from "../authFlow.ts";

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

function errorMessage(value: unknown): string | undefined {
  if (typeof value === "string" && value.trim()) return value;
  if (Array.isArray(value)) {
    const messages = value.map(errorMessage).filter((message): message is string => Boolean(message));
    return messages.length ? messages.join("; ") : undefined;
  }
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (typeof record.message === "string" && record.message.trim()) return record.message;
    if (typeof record.msg === "string" && record.msg.trim()) return record.msg;
    if (record.detail !== undefined) return errorMessage(record.detail);
  }
  return undefined;
}

/** Normalize fetch and XHR error payloads into the same public ApiError shape. */
export function apiErrorFromData(
  status: number,
  statusText: string,
  body: unknown,
  messageOverride?: string,
  kindOverride?: ApiErrorKind,
): ApiError {
  let parsedBody = body;
  if (typeof body === "string" && body.trim()) {
    try { parsedBody = JSON.parse(body); } catch { /* Keep the plain response body. */ }
  }
  const rawBody = parsedBody === "" || parsedBody == null ? undefined : parsedBody;
  const detail = rawBody && typeof rawBody === "object" && "detail" in rawBody
    ? (rawBody as { detail?: unknown }).detail
    : rawBody;
  const message = messageOverride || errorMessage(detail) || errorMessage(rawBody) || `${status} ${statusText}`.trim();
  const structured = detail !== undefined && detail !== null && typeof detail === "object";
  const kind = kindOverride || (structured ? "business" : "http");
  const error = new ApiError(status, message, detail, kind, rawBody);
  const codeSource = detail && typeof detail === "object" && !Array.isArray(detail)
    ? detail
    : rawBody && typeof rawBody === "object" && !Array.isArray(rawBody) ? rawBody : undefined;
  if (codeSource && typeof (codeSource as Record<string, unknown>).code === "string") {
    error.code = (codeSource as Record<string, unknown>).code as string;
  }
  return error;
}

export function clearAuthOn401(requestSessionMarker: string | null) {
  if (typeof window === "undefined" || csrfToken() !== requestSessionMarker) return;
  try { sessionStorage.removeItem("danbooru_batch_job"); } catch {}
  if (window.location.pathname.startsWith("/admin/login")) return;
  const destination = window.location.pathname + window.location.search + window.location.hash;
  window.location.replace("/admin/login?redirect=" + encodeURIComponent(destination));
}

export function uploadWithProgress<T>(
  form: FormData,
  path: string,
  onProgress?: (pct: number) => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.timeout = 10 * 60 * 1000;
    const csrf = csrfToken();
    if (csrf) xhr.setRequestHeader("X-CSRF-Token", csrf);
    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
      };
    }
    xhr.ontimeout = () => reject(apiErrorFromData(0, "", undefined, "Upload timed out", "network"));
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        let body: unknown = {};
        try { body = JSON.parse(xhr.responseText); } catch { /* empty/non-JSON success body */ }
        resolve(body as T);
        return;
      }
      const error = apiErrorFromData(xhr.status, xhr.statusText, xhr.responseText);
      if (xhr.status === 401) clearAuthOn401(csrf);
      reject(error);
    };
    xhr.onerror = () => reject(apiErrorFromData(0, "", undefined, "Network error", "network"));
    xhr.send(form);
  });
}


async function apiErrorFromResponse(res: Response): Promise<ApiError> {
  const text = await res.text().catch(() => "");
  return apiErrorFromData(res.status, res.statusText, text);
}

async function fetchApiResponse(path: string, options: RequestInit | undefined, jsonContentType: boolean): Promise<Response> {
  const headers = new Headers(options?.headers);
  if (jsonContentType && !headers.has("Content-Type") && !(options?.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (typeof window !== "undefined" && !["GET", "HEAD", "OPTIONS"].includes((options?.method || "GET").toUpperCase())) {
    const csrf = csrfToken();
    if (csrf) headers.set("X-CSRF-Token", csrf);
  }

  const requestSessionMarker = typeof window === "undefined" ? null : csrfToken();
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      ...options,
      headers,
      credentials: "same-origin",
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
    if (res.status === 401 && !path.startsWith("/api/v1/auth/")) clearAuthOn401(requestSessionMarker);
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
