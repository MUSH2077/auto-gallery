"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { motionConfig, motionTokens, useStaggeredEntrance } from "@/lib/motion";
import { PageHeader, PageShell, EmptyState, ErrorState, ConfirmDialog, PermissionGuard } from "@/components";
import { useRouter } from "next/navigation";
import { useT } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";

type MergeSelection = {
  key: string;
  targetId: string;
  targetName: string;
  sources: Map<string, string>;
};

function duplicateGroupKey(group: { reason: string; description: string }) {
  const normalize = (value: string) => value.normalize("NFKC").trim().toLocaleLowerCase();
  return `${normalize(group.reason)}\u0000${normalize(group.description)}`;
}

function CreatorDuplicatesContent() {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();
  const { has } = usePermissions();
  const canCurate = has("curation");
  const dups = useQuery({ queryKey: queryKeys.creators.duplicates, queryFn: api.listDuplicateCreators });
  const [selection, setSelection] = useState<MergeSelection | null>(null);
  const [mergeFailures, setMergeFailures] = useState<Array<{ id: string; name: string; reason: string }>>([]);
  const [confirmMerge, setConfirmMerge] = useState(false);
  const duplicateGroups = dups.data?.duplicates || [];
  const groupEntrance = useStaggeredEntrance(
    duplicateGroups.map(duplicateGroupKey),
  );
  // Merge feedback: the merged group collapses briefly before the refetch
  // removes it (state confirmation → essential, survives low-end gate).
  const [collapsingGroup, setCollapsingGroup] = useState<string | null>(null);

  const merge = useMutation({
    mutationFn: (params: { targetId: string; sourceIds: string[] }) =>
      api.mergeCreators(params.targetId, params.sourceIds),
    onSuccess: (result) => {
      const failed = result.results.filter((item) => item.status !== "merged" && item.status !== "ok");
      const failedIds = new Set(failed.map((item) => item.source_id));
      setMergeFailures(failed.map((item) => ({
        id: item.source_id,
        name: selection?.sources.get(item.source_id) || item.source_id,
        reason: item.error || t("duplicates.merge_failed"),
      })));
      void qc.invalidateQueries({ queryKey: queryKeys.creators.duplicates });
      void qc.invalidateQueries({ queryKey: queryKeys.creators.all });
      void qc.invalidateQueries({ queryKey: queryKeys.subscriptions.all });
      void qc.invalidateQueries({ queryKey: ["search"] });
      if (failed.length) {
        setSelection((current) => current ? {
          ...current,
          sources: new Map([...current.sources].filter(([sourceId]) => failedIds.has(sourceId))),
        } : null);
        setConfirmMerge(true);
        return;
      }
      setConfirmMerge(false);
      const finish = () => {
        setSelection(null);
        setCollapsingGroup(null);
      };
      const matchedGroup = dups.data?.duplicates.find(
        (group) => duplicateGroupKey(group) === selection?.key,
      );
      if (matchedGroup && motionConfig.shouldAnimate({ essential: true })) {
        setCollapsingGroup(duplicateGroupKey(matchedGroup));
        window.setTimeout(finish, motionTokens.duration.slow);
      } else {
        finish();
      }
    },
  });

  const toggleSource = (group: { reason: string; description: string; creator_ids: string[]; creator_names: string[] }, id: string) => {
    const key = duplicateGroupKey(group);
    const nameAt = (creatorId: string) => group.creator_names[group.creator_ids.indexOf(creatorId)] || creatorId;
    const targetId = group.creator_ids.find((creatorId) => creatorId !== id) || id;
    setMergeFailures([]);
    setSelection((current) => {
      if (!current || current.key !== key) {
        return { key, targetId, targetName: nameAt(targetId), sources: new Map([[id, nameAt(id)]]) };
      }
      if (id === current.targetId) return current;
      const sources = new Map(current.sources);
      if (sources.has(id)) sources.delete(id); else sources.set(id, nameAt(id));
      return sources.size ? { ...current, sources } : null;
    });
  };

  const handleMerge = () => {
    if (!selection || selection.sources.size === 0) return;
    merge.mutate({ targetId: selection.targetId, sourceIds: [...selection.sources.keys()] });
  };

  return (
    <PageShell>
      <PageHeader title={t("duplicates.title")} description={t("duplicates.desc")} />

      <div className="mb-6 rounded-md border border-warning-subtle bg-warning-subtle p-4 text-sm text-warning dark:border-warning/30 dark:bg-warning/15 dark:text-warning">
        <strong>{t("duplicates.warning")}</strong> {t("duplicates.warning_detail")}
      </div>

      {dups.isLoading && (
        <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-24 rounded-md bg-subtle animate-pulse dark:bg-subtle" />)}</div>
      )}
      {dups.error && <ErrorState message={(dups.error as Error).message} onRetry={() => dups.refetch()} />}
      {dups.data && dups.data.duplicates.length === 0 && (
        <EmptyState title={t("duplicates.no_duplicates")} description={t("duplicates.no_duplicates_desc")} />
      )}

      {dups.data?.duplicates.map((group, gi) => {
        const groupKey = duplicateGroupKey(group);
        const entrance = groupEntrance(groupKey, gi);
        const activeGroup = selection?.key === groupKey;
        const displayedTargetId = activeGroup ? selection.targetId : group.creator_ids[0];
        return (
        <div key={groupKey}
          className={`card mb-4 p-4 ${entrance.className} ${collapsingGroup === groupKey ? "merge-collapse" : ""}`}
          style={entrance.style}>
          <div className="flex items-center justify-between mb-3">
            <div>
              <span className="badge font-mono">
                {group.reason.replace(/_/g, " ")}
              </span>
              <span className="ml-2 text-sm text-muted">{group.description}</span>
            </div>
            <span className="text-xs text-muted">{group.creator_ids.length} {t("duplicates.creators_count")}</span>
          </div>

          <div className="space-y-2">
            {group.creator_ids.map((cid, i) => (
              <div key={cid} className="flex items-center gap-3 rounded-md border border-border p-2 transition-colors hover:bg-subtle dark:border-border dark:hover:bg-subtle">
                {canCurate && (
                  <input type="checkbox" aria-label={t("common.select_item", { name: group.creator_names[i] || cid.slice(0, 8) })}
                    checked={activeGroup && selection.sources.has(cid)}
                    disabled={activeGroup && selection.targetId === cid}
                    onChange={() => toggleSource(group, cid)}
                    className="rounded shrink-0"
                  />
                )}
                <div className="flex-1 min-w-0">
                  <button
                    onClick={() => router.push(`/admin/creators/${cid}`)}
                    className="text-sm font-medium text-blue-600 hover:underline truncate block"
                  >
                    {group.creator_names[i] || cid.slice(0, 8)}
                  </button>
                  <span className="font-mono text-xs text-muted">{cid.slice(0, 8)}...</span>
                </div>
                <span className="shrink-0 text-xs text-muted">
                  {cid === displayedTargetId ? t("duplicates.keep_target") : t("duplicates.merge_into")}
                </span>
              </div>
            ))}
          </div>
        </div>
        );
      })}

      {/* Merge action bar */}
      {canCurate && selection && selection.sources.size > 0 && (
        <div className="fixed right-0 bottom-0 left-0 z-30 flex items-center justify-between border-t border-border bg-white p-4 shadow-lg dark:border-border dark:bg-surface">
          <div>
            <span className="text-sm font-medium">
              {t("duplicates.target")} <span className="text-blue-600">{selection.targetName}</span>
            </span>
            <span className="ml-4 text-sm text-muted">
              {t("duplicates.source_selected").replace("{count}", String(selection.sources.size))}: {[...selection.sources.values()].join(", ")}
            </span>
          </div>
          <div className="flex gap-3">
            <button
              onClick={() => { setSelection(null); setMergeFailures([]); }}
              disabled={merge.isPending}
              className="btn-ghost"
            >
              {t("duplicates.cancel")}
            </button>
            <button
              onClick={() => setConfirmMerge(true)}
              disabled={merge.isPending}
              className="btn-danger"
            >
              {t("duplicates.merge_btn").replace("{count}", String(selection.sources.size))}
            </button>
          </div>
        </div>
      )}

      {mergeFailures.length > 0 && (
        <div role="alert" className="mb-4 rounded-md border border-danger/30 bg-danger-subtle p-3 text-sm text-danger">
          <p className="font-medium">{t("duplicates.partial_failure")}</p>
          {mergeFailures.map((failure) => <p key={failure.id}>{failure.name}: {failure.reason}</p>)}
        </div>
      )}

      {confirmMerge && (
        <ConfirmDialog
          open
          title={t("duplicates.merge_title")}
          message={`${t("duplicates.merge_msg").replace("{count}", String(selection?.sources.size || 0))} ${selection?.targetName || ""} ← ${[...(selection?.sources.values() || [])].join(", ")}`}
          onConfirm={handleMerge}
          onCancel={() => setConfirmMerge(false)}
          isPending={merge.isPending}
          error={(merge.error as Error)?.message}
        >
          {mergeFailures.length > 0 && <div role="alert" className="mb-3 text-sm text-danger">
            {mergeFailures.map((failure) => <p key={failure.id}>{failure.name}: {failure.reason}</p>)}
          </div>}
        </ConfirmDialog>
      )}
    </PageShell>
  );
}

export default function CreatorDuplicatesPage() {
  return <PermissionGuard module="library"><CreatorDuplicatesContent /></PermissionGuard>;
}
