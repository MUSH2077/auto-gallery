"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys, WorkListItem } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState } from "@/components";

export default function WorksPage() {
  const router = useRouter();
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const limit = 25;

  const works = useQuery({ queryKey: [...queryKeys.works.all, page], queryFn: () => api.listWorks(page * limit, limit) });

  return (
    <main className="max-w-7xl mx-auto p-6">
      <PageHeader title="Works" description={`${works.data?.length || 0} works`} />

      <div className="flex gap-3 mb-4">
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search works..." className="flex-1 max-w-md border rounded px-3 py-2 text-sm" />
      </div>

      {works.isLoading && <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4">{Array.from({ length: 10 }).map((_, i) => <div key={i} className="bg-white dark:bg-slate-800 rounded-lg shadow p-3 animate-pulse"><div className="h-32 bg-gray-200 rounded mb-2" /><div className="h-3 bg-gray-200 rounded w-3/4" /></div>)}</div>}
      {works.error && <ErrorState message={(works.error as Error).message} />}
      {works.data && !works.data.length && <EmptyState title="No works" description="Works will appear after download and import jobs complete." />}

      {works.data && works.data.length > 0 && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4 mb-6">
            {works.data.filter((w: WorkListItem) => !search || (w.title && w.title.toLowerCase().includes(search.toLowerCase()))).map((w: WorkListItem) => (
              <div key={w.id} className="bg-white dark:bg-slate-800 rounded-lg shadow overflow-hidden cursor-pointer hover:shadow-md transition-shadow" onClick={() => router.push(`/admin/works/${w.id}`)}>
                <div className="h-32 bg-gray-100 dark:bg-slate-700 flex items-center justify-center text-gray-400 dark:text-gray-500 text-xs overflow-hidden relative">
                  {w.thumbnail_asset_id ? (
                    <img src={api.mediaUrl(w.thumbnail_asset_id, "thumb")} alt={w.title || ""} className="w-full h-full object-cover" loading="lazy" />
                  ) : (
                    <span>No thumbnail</span>
                  )}
                  {w.asset_count > 1 && (
                    <span className="absolute top-1 right-1 bg-black/70 text-white text-xs px-1.5 py-0.5 rounded font-medium">
                      {w.asset_count}p
                    </span>
                  )}
                </div>
                <div className="p-3">
                  <div className="text-sm font-medium truncate">{w.title || "Untitled"}</div>
                  <div className="text-xs text-gray-400 dark:text-gray-500 mt-1">
                    {w.posted_at ? new Date(w.posted_at).toLocaleDateString() : "No date"}
                    {w.is_nsfw && <span className="ml-2 px-1.5 py-0.5 bg-red-100 dark:bg-red-900/30 text-red-600 rounded text-xs">NSFW</span>}
                  </div>
                </div>
              </div>
            ))}
          </div>
          <div className="flex gap-2 justify-center">
            <button disabled={page === 0} onClick={() => setPage(page - 1)} className="px-3 py-1 text-sm border rounded disabled:opacity-30">Prev</button>
            <button onClick={() => setPage(page + 1)} disabled={!works.data || works.data.length < limit} className="px-3 py-1 text-sm border rounded disabled:opacity-30">Next</button>
          </div>
        </>
      )}
    </main>
  );
}
