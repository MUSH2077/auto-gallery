"use client";

import { useCallback } from "react";
import { useIsMutating, useMutation, useMutationState, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";

export const WORK_FAVORITE_MUTATION_KEY = ["works", "favorite"] as const;

const confirmedFavorites = new WeakMap<QueryClient, Map<string, boolean>>();

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function updateItems(items: unknown, id: string, isFavorite: boolean): { value: unknown; changed: boolean } {
  if (!Array.isArray(items)) return { value: items, changed: false };
  let changed = false;
  const next = items.map((item) => {
    if (!isRecord(item) || item.id !== id || !("is_favorite" in item)) return item;
    changed = true;
    return { ...item, is_favorite: isFavorite };
  });
  return { value: changed ? next : items, changed };
}

export function updateWorkFavoriteCache(data: unknown, id: string, isFavorite: boolean): unknown {
  if (!isRecord(data)) return data;
  if (data.id === id && "is_favorite" in data) return { ...data, is_favorite: isFavorite };
  let changed = false;
  const next: Record<string, unknown> = { ...data };
  const groups = data.groups;
  if (isRecord(groups) && isRecord(groups.works)) {
    const result = updateItems(groups.works.items, id, isFavorite);
    if (result.changed) {
      changed = true;
      next.groups = { ...groups, works: { ...groups.works, items: result.value } };
    }
  }
  const results = updateItems(data.results, id, isFavorite);
  if (results.changed) {
    changed = true;
    next.results = results.value;
  }
  return changed ? next : data;
}

export function findWorkFavorite(data: unknown, id: string): boolean | undefined {
  if (!isRecord(data)) return undefined;
  if (data.id === id && typeof data.is_favorite === "boolean") return data.is_favorite;
  if (isRecord(data.groups) && isRecord(data.groups.works)) {
    const item = Array.isArray(data.groups.works.items)
      ? data.groups.works.items.find((entry) => isRecord(entry) && entry.id === id)
      : undefined;
    if (isRecord(item) && typeof item.is_favorite === "boolean") return item.is_favorite;
  }
  if (Array.isArray(data.results)) {
    const item = data.results.find((entry) => isRecord(entry) && entry.id === id);
    if (isRecord(item) && typeof item.is_favorite === "boolean") return item.is_favorite;
  }
  return undefined;
}

function updateWorkQueries(qc: ReturnType<typeof useQueryClient>, id: string, isFavorite: boolean) {
  for (const [queryKey, data] of qc.getQueriesData({ queryKey: queryKeys.works.all })) {
    const next = updateWorkFavoriteCache(data, id, isFavorite);
    if (next !== data) qc.setQueryData(queryKey, next);
  }
}

function currentWorkFavorite(qc: ReturnType<typeof useQueryClient>, id: string): boolean | undefined {
  for (const [, data] of qc.getQueriesData({ queryKey: queryKeys.works.all })) {
    const value = findWorkFavorite(data, id);
    if (value !== undefined) return value;
  }
  return undefined;
}

export function useWorkFavorite(workId: string) {
  const qc = useQueryClient();
  const pendingCount = useIsMutating({ mutationKey: [...WORK_FAVORITE_MUTATION_KEY, workId] });
  const mutationStates = useMutationState({ filters: { mutationKey: [...WORK_FAVORITE_MUTATION_KEY, workId], exact: true }, select: (current) => ({ status: current.state.status, error: current.state.error, variables: current.state.variables as { desired: boolean } | undefined }) });
  const latestState = mutationStates[mutationStates.length - 1];
  const sharedError = latestState?.status === "error" ? latestState.error : undefined;
  const key = [...WORK_FAVORITE_MUTATION_KEY, workId] as const;
  const mutation = useMutation({
    mutationKey: key,
    scope: { id: `work-favorite:${workId}` },
    mutationFn: async ({ desired }: { desired: boolean; previous: boolean }) => {
      const confirmed = confirmedFavorites.get(qc)!;
      let current = confirmed.get(workId);
      if (current === undefined) {
        current = (await api.getWork(workId)).is_favorite;
        confirmed.set(workId, current);
      }
      if (current === desired) return { is_favorite: current };
      try {
        const updated = await api.toggleWorkFavorite(workId);
        confirmed.set(workId, updated.is_favorite);
        return updated;
      } catch (error) {
        confirmed.delete(workId);
        try { confirmed.set(workId, (await api.getWork(workId)).is_favorite); } catch { /* Keep the outcome unknown. */ }
        throw error;
      }
    },
    onError: (_error, variables) => {
      if (qc.isMutating({ mutationKey: key, exact: true }) <= 1) {
        updateWorkQueries(qc, workId, confirmedFavorites.get(qc)?.get(workId) ?? variables.previous);
      }
    },
    onSuccess: (updated) => {
      if (qc.isMutating({ mutationKey: key, exact: true }) <= 1) updateWorkQueries(qc, workId, updated.is_favorite);
    },
    onSettled: (_data, error) => {
      if (qc.isMutating({ mutationKey: key, exact: true }) <= 1) confirmedFavorites.get(qc)?.delete(workId);
      if (qc.isMutating({ mutationKey: key, exact: true }) <= 1) void qc.invalidateQueries({ queryKey: queryKeys.curation.subject("work", workId) });
      if (qc.isMutating({ mutationKey: WORK_FAVORITE_MUTATION_KEY }) <= 1) {
        void qc.invalidateQueries({ queryKey: queryKeys.works.all, predicate: query => {
          const [,, , search] = query.queryKey;
          return query.queryKey[1] === "compound-search" && typeof search === "string" && (Boolean(error) || /(?:^|\s)-?is:favorite(?:\s|$)|(?:^|\s)sort:(?:heat|updated)(?:-|\s|$)/i.test(search));
        } });
      }
    },
  });
  const toggle = useCallback(() => {
    const pending = qc.getMutationCache().findAll({ mutationKey: key, exact: true, status: "pending" });
    const previous = (pending.at(-1)?.state.variables as { desired: boolean } | undefined)?.desired ?? currentWorkFavorite(qc, workId);
    if (previous === undefined) return;
    let confirmed = confirmedFavorites.get(qc);
    if (!confirmed) { confirmed = new Map(); confirmedFavorites.set(qc, confirmed); }
    if (!confirmed.has(workId) && !qc.isMutating({ mutationKey: key, exact: true })) confirmed.set(workId, previous);
    void qc.cancelQueries({ queryKey: queryKeys.works.all, predicate: ({ queryKey, state }) =>
      queryKey[1] === "compound-search" || (queryKey.length === 2 && queryKey[1] === workId)
      || findWorkFavorite(state.data, workId) !== undefined }, { revert: false });
    const desired = !previous;
    updateWorkQueries(qc, workId, desired);
    mutation.mutate({ desired, previous });
  }, [mutation, qc, workId]);
  const favoriteError = mutationStates.length ? sharedError : mutation.error;
  const optimistic = mutationStates.filter(state => state.status === "pending").at(-1)?.variables?.desired;
  return { ...mutation, error: favoriteError, isError: Boolean(favoriteError), isPending: pendingCount > 0, optimistic, toggle };
}
