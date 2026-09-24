/** Shared browser login flow for the public login page and AuthProvider. */

const TOKEN_KEY = "ag_token";
const TOKEN_COOKIE_AGE_SECONDS = 7 * 24 * 60 * 60;

export interface AuthUser {
  id: number;
  username: string;
  display_name: string | null;
  must_change_password: boolean;
  preferences?: Record<string, unknown> | null;
}

export function storedToken(): string | null {
  return typeof window === "undefined" ? null : localStorage.getItem(TOKEN_KEY);
}

export function saveToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
  document.cookie = `${TOKEN_KEY}=${token}; path=/; SameSite=Lax; max-age=${TOKEN_COOKIE_AGE_SECONDS}`;
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
  document.cookie = `${TOKEN_KEY}=; path=/; max-age=0`;
}

export async function loadUser(token: string): Promise<AuthUser> {
  const response = await fetch("/api/v1/auth/me", {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) throw new Error("Invalid or expired login");
  return response.json() as Promise<AuthUser>;
}

export async function loginAndLoadUser(
  username: string,
  password: string,
): Promise<{ token: string; user: AuthUser }> {
  const response = await fetch("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "Login failed");
  }
  const body = await response.json();
  const token: string = body.access_token;
  const user = await loadUser(token);
  return { token, user };
}
