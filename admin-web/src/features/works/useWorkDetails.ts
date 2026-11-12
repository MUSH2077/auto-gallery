"use client";
import {useQuery,useMutation,useQueryClient} from '@tanstack/react-query';
import {api,queryKeys} from '@/lib/api';
import {useT} from '@/lib/i18n';
import {usePermissions} from '@/lib/usePermissions';
import { refreshWorkVisibility } from "./refreshWorkVisibility";
import { useWorkFavorite } from '@/features/works/useWorkFavorite';
export function useWorkDetails(id:string) {
  const t = useT();

  const qc = useQueryClient();
  const { has } = usePermissions();
  const canCurate = has("curation");

  const work = useQuery({ queryKey: queryKeys.works.detail(id), queryFn: () => api.getWork(id) });
  const toggleFavorite = useWorkFavorite(id);
  const sources = useQuery({ queryKey: queryKeys.works.sources(id), queryFn: () => api.getWorkSources(id) });
  const hasPixivSource = (sources.data || []).some((source) => source.source === "pixiv");
  const remoteState = useQuery({
    queryKey: queryKeys.works.remoteState(id),
    queryFn: () => api.getWorkRemoteState(id),
    enabled: hasPixivSource,
    staleTime: 0,
    gcTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    retry: false,
  });
  const workTags = useQuery({ queryKey: ["works", id, "tags"], queryFn: () => api.getWorkTags(id) });
  const curateWork = useMutation({
    mutationKey: ["works", "curation", id],
    mutationFn: (action: "trash" | "restore") => api.batchCurateWorks([id], action),
    onMutate: (action) => {
      void qc.cancelQueries({ queryKey: queryKeys.works.detail(id), exact: true }, { revert: false });
      const previous = qc.getQueryData<typeof work.data>(queryKeys.works.detail(id));
      qc.setQueryData<typeof work.data>(queryKeys.works.detail(id), current => current ? {
        ...current, curation_state: { ...current.curation_state, visibility: action === "trash" ? "trashed" : "visible" },
      } : current);
      return { curationState: previous?.curation_state };
    },
    onError: (_error, _action, context) => {
      qc.setQueryData<typeof work.data>(queryKeys.works.detail(id), current => current ? { ...current, curation_state: context?.curationState } : current);
    },
    onSettled: async () => {
      await refreshWorkVisibility(qc, [id]);
    },
  });


return {t,qc,has,canCurate,work,toggleFavorite,sources,hasPixivSource,remoteState,workTags,curateWork};
}
