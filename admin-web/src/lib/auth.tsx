"use client";

import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { clearPrivateDiscoveryCache } from "@/lib/remoteDiscoveryPrivateCache";
import { clearLegacyToken, loadUser, loginAndLoadUser, logoutBrowser, type AuthUser } from "@/lib/authFlow";

const ME_QUERY_KEY = ["me"] as const;

export type { AuthUser } from "@/lib/authFlow";

interface AuthContextValue {
  user: AuthUser | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (username: string, password: string) => Promise<AuthUser>;
  refreshUser: () => Promise<void>;
  authUnavailable: boolean;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [authUnavailable, setAuthUnavailable] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const loginGeneration = useRef(0);

  useEffect(() => {
    const generation = loginGeneration.current;
    clearLegacyToken();
    loadUser()
      .then((data) => {
        if (generation !== loginGeneration.current) return;
        queryClient.setQueryData(ME_QUERY_KEY, data);
        setUser(data);
        setAuthUnavailable(false);
      })
      .catch((error: Error & { status?: number }) => {
        if (generation !== loginGeneration.current) return;
        if (error.status === 503) {
          setAuthUnavailable(true);
          return;
        }
        queryClient.removeQueries({ queryKey: ME_QUERY_KEY, exact: true });
      })
      .finally(() => {
        if (generation === loginGeneration.current) setIsLoading(false);
      });
  }, [queryClient]);

  const login = useCallback(async (username: string, password: string): Promise<AuthUser> => {
    loginGeneration.current += 1;
    const me = await loginAndLoadUser(username, password);

    clearPrivateDiscoveryCache(queryClient);
    queryClient.setQueryData(ME_QUERY_KEY, me);
    setUser(me);
    setAuthUnavailable(false);
    setIsLoading(false);
    return me;
  }, [queryClient]);

  const refreshUser = useCallback(async () => {
    const me = await loadUser();
    if (user?.id !== me.id) clearPrivateDiscoveryCache(queryClient);
    queryClient.setQueryData(ME_QUERY_KEY, me);
    setUser(me);
    setAuthUnavailable(false);
  }, [queryClient, user?.id]);

  const logout = useCallback(async () => {
    try {
      await logoutBrowser();
    } catch {
      setAuthUnavailable(true);
      return;
    }
    clearPrivateDiscoveryCache(queryClient);
    queryClient.removeQueries({ queryKey: ME_QUERY_KEY, exact: true });
    try { sessionStorage.removeItem("danbooru_batch_job"); } catch {}
    setUser(null);
    setAuthUnavailable(false);
  }, [queryClient]);

  return (
    <AuthContext.Provider
      value={{
        user,
        isAuthenticated: !!user,
        isLoading,
        login,
        refreshUser,
        authUnavailable,
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
