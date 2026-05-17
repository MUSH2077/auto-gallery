"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, ConfirmDialog } from "@/components";
import { useRouter } from "next/navigation";

export default function CreatorDuplicatesPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const dups = useQuery({ queryKey: ["creator-duplicates"], queryFn: api.listDuplicateCreators });
  const [selectedTarget, setSelectedTarget] = useState<string | null>(null);
  const [selectedSources, setSelectedSources] = useState<Set<string>>(new Set());
  const [confirmMerge, setConfirmMerge] = useState(false);

  const merge = useMutation({
    mutationFn: (params: { targetId: string; sourceIds: string[] }) =>
      api.mergeCreators(params.targetId, params.sourceIds),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["creator-duplicates"] });
      qc.invalidateQueries({ queryKey: queryKeys.creators.all });
      setSelectedTarget(null);
      setSelectedSources(new Set());
      setConfirmMerge(false);
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
      <PageHeader title="Creator Duplicates" description="Detect and merge duplicate creator records across sources." />

      <div className="bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 rounded-lg p-4 text-sm mb-6">
        <strong>Manual review required.</strong> Merging transfers all links, source accounts, and subscriptions from the source creator to the target. The source creator is deleted after merging. This action is irreversible.
      </div>

      {dups.isLoading && (
        <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-24 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>
      )}
      {dups.error && <ErrorState message={(dups.error as Error).message} onRetry={() => dups.refetch()} />}
      {dups.data && dups.data.duplicates.length === 0 && (
        <EmptyState title="No duplicates" description="All creators have unique identities." />
      )}

      {dups.data?.duplicates.map((group, gi) => (
        <div key={gi} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 mb-4">
          <div className="flex items-center justify-between mb-3">
            <div>
              <span className="text-xs px-2 py-0.5 rounded bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-400 font-mono">
                {group.reason.replace(/_/g, " ")}
              </span>
              <span className="text-sm text-gray-500 dark:text-gray-400 ml-2">{group.description}</span>
            </div>
            <span className="text-xs text-gray-400">{group.creator_ids.length} creators</span>
          </div>

          <div className="space-y-2">
            {group.creator_ids.map((cid, i) => (
              <div key={cid} className="flex items-center gap-3 p-2 rounded border dark:border-slate-700 hover:bg-gray-50 dark:hover:bg-slate-700/50">
                <input
                  type="checkbox"
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
                  <span className="text-xs text-gray-400 dark:text-gray-500 font-mono">{cid.slice(0, 8)}...</span>
                </div>
                <span className="text-xs text-gray-400 shrink-0">
                  {cid === group.creator_ids[0] ? "Keep as target" : "Merge into target"}
                </span>
              </div>
            ))}
          </div>
        </div>
      ))}

      {/* Merge action bar */}
      {selectedSources.size > 0 && (
        <div className="fixed bottom-0 left-0 right-0 bg-white dark:bg-slate-800 border-t dark:border-slate-700 shadow-lg p-4 flex items-center justify-between z-30">
          <div>
            <span className="text-sm font-medium">
              Target: <span className="font-mono text-blue-600">{selectedTarget?.slice(0, 8)}...</span>
            </span>
            <span className="text-sm text-gray-500 dark:text-gray-400 ml-4">
              {selectedSources.size} source{selectedSources.size > 1 ? "s" : ""} selected
            </span>
          </div>
          <div className="flex gap-3">
            <button
              onClick={() => { setSelectedSources(new Set()); setSelectedTarget(null); }}
              className="px-4 py-2 text-sm border rounded hover:bg-gray-50 dark:hover:bg-slate-700 dark:text-gray-300"
            >
              Cancel
            </button>
            <button
              onClick={() => setConfirmMerge(true)}
              className="px-4 py-2 text-sm bg-red-600 text-white rounded hover:bg-red-700"
            >
              Merge {selectedSources.size} → Target
            </button>
          </div>
        </div>
      )}

      {confirmMerge && (
        <ConfirmDialog
          open
          title="Merge Creators"
          message={`Merge ${selectedSources.size} creator(s) into the target? Source creators will be deleted. This cannot be undone.`}
          onConfirm={handleMerge}
          onCancel={() => setConfirmMerge(false)}
          isPending={merge.isPending}
          error={(merge.error as Error)?.message}
        />
      )}
    </main>
  );
}
