"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { motionConfig, motionTokens, staggerDelay } from "@/lib/motion";
import { PageHeader, EmptyState, ErrorState, ConfirmDialog } from "@/components";
import { useRouter } from "next/navigation";
import { useT } from "@/lib/i18n";

export default function CreatorDuplicatesPage() {
  const t = useT();
  const router = useRouter();
  const qc = useQueryClient();
  const dups = useQuery({ queryKey: queryKeys.creators.duplicates, queryFn: api.listDuplicateCreators });
  const [selectedTarget, setSelectedTarget] = useState<string | null>(null);
  const [selectedSources, setSelectedSources] = useState<Set<string>>(new Set());
  const [confirmMerge, setConfirmMerge] = useState(false);
  // Merge feedback: the merged group collapses briefly before the refetch
  // removes it (state confirmation → essential, survives low-end gate).
  const [collapsingGroup, setCollapsingGroup] = useState<number | null>(null);

  const merge = useMutation({
    mutationFn: (params: { targetId: string; sourceIds: string[] }) =>
      api.mergeCreators(params.targetId, params.sourceIds),
    onSuccess: () => {
      setConfirmMerge(false);
      const finish = () => {
        qc.invalidateQueries({ queryKey: ["creator-duplicates"] });
        qc.invalidateQueries({ queryKey: queryKeys.creators.all });
        setSelectedTarget(null);
        setSelectedSources(new Set());
        setCollapsingGroup(null);
      };
      const gi = dups.data?.duplicates.findIndex(
        (group) => !!selectedTarget && group.creator_ids.includes(selectedTarget),
      ) ?? -1;
      if (gi >= 0 && motionConfig.shouldAnimate({ essential: true })) {
        setCollapsingGroup(gi);
        window.setTimeout(finish, motionTokens.duration.slow);
      } else {
        finish();
      }
    },
  });

  const toggleSource = (id: string, targetId: string) => {
    const next = new Set(selectedSources);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
      setSelectedTarget(targetId);
    }
    setSelectedSources(next);
  };

  const handleMerge = () => {
    if (!selectedTarget || selectedSources.size === 0) return;
    merge.mutate({ targetId: selectedTarget, sourceIds: [...selectedSources] });
  };

  return (
    <main className="max-w-5xl mx-auto p-6">
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

      {dups.data?.duplicates.map((group, gi) => (
        <div key={gi}
          className={`card mb-4 p-4 page-item ${collapsingGroup === gi ? "merge-collapse" : ""}`}
          style={{ "--delay": staggerDelay(gi) } as React.CSSProperties}>
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
                <input type="checkbox" aria-label="Select item"
                  checked={selectedSources.has(cid)}
                  onChange={() => toggleSource(cid, group.creator_ids[0] === cid ? group.creator_ids[1] : group.creator_ids[0])}
                  className="rounded shrink-0"
                />
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
                  {cid === group.creator_ids[0] ? t("duplicates.keep_target") : t("duplicates.merge_into")}
                </span>
              </div>
            ))}
          </div>
        </div>
      ))}

      {/* Merge action bar */}
      {selectedSources.size > 0 && (
        <div className="fixed right-0 bottom-0 left-0 z-30 flex items-center justify-between border-t border-border bg-white p-4 shadow-lg dark:border-border dark:bg-surface">
          <div>
            <span className="text-sm font-medium">
              {t("duplicates.target")} <span className="font-mono text-blue-600">{selectedTarget?.slice(0, 8)}...</span>
            </span>
            <span className="ml-4 text-sm text-muted">
              {t("duplicates.source_selected").replace("{count}", String(selectedSources.size))}
            </span>
          </div>
          <div className="flex gap-3">
            <button
              onClick={() => { setSelectedSources(new Set()); setSelectedTarget(null); }}
              className="btn-ghost"
            >
              {t("duplicates.cancel")}
            </button>
            <button
              onClick={() => setConfirmMerge(true)}
              className="btn-danger"
            >
              {t("duplicates.merge_btn").replace("{count}", String(selectedSources.size))}
            </button>
          </div>
        </div>
      )}

      {confirmMerge && (
        <ConfirmDialog
          open
          title={t("duplicates.merge_title")}
          message={t("duplicates.merge_msg").replace("{count}", String(selectedSources.size))}
          onConfirm={handleMerge}
          onCancel={() => setConfirmMerge(false)}
          isPending={merge.isPending}
          error={(merge.error as Error)?.message}
        />
      )}
    </main>
  );
}
