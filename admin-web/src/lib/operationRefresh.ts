import type { QueryClient } from "@tanstack/react-query";
import { queryKeys } from "@/lib/api";

export type OperationRefreshMeta = Record<string, unknown> | null | undefined;

/** Resource-owned cache refresh policy for completed admin operations. */
export function refreshOperationResources(
  qc: QueryClient,
  kind: string,
  meta?: OperationRefreshMeta,
) {
  const refreshes = new Map<string, { queryKey: readonly unknown[]; refetchType: "active" | "all" }>();
  const invalidate = (queryKey: readonly unknown[], refetchType: "active" | "all" = "active") => {
    refreshes.set(JSON.stringify(queryKey), { queryKey, refetchType });
  };
  const refetchCreatorSubscriptions = () => {
    invalidate(queryKeys.creators.all, "all");
    invalidate(queryKeys.subscriptions.all, "all");
  };

  if (kind === "danbooru-import-all") {
    refetchCreatorSubscriptions();
    invalidate(queryKeys.workbench, "all");
    invalidate(["creator-subscription-overview"], "all");
  } else if (kind === "danbooru-mapping-refresh") {
    refetchCreatorSubscriptions();
    [queryKeys.workbench, ["repositories"], ["creator-subscription-overview"], queryKeys.tasks.all].forEach(key => invalidate(key));
  } else if (kind === "hierarchy-delete") {
    refetchCreatorSubscriptions();
    [queryKeys.works.all, queryKeys.curation.all, queryKeys.downloadJobs.all, queryKeys.importJobs.all,
      queryKeys.tasks.all, queryKeys.workbench, ["repositories"], ["creator-subscription-overview"], queryKeys.system.storageBreakdown].forEach(key => invalidate(key));
  } else {
    invalidate(queryKeys.tasks.all);
    if (kind === "admin-backup-create") {
      invalidate(queryKeys.backups.all);
    } else if (kind === "admin-cleanup-metadata-jsons" || kind === "admin-creator-reenrich") {
      [queryKeys.system.storageBreakdown, queryKeys.system.systemInfo].forEach(key => invalidate(key));
    } else if (kind === "admin-rebuild" || kind === "admin-disk-import") {
      [queryKeys.workbench, queryKeys.works.all, queryKeys.downloadJobs.all, queryKeys.importJobs.all,
        queryKeys.system.storageBreakdown, queryKeys.system.systemInfo].forEach(key => invalidate(key));
    } else {
      const entity = typeof meta?.entity === "string" ? meta.entity : undefined;
      if (kind === "admin-clear") {
        [queryKeys.system.storageBreakdown, queryKeys.system.systemInfo, queryKeys.sources, ["search"], ["reference-name-anchors"]].forEach(key => invalidate(key));
      }
      if (entity === "creators" || entity === "all") {
        [queryKeys.creators.all, queryKeys.subscriptions.all, queryKeys.workbench, ["repositories"], ["creator-subscription-overview"]].forEach(key => invalidate(key));
      }
      if (entity === "works" || entity === "all" || entity === "downloads") {
        [queryKeys.works.all, queryKeys.workbench, queryKeys.system.storageBreakdown, queryKeys.system.systemInfo, queryKeys.curation.all,
          queryKeys.downloadJobs.all, queryKeys.importJobs.all].forEach(key => invalidate(key));
      }
      if (entity === "tags" || entity === "all") [queryKeys.tags.all, queryKeys.works.all, queryKeys.workbench].forEach(key => invalidate(key));
      if (entity === "jobs" || entity === "downloads" || entity === "all") {
        [queryKeys.subscriptions.all, queryKeys.downloadJobs.all, queryKeys.importJobs.all, queryKeys.system.queueStats, queryKeys.workbench].forEach(key => invalidate(key));
      }
      if (entity === "settings") [queryKeys.admin.settings, queryKeys.schedulerDecisions, ["gallerydl-config"]].forEach(key => invalidate(key));
    }
  }
  for (const filters of refreshes.values()) void qc.invalidateQueries(filters);
}
