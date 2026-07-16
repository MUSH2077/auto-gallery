"use client";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";

/**
 * Single source of truth for module-permission checks on the client.
 *
 * Wraps the same `queryKeys.me` query AdminNav already uses (TanStack Query
 * dedupes concurrent callers, so this does not trigger an extra request).
 * Admins implicitly have every module; non-admins are gated by
 * `me.permissions` (a subset of the module keys in `me.modules`).
 */
export function usePermissions() {
  const me = useQuery({ queryKey: queryKeys.me, queryFn: api.getMe });

  const isAdmin = !!me.data?.is_admin;
  const permissions = me.data?.permissions || [];

  function has(module: string): boolean {
    if (isAdmin) return true;
    return permissions.includes(module);
  }

  return { isAdmin, has, isLoading: me.isLoading };
}
