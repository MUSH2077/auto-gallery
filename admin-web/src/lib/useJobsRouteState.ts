"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { SEARCH_PAGE_SIZE } from "@/lib/api";
import { parseJobsRoute, updatedJobsHref } from "@/lib/jobsRoute";

export function useJobsRouteState() {
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();
  const state = parseJobsRoute(new URLSearchParams(sp.toString()), SEARCH_PAGE_SIZE);

  const updateParams = (updates: Record<string, string | null>, replace = true) => {
    const href = updatedJobsHref(pathname, new URLSearchParams(sp.toString()), updates);
    if (replace) router.replace(href, { scroll: false }); else router.push(href, { scroll: false });
  };
  const openTaskDetail = (id: string) => updateParams({ task: id, job: null, import_job: null }, false);
  const openDownloadDetail = (id: string) => updateParams({ tab: "downloads", job: id, import_job: null, task: null }, false);
  const openImportDetail = (id: string) => updateParams({ tab: "imports", import_job: id, job: null, task: null }, false);
  const closeDetail = () => updateParams({ job: null, import_job: null, task: null });

  return { ...state, updateParams, openTaskDetail, openDownloadDetail, openImportDetail, closeDetail };
}
