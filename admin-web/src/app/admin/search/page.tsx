"use client";
import { useState, useEffect, useRef, Suspense } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";

function SearchContent() {
  const t = useT();
  const toast = useToast();
  const router = useRouter();
  const searchParams = useSearchParams();
  const qc = useQueryClient();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Sync URL ?q= param into search — handles initial load AND tag-click navigation
  useEffect(() => {
    const q = searchParams.get("q");
    if (q) {
      setQuery(q);
      setDebounced(q);
    }
  }, [searchParams]);

  // Debounce: search 300ms after user stops typing
  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setDebounced(query), 300);
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, [query]);

  const results = useQuery({
    queryKey: ["search", debounced],
    queryFn: () => api.search(debounced),
    enabled: debounced.length > 0,
  });

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title={t("search.title")} description={t("search.desc")}>
        <button
          className="btn-ghost px-3 py-1.5 text-xs"
          onClick={() => {
            if (confirm(t("settings.reindex_confirm_msg"))) {
              api.reindexSearch()
                .then((d) => toast.info(d.message || d.status))
                .catch((e: Error) => toast.info(e.message));
            }
          }}>
          {t("settings.reindex")}
        </button>
      </PageHeader>
      <div className="flex gap-2 mb-6">
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("search.placeholder")}
          className="input flex-1 px-4"
          autoFocus />
      </div>

      {!debounced && <EmptyState title={t("search.empty")} description={t("search.empty_desc")} />}

      {results.isLoading && debounced && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-20 rounded-md bg-[#eaeef2] animate-pulse dark:bg-[#21262d]" />)}</div>}
      {results.data && !results.data.total && debounced && <EmptyState title={t("search.no_results")} description={t("search.no_results_for").replace("{query}", debounced)} />}
      {results.data && results.data.total > 0 && (
        <div>
          <p className="mb-4 text-sm text-[#57606a] dark:text-[#8b949e]">{t("search.results_for").replace("{count}", String(results.data.total)).replace("{query}", debounced)}</p>
          <div className="space-y-2">
                {results.data.results.map((r: any, i: number) => (
                  <div key={r.id || i} className="card-interactive flex cursor-pointer gap-4 p-4" onClick={() => router.push(`/admin/works/${r.id}`)}>
                    {r.thumbnail_asset_id ? (
                      <img src={api.mediaUrl(r.thumbnail_asset_id, "thumb")} alt={r.title || ""} className="w-16 h-16 object-cover rounded shrink-0" loading="lazy" />
                    ) : (
                      <div className="flex h-16 w-16 shrink-0 items-center justify-center rounded-md border border-ag-border bg-[#f6f8fa] text-xs text-[#57606a] dark:border-ag-border dark:bg-[#21262d] dark:text-[#8b949e]">{t("search.na")}</div>
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium text-[#24292f] dark:text-ag-text">{r.title || t("search.untitled")}</span>
                        {r.is_nsfw && <span className="badge border-[#ffebe9] bg-[#ffebe9] text-[#cf222e] dark:border-[#da3633]/30 dark:bg-[#da3633]/15 dark:text-[#ff7b72]">{t("search.nsfw")}</span>}
                        {r.asset_count > 1 && <span className="shrink-0 text-xs text-[#57606a] dark:text-[#8b949e]">{r.asset_count}p</span>}
                      </div>
                      <div className="mt-1 flex items-center gap-2 text-xs text-[#57606a] dark:text-[#8b949e]">
                        {r.source && <SourceBadge source={r.source} />}
                        {r.creator_name && <span>{r.creator_name}</span>}
                        {r.posted_at && <span>{new Date(r.posted_at).toLocaleDateString()}</span>}
                      </div>
                      {r.description && <p className="mt-1 line-clamp-2 text-xs text-[#57606a] dark:text-[#8b949e]">{r.description}</p>}
                      {r.tags && r.tags.length > 0 && (
                        <div className="flex gap-1 mt-1.5 flex-wrap">
                          {r.tags.slice(0, 8).map((tag: string) => (
                            <span key={tag} onClick={(e) => { e.stopPropagation(); router.push(`/admin/search?q=${encodeURIComponent(tag)}`); }}
                              className="badge cursor-pointer text-[10px] transition-colors hover:border-[#0969da] hover:text-[#0969da] dark:hover:border-[#58a6ff] dark:hover:text-[#58a6ff]">{tag}</span>
                          ))}
                          {r.tags.length > 8 && <span className="text-[10px] text-[#57606a] dark:text-[#8b949e]">+{r.tags.length - 8}</span>}
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

export default function SearchPage() {
  return (
    <Suspense>
      <SearchContent />
    </Suspense>
  );
}
