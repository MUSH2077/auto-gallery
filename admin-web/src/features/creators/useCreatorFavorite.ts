"use client";

import { useMutation, useMutationState, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useToast } from "@/components/Toast";

const confirmedFavorites = new WeakMap<QueryClient, Map<string, boolean>>();

function updateCreatorQueries(qc: QueryClient, id: string, favorite: boolean) {
  for (const [key, data] of qc.getQueriesData({ queryKey: queryKeys.creators.all })) {
    if (!data || typeof data !== "object") continue;
    const value = data as Record<string, unknown>;
    if (value.id === id && typeof value.is_favorite === "boolean") {
      qc.setQueryData(key, { ...value, is_favorite: favorite });
    } else if (Array.isArray(value.items) && value.items.some(item => item?.id === id)) {
      qc.setQueryData(key, { ...value, items: value.items.map(item => item?.id === id ? { ...item, is_favorite: favorite } : item) });
    }
  }
}

function currentFavorite(qc: QueryClient, id: string): boolean | undefined {
  for (const [, data] of qc.getQueriesData({ queryKey: queryKeys.creators.all })) {
    if (!data || typeof data !== "object") continue;
    const value = data as Record<string, unknown>;
    const item = value.id === id ? value : Array.isArray(value.items) ? value.items.find(item => item?.id === id) : undefined;
    if (item && typeof item.is_favorite === "boolean") return item.is_favorite;
  }
}

export function useCreatorFavorite(id: string, initialFavorite: boolean | undefined) {
  const qc = useQueryClient();
  const toast = useToast();
  const key = ["creators", "favorite", id] as const;
  const pending = useMutationState({
    filters: { mutationKey: key, exact: true, status: "pending" },
    select: state => state.state.variables as { desired: boolean } | undefined,
  });
  const mutation = useMutation({
    mutationKey: key,
    scope: { id: `creator-favorite:${id}` },
    mutationFn: async ({ desired }: { desired: boolean; previous: boolean }) => {
      const confirmed = confirmedFavorites.get(qc)!;
      let current = confirmed.get(id);
      if (current === undefined) { current = (await api.getCreator(id)).is_favorite; confirmed.set(id, current); }
      if (current === desired) return { is_favorite: current };
      try {
        const updated = await api.toggleCreatorFavorite(id);
        confirmed.set(id, updated.is_favorite);
        return updated;
      } catch (error) {
        confirmed.delete(id);
        try { confirmed.set(id, (await api.getCreator(id)).is_favorite); } catch { /* Keep the outcome unknown. */ }
        throw error;
      }
    },
    onError: (error, variables) => {
      if (qc.isMutating({ mutationKey: key, exact: true }) <= 1) updateCreatorQueries(qc, id, confirmedFavorites.get(qc)?.get(id) ?? variables.previous);
      toast.error(error instanceof Error ? error.message : String(error));
    },
    onSuccess: updated => {
      if (qc.isMutating({ mutationKey: key, exact: true }) <= 1) updateCreatorQueries(qc, id, updated.is_favorite);
    },
    onSettled: (_data, error) => {
      if (qc.isMutating({ mutationKey: key, exact: true }) <= 1) {
        confirmedFavorites.get(qc)?.delete(id);
        if (qc.isMutating({ mutationKey: ["creators", "favorite"] }) <= 1) {
          void qc.invalidateQueries({ queryKey: queryKeys.creators.all, predicate: query => {
            const search = query.queryKey[2];
            return query.queryKey[1] === "virtual" && typeof search === "string" && (Boolean(error) || /(?:^|\s)-?is:favorite(?:\s|$)|(?:^|\s)sort:(?:heat|updated)(?:-|\s|$)/i.test(search));
          } });
        }
      }
    },
  });
  return { isFavorite: pending.at(-1)?.desired ?? initialFavorite, toggle: () => {
    const queued = qc.getMutationCache().findAll({ mutationKey: key, exact: true, status: "pending" });
    const previous = (queued.at(-1)?.state.variables as { desired: boolean } | undefined)?.desired ?? currentFavorite(qc, id);
    if (previous === undefined) return;
    let confirmed = confirmedFavorites.get(qc);
    if (!confirmed) { confirmed = new Map(); confirmedFavorites.set(qc, confirmed); }
    if (!confirmed.has(id) && !qc.isMutating({ mutationKey: key, exact: true })) confirmed.set(id, previous);
    void qc.cancelQueries({ queryKey: queryKeys.creators.all }, { revert: false });
    updateCreatorQueries(qc, id, !previous);
    mutation.mutate({ desired: !previous, previous });
  } };
}
