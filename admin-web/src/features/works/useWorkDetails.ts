"use client";
import {useQuery,useMutation,useQueryClient} from '@tanstack/react-query';
import {useRouter} from 'next/navigation';
import {api,queryKeys} from '@/lib/api';
import {useT} from '@/lib/i18n';
import {useI18nFormat} from '@/lib/i18n-format';
import {usePermissions} from '@/lib/usePermissions';
import { useWorkFavorite } from '@/features/works/useWorkFavorite';
export interface WorkSourceData { id:string; source:string; source_work_id:string; source_creator_id:string; source_url?:string; title?:string; description?:string; posted_at?:string; raw_metadata?:Record<string,unknown>; }
export function useWorkDetails(id:string) {
  const t = useT();
  const fmt = useI18nFormat();
  const router = useRouter();

  const qc = useQueryClient();
  const { has } = usePermissions();
  const canCurate = has("curation");

  const work = useQuery({ queryKey: queryKeys.works.detail(id), queryFn: () => api.getWork(id) });
  const toggleFavorite = useWorkFavorite(id);
  const sources = useQuery({ queryKey: queryKeys.works.sources(id), queryFn: () => api.getWorkSources(id) });
  const hasPixivSource = ((sources.data || []) as WorkSourceData[]).some((source) => source.source === "pixiv");
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
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.works.detail(id) });
      qc.invalidateQueries({ queryKey: queryKeys.curation.all });
      qc.invalidateQueries({ queryKey: queryKeys.works.all });
    },
  });


return {t,fmt,router,qc,has,canCurate,work,toggleFavorite,sources,hasPixivSource,remoteState,workTags,curateWork};
}
