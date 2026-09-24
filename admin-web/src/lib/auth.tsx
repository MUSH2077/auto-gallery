"use client";

import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { clearPrivateDiscoveryCache } from "@/lib/remoteDiscoveryPrivateCache";
import { clearToken, loadUser, loginAndLoadUser, saveToken, storedToken, type AuthUser } from "@/lib/authFlow";

const ME_QUERY_KEY = ["me"] as const;

export type { AuthUser } from "@/lib/authFlow";

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
  const queryClient = useQueryClient();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  // Validate token on mount
  useEffect(() => {
    const stored = storedToken();
    if (!stored) {
      setIsLoading(false);
      return;
    }
    // Verify with backend
    loadUser(stored)
      .then((data: AuthUser) => {
        queryClient.setQueryData(ME_QUERY_KEY, data);
        setToken(stored);
        setUser(data);
      })
      .catch(() => {
        queryClient.removeQueries({ queryKey: ME_QUERY_KEY, exact: true });
        clearToken();
      })
      .finally(() => setIsLoading(false));
  }, [queryClient]);

  const login = useCallback(async (username: string, password: string): Promise<AuthUser> => {
    const { token: accessToken, user: me } = await loginAndLoadUser(username, password);

    clearPrivateDiscoveryCache(queryClient);
    queryClient.setQueryData(ME_QUERY_KEY, me);
    saveToken(accessToken);
    setToken(accessToken);
    setUser(me);
    return me;
  }, [queryClient]);

  const updateAccessToken = useCallback(async (nextToken: string) => {
    const me = await loadUser(nextToken);
    if (user?.id !== me.id) clearPrivateDiscoveryCache(queryClient);
    queryClient.setQueryData(ME_QUERY_KEY, me);
    saveToken(nextToken);
    setToken(nextToken);
    setUser(me);
  }, [queryClient, user?.id]);

  const logout = useCallback(() => {
    clearPrivateDiscoveryCache(queryClient);
    queryClient.removeQueries({ queryKey: ME_QUERY_KEY, exact: true });
    clearToken();
    // Clean up batch import state so re-login doesn't recover stale jobs
    try { sessionStorage.removeItem("danbooru_batch_job"); } catch {}
    setToken(null);
    setUser(null);
  }, [queryClient]);

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
