/** Shared browser login flow for the public login page and AuthProvider. */

export class AuthUserLookupError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export interface AuthUser {
  id: number;
  username: string;
  display_name: string | null;
  must_change_password: boolean;
  preferences?: Record<string, unknown> | null;
}

export function clearLegacyToken(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem("ag_token");
  document.cookie = "ag_token=; path=/; max-age=0";
}

export function csrfToken(): string | null {
  if (typeof document === "undefined") return null;
  return document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith("ag_csrf="))?.slice(8) || null;
}

export async function logoutBrowser(): Promise<void> {
  const csrf = csrfToken();
  clearLegacyToken();
  const response = await fetch("/api/v1/auth/browser/logout", {
    method: "POST",
    credentials: "same-origin",
    headers: csrf ? { "X-CSRF-Token": csrf } : {},
  });
  if (!response.ok) throw new Error("Logout failed");
}

export async function loadUser(): Promise<AuthUser> {
  const response = await fetch("/api/v1/auth/me", { credentials: "same-origin" });
  if (!response.ok) {
    throw new AuthUserLookupError(
      response.status === 503 ? "Session service temporarily unavailable" : "Invalid or expired login",
      response.status,
    );
  }
  return response.json() as Promise<AuthUser>;
}

export async function loginAndLoadUser(
  username: string,
  password: string,
): Promise<AuthUser> {
  clearLegacyToken();
  const response = await fetch("/api/v1/auth/browser/login", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "Login failed");
  }
  // A failed lookup leaves the session cookie to be replaced by the next
  // successful login. Automatic logout could race with a newer login.
  return loadUser();
}
