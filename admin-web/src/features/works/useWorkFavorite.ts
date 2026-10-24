"use client";

import { useCallback } from "react";
import { useIsMutating, useMutation, useMutationState, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";

export const WORK_FAVORITE_MUTATION_KEY = ["works", "favorite"] as const;

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
  const mutationStates = useMutationState({ filters: { mutationKey: [...WORK_FAVORITE_MUTATION_KEY, workId], exact: true }, select: (current) => ({ status: current.state.status, error: current.state.error }) });
  const latestState = mutationStates[mutationStates.length - 1];
  const sharedError = latestState?.status === "error" ? latestState.error : undefined;
  const mutation = useMutation({
    mutationKey: [...WORK_FAVORITE_MUTATION_KEY, workId],
    mutationFn: () => api.toggleWorkFavorite(workId),
    onMutate: async () => {
      await qc.cancelQueries({ queryKey: queryKeys.works.all });
      const previous = currentWorkFavorite(qc, workId);
      const optimistic = previous === undefined ? undefined : !previous;
      if (optimistic !== undefined) updateWorkQueries(qc, workId, optimistic);
      return { optimistic };
    },
    onError: (_error, _variables, context) => {
      if (context?.optimistic !== undefined) updateWorkQueries(qc, workId, !context.optimistic);
    },
    onSuccess: (updated) => {
      updateWorkQueries(qc, workId, updated.is_favorite);
    },
    onSettled: () => {
      if (qc.isMutating({ mutationKey: WORK_FAVORITE_MUTATION_KEY }) <= 1) {
        void qc.invalidateQueries({ queryKey: queryKeys.works.all });
      }
    },
  });
  const toggle = useCallback(() => {
    if (pendingCount > 0 || mutation.isPending || qc.isMutating({ mutationKey: [...WORK_FAVORITE_MUTATION_KEY, workId] }) > 0) return;
    mutation.mutate();
  }, [mutation, pendingCount, qc, workId]);
  const favoriteError = mutationStates.length ? sharedError : mutation.error;
  return { ...mutation, error: favoriteError, isError: Boolean(favoriteError), isPending: pendingCount > 0, toggle };
}
