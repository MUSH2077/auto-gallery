"use client";

import React, { createContext, useCallback, useContext, useEffect, useState } from "react";

const TOKEN_KEY = "ag_token";

function setTokenCookie(token: string) {
  if (typeof document === "undefined") return;
  document.cookie = `${TOKEN_KEY}=${token}; path=/; SameSite=Lax; max-age=${7 * 24 * 60 * 60}`;
}

function clearTokenCookie() {
  if (typeof document === "undefined") return;
  document.cookie = `${TOKEN_KEY}=; path=/; max-age=0`;
}

export interface AuthUser {
  id: number;
  username: string;
  display_name: string | null;
  must_change_password: boolean;
}

interface AuthContextValue {
  user: AuthUser | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (username: string, password: string) => Promise<AuthUser>;
  updateAccessToken: (token: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  // Validate token on mount
  useEffect(() => {
    const stored = typeof window !== "undefined" ? localStorage.getItem(TOKEN_KEY) : null;
    if (!stored) {
      setIsLoading(false);
      return;
    }
    // Verify with backend
    fetch("/api/v1/auth/me", {
      headers: { Authorization: `Bearer ${stored}` },
    })
      .then((res) => {
        if (res.status === 403) {
          // Password change still required — synthesize user so AuthGuard can redirect
          setToken(stored);
          setUser({ id: 0, username: "admin", display_name: "Admin", must_change_password: true });
          setIsLoading(false);
          return Promise.reject("handled");
        }
        if (!res.ok) throw new Error("invalid");
        return res.json();
      })
      .then((data: AuthUser) => {
        setToken(stored);
        setUser(data);
      })
      .catch((err) => {
        if (err === "handled") return; // 403 already handled above
        localStorage.removeItem(TOKEN_KEY);
        clearTokenCookie();
      })
      .finally(() => setIsLoading(false));
  }, []);

  const login = useCallback(async (username: string, password: string): Promise<AuthUser> => {
    const res = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || "Login failed");
    }
    const data = await res.json();
    const accessToken: string = data.access_token;

    // Fetch user info
    const meRes = await fetch("/api/v1/auth/me", {
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    const me: AuthUser = await meRes.json();

    localStorage.setItem(TOKEN_KEY, accessToken);
    setTokenCookie(accessToken);
    setToken(accessToken);
    setUser(me);
    return me;
  }, []);

  const updateAccessToken = useCallback(async (nextToken: string) => {
    const meRes = await fetch("/api/v1/auth/me", {
      headers: { Authorization: `Bearer ${nextToken}` },
    });
    if (!meRes.ok && meRes.status !== 403) {
      throw new Error("Failed to refresh session");
    }
    const me: AuthUser = await meRes.json();
    localStorage.setItem(TOKEN_KEY, nextToken);
    setTokenCookie(nextToken);
    setToken(nextToken);
    setUser(me);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    clearTokenCookie();
    // Clean up batch import state so re-login doesn't recover stale jobs
    try { sessionStorage.removeItem("danbooru_batch_job"); } catch {}
    setToken(null);
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        user,
        token,
        isAuthenticated: !!user,
        isLoading,
        login,
        updateAccessToken,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
