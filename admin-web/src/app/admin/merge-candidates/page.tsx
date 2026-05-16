"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";
import { useRouter } from "next/navigation";

export default function MergeCandidatesPage() {
  const router = useRouter();
  const mc = useQuery({ queryKey: ["merge-candidates"], queryFn: api.listMergeCandidates });

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title="Merge Candidates" description="Works with the same title across multiple sources" />

      <div className="bg-yellow-50 dark:bg-yellow-900/30 border border-yellow-200 dark:border-yellow-800 rounded-lg p-4 text-sm mb-6">
        <strong>Manual review required.</strong> Cross-source merge is not automatic. Review candidates and merge manually.
      </div>

      {mc.isLoading && <div className="animate-pulse space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 dark:bg-slate-700 rounded" />)}</div>}
      {mc.error && <ErrorState message={(mc.error as Error).message} />}

      {mc.data && mc.data.candidates.length === 0 && (
        <EmptyState title="No merge candidates" description="No works with matching titles across different sources." />
      )}

      {mc.data?.candidates.map((c, i) => (
        <div key={i} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 mb-2 text-sm">
          <div className="font-medium mb-2">{c.title}</div>
          <div className="flex items-center gap-2 mb-2">
            {c.sources.map((s) => <SourceBadge key={s} source={s} />)}
            <span className="text-xs text-gray-500 dark:text-gray-400">{c.source_count} sources</span>
          </div>
          <div className="flex gap-2 flex-wrap">
            {c.work_ids.map((wid) => (
              <button key={wid} onClick={() => router.push(`/admin/works/${wid}`)}
                className="text-xs text-blue-600 hover:underline font-mono">{wid.slice(0, 8)}</button>
            ))}
          </div>
        </div>
      ))}
    </main>
  );
}
