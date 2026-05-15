"use client";
import { useState } from "react";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, SourceBadge } from "@/components";


function AllPages({ workId }: { workId: string }) {
  const assets = useQuery({ queryKey: ["works", workId, "assets"], queryFn: () => api.getWorkAssets(workId) });
  const [activeIndex, setActiveIndex] = useState(0);
  if (assets.isLoading) return <div className="bg-white rounded-lg shadow p-4 animate-pulse"><div className="h-24 bg-gray-100 rounded" /></div>;
  if (!assets.data || !assets.data.length) return <div className="bg-white rounded-lg shadow p-4"><h3 className="font-medium mb-2 text-sm">Pages</h3><p className="text-xs text-gray-400">No assets available.</p></div>;
  const current = assets.data[activeIndex];
  return (
    <div className="bg-white rounded-lg shadow p-4">
      <h3 className="font-medium mb-2 text-sm">Pages ({assets.data.length})</h3>
      {current && (
        <div className="mb-3">
          <img src={api.mediaUrl(current.id, "preview")} alt={current.file_name} className="w-full rounded-lg object-contain max-h-96 bg-gray-100" />
          <p className="text-xs text-gray-400 mt-1 text-center">{activeIndex + 1} / {assets.data.length}</p>
        </div>
      )}
      <div className="flex gap-2 overflow-x-auto pb-1">
        {assets.data.map((a, i) => (
          <button key={a.id} onClick={() => setActiveIndex(i)}
            className={`shrink-0 w-16 h-16 rounded border-2 overflow-hidden ${i === activeIndex ? "border-blue-500" : "border-gray-200 hover:border-gray-400"}`}>
            <img src={api.mediaUrl(a.id, "thumb")} alt={`Page ${i+1}`} className="w-full h-full object-cover" />
          </button>
        ))}
      </div>
    </div>
  );
}

export default function WorkDetailPage() {
  const params = useParams();
  const id = params.id as string;

  const work = useQuery({ queryKey: queryKeys.works.detail(id), queryFn: () => api.getWork(id) });
  const sources = useQuery({ queryKey: queryKeys.works.sources(id), queryFn: () => api.getWorkSources(id) });

  if (work.isLoading) return <main className="max-w-4xl mx-auto p-6"><div className="animate-pulse space-y-4"><div className="h-8 bg-gray-200 rounded w-1/3" /><div className="h-64 bg-gray-200 rounded" /></div></main>;
  if (work.error) return <main className="max-w-4xl mx-auto p-6"><div className="p-4 bg-red-50 border border-red-200 rounded-lg text-red-700">{(work.error as Error).message}</div></main>;
  if (!work.data) return null;
  const w = work.data;

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title={w.title || "Untitled"} description={`${w.is_nsfw ? "🔞 NSFW · " : ""}Posted: ${w.posted_at || "Unknown"}`} />

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div className="md:col-span-2 space-y-4">
          <div className="bg-white rounded-lg shadow p-4">
            <h3 className="font-medium mb-3">Details</h3>
            <dl className="text-sm space-y-2">
              <div className="flex gap-2"><dt className="text-gray-500 w-24">Title:</dt><dd>{w.title || "Untitled"}</dd></div>
              <div className="flex gap-2"><dt className="text-gray-500 w-24">Posted:</dt><dd>{w.posted_at || "Unknown"}</dd></div>
              <div className="flex gap-2"><dt className="text-gray-500 w-24">NSFW:</dt><dd>{w.is_nsfw ? "Yes" : "No"}</dd></div>
              <div className="flex gap-2"><dt className="text-gray-500 w-24">Created:</dt><dd className="text-xs">{new Date(w.created_at).toLocaleString()}</dd></div>
            </dl>
          </div>

          {w.description && (
            <div className="bg-white rounded-lg shadow p-4">
              <h3 className="font-medium mb-2">Description</h3>
              <p className="text-sm whitespace-pre-wrap">{w.description}</p>
            </div>
          )}

          <div className="bg-white rounded-lg shadow p-4">
            <h3 className="font-medium mb-3">Source Records ({sources.data?.length || 0})</h3>
            {sources.data && sources.data.length > 0 ? (
              <div className="space-y-2">
                {sources.data.map((s: any) => (
                  <div key={s.id} className="border rounded-lg p-3 text-sm">
                    <div className="flex items-center gap-2 mb-1">
                      <SourceBadge source={s.source} />
                      <span className="font-mono text-xs text-gray-500">{s.source_work_id}</span>
                    </div>
                    {s.source_url && <div className="text-xs"><a href={s.source_url} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">Open source &rarr;</a></div>}
                    {s.title && <div className="text-xs text-gray-600 mt-1">Source title: {s.title}</div>}
                  </div>
                ))}
              </div>
            ) : <p className="text-sm text-gray-400">No source records available.</p>}
          </div>
        </div>

        <div className="space-y-4">
          <AllPages workId={id} />
        </div>
      </div>
    </main>
  );
}
