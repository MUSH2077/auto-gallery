import type { QueryClient } from "@tanstack/react-query";
import { queryKeys } from "@/lib/api";

export type OperationRefreshMeta = Record<string, unknown> | null | undefined;

/** Resource-owned cache refresh policy for completed admin operations. */
export function refreshOperationResources(
  qc: QueryClient,
  kind: string,
  meta?: OperationRefreshMeta,
  result?: Record<string, unknown> | null,
) {
  const invalidate = (key: readonly unknown[]) => { void qc.invalidateQueries({ queryKey: key }); };
  const refetch = (key: readonly unknown[]) => { void qc.refetchQueries({ queryKey: key, type: "all" }); };
  const refetchCreatorSubscriptions = () => {
    invalidate(queryKeys.creators.all);
    invalidate(queryKeys.subscriptions.all);
    invalidate(queryKeys.creators.count);
    invalidate(queryKeys.subscriptions.count);
    refetch(queryKeys.creators.all);
    refetch(queryKeys.subscriptions.all);
  };

  if (kind === "danbooru-import-all") {
    refetchCreatorSubscriptions();
    invalidate(queryKeys.workbench);
    invalidate(["creator-subscription-overview"]);
    refetch(queryKeys.workbench);
    refetch(["creator-subscription-overview"]);
    const creatorId = typeof result?.creator_id === "string" ? result.creator_id : null;
    if (creatorId) {
      refetch(queryKeys.creators.detail(creatorId));
      refetch(queryKeys.creators.links(creatorId));
      refetch(["creator-subscription-overview", creatorId]);
    }
    const subscriptionId = typeof result?.subscription_id === "string" ? result.subscription_id : null;
    if (subscriptionId) {
      refetch(queryKeys.subscriptions.detail(subscriptionId));
      refetch(queryKeys.subscriptions.sources(subscriptionId));
    }
    return;
  }
  if (kind === "danbooru-mapping-refresh") {
    refetchCreatorSubscriptions();
    [queryKeys.workbench, ["repositories"], ["creator-subscription-overview"], queryKeys.tasks.all].forEach(invalidate);
    return;
  }
  if (kind === "hierarchy-delete") {
    refetchCreatorSubscriptions();
    [queryKeys.works.all, queryKeys.curation.all, queryKeys.downloadJobs.all, queryKeys.importJobs.all,
      queryKeys.tasks.all, queryKeys.workbench, ["repositories"], ["creator-subscription-overview"], queryKeys.system.storageBreakdown].forEach(invalidate);
    return;
  }

  invalidate(queryKeys.tasks.all);
  if (kind === "admin-backup-create") {
    invalidate(queryKeys.backups.all);
    return;
  }
  if (kind === "admin-cleanup-metadata-jsons" || kind === "admin-creator-reenrich") {
    [queryKeys.system.storageBreakdown, queryKeys.system.systemInfo].forEach(invalidate);
    return;
  }
  if (kind === "admin-rebuild" || kind === "admin-disk-import") {
    [queryKeys.workbench, queryKeys.works.all, queryKeys.downloadJobs.all, queryKeys.importJobs.all, queryKeys.system.storageBreakdown, queryKeys.system.systemInfo].forEach(invalidate);
    return;
  }
  const entity = typeof meta?.entity === "string" ? meta.entity : undefined;
  if (kind === "admin-clear") {
    [queryKeys.system.storageBreakdown, queryKeys.system.systemInfo, queryKeys.sources, ["search"], ["reference-name-anchors"]].forEach(invalidate);
  }
  if (entity === "creators" || entity === "all") {
    // This callback also runs for restored results. Invalidate rather than
    // inventing zero counts that could overwrite content added since then.
    [queryKeys.creators.all, queryKeys.subscriptions.all, queryKeys.workbench, ["repositories"], ["creator-subscription-overview"]].forEach(invalidate);
  }
  if (entity === "works" || entity === "all" || entity === "downloads") {
    [queryKeys.works.all, queryKeys.workbench, queryKeys.system.storageBreakdown, queryKeys.system.systemInfo, queryKeys.curation.all,
      queryKeys.downloadJobs.all, queryKeys.importJobs.all].forEach(invalidate);
  }
  if (entity === "tags" || entity === "all") [queryKeys.tags.all, queryKeys.works.all, queryKeys.workbench].forEach(invalidate);
  if (entity === "jobs" || entity === "downloads" || entity === "all") {
    [queryKeys.subscriptions.all, queryKeys.downloadJobs.all, queryKeys.importJobs.all, queryKeys.system.queueStats, queryKeys.workbench].forEach(invalidate);
  }
  if (entity === "settings") [queryKeys.admin.settings, queryKeys.schedulerDecisions, ["gallerydl-config"]].forEach(invalidate);
}
