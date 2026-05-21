"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { PageHeader, EmptyState, SourceBadge } from "@/components";

export default function SearchPage() {
  const t = useT();
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const results = useQuery({
    queryKey: ["search", submitted],
    queryFn: () => api.search(submitted),
    enabled: !!submitted,
  });

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title={t("search.title")} description={t("search.desc")} />
      <form onSubmit={(e) => { e.preventDefault(); setSubmitted(query); }} className="flex gap-2 mb-6">
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("search.placeholder")} className="flex-1 border rounded px-4 py-2 text-sm dark:bg-slate-700 dark:text-white dark:border-slate-600" />
        <button type="submit" className="px-6 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600">{t("search.button")}</button>
      </form>

      {!submitted && <EmptyState title={t("search.empty")} description={t("search.empty_desc")} />}

      {results.isLoading && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-20 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
      {results.data && !results.data.total && <EmptyState title={t("search.no_results")} description={t("search.no_results_for").replace("{query}", submitted)} />}
      {results.data && results.data.total > 0 && (
        <div>
          <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">{t("search.results_for").replace("{count}", String(results.data.total)).replace("{query}", submitted)}</p>
          <div className="space-y-2">
            {results.data.results.map((r: any, i: number) => (
              <div key={r.id || i} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 flex gap-4 cursor-pointer hover:shadow-md transition-shadow" onClick={() => router.push(`/admin/works/${r.id}`)}>
                {r.thumbnail_asset_id ? (
                  <img src={api.mediaUrl(r.thumbnail_asset_id, "thumb")} alt={r.title || ""} className="w-16 h-16 object-cover rounded shrink-0" />
                ) : (
                  <div className="w-16 h-16 bg-gray-100 dark:bg-slate-700 rounded shrink-0 flex items-center justify-center text-gray-400 text-xs">{t("search.na")}</div>
                )}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-sm dark:text-white truncate">{r.title || t("search.untitled")}</span>
                    {r.is_nsfw && <span className="text-xs bg-red-100 dark:bg-red-900/30 text-red-600 px-1 rounded shrink-0">{t("search.nsfw")}</span>}
                    {r.asset_count > 1 && <span className="text-xs text-gray-400 shrink-0">{r.asset_count}p</span>}
                  </div>
                  <div className="flex items-center gap-2 mt-1 text-xs text-gray-400 dark:text-gray-500">
                    {r.source && <SourceBadge source={r.source} />}
                    {r.creator_name && <span>{r.creator_name}</span>}
                    {r.posted_at && <span>{new Date(r.posted_at).toLocaleDateString()}</span>}
                  </div>
                  {r.description && <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 line-clamp-2">{r.description}</p>}
                  {r.tags && r.tags.length > 0 && (
                    <div className="flex gap-1 mt-1.5 flex-wrap">
                      {r.tags.slice(0, 8).map((tag: string) => (
                        <span key={tag} className="text-[10px] bg-gray-100 dark:bg-slate-700 text-gray-600 dark:text-gray-400 px-1.5 py-0.5 rounded">{tag}</span>
                      ))}
                      {r.tags.length > 8 && <span className="text-[10px] text-gray-400">+{r.tags.length - 8}</span>}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </main>
  );
}
