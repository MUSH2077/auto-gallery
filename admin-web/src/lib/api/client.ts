const BASE = "";

export class ApiError extends Error {
  status: number;
  detail?: unknown;
  code?: string;

  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
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

  const res = await fetch(`${BASE}${path}`, {
    headers,
    ...options,
  });
  // Global 401 handler: clear auth state and redirect to login
  // (skips auth endpoints to avoid redirect loops during login)
  if (res.status === 401 && !path.startsWith("/api/v1/auth/")) {
    clearAuthOn401();
    throw new ApiError(401, "Session expired — redirecting to login");
  }
  if (res.status === 204) return undefined as T;
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = body.detail;
    const message = typeof detail === "string"
      ? detail
      : detail && typeof detail === "object" && typeof detail.message === "string"
        ? detail.message
        : `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, message, detail);
  }
  return res.json();
}
