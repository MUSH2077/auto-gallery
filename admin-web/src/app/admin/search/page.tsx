"use client";
import { useState, useEffect, useRef, Suspense } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { api } from "@/lib/api";
import { useToast } from "@/components/Toast";
import { useT } from "@/lib/i18n";
import { PageHeader, EmptyState, SourceBadge } from "@/components";
import { Breadcrumb } from "@/components/Breadcrumb";

type Kind = "all" | "works" | "creators" | "tags";

function SearchContent() {
  const t = useT();
  const toast = useToast();
  const router = useRouter();
  const qc = useQueryClient();
  const searchParams = useSearchParams();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [kind, setKind] = useState<Kind>("all");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const q = searchParams.get("q");
    if (q) { setQuery(q); setDebounced(q); }
  }, [searchParams]);

  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setDebounced(query), 300);
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, [query]);

  const results = useQuery({
    queryKey: ["search", debounced, kind],
    queryFn: () => api.search(debounced, 0, kind === "all" ? 20 : 50, kind),
    enabled: debounced.length > 0,
  });

  const data = results.data;
  const worksHits = data?.results || [];
  const creatorsHits = (data?.creators || []) as any[];
  const tagsHits = (data?.tags || []) as any[];
  const total = data?.total || 0;

  const tabs: { key: Kind; label: string; count: number }[] = [
    { key: "all", label: t("search.tab_all"), count: total || (worksHits.length + creatorsHits.length + tagsHits.length) },
    { key: "works", label: t("search.tab_works"), count: kind === "works" ? total : 0 },
    { key: "creators", label: t("search.tab_creators"), count: creatorsHits.length },
    { key: "tags", label: t("search.tab_tags"), count: tagsHits.length },
  ];

  return (
    <main className="max-w-6xl mx-auto p-6">
      <Breadcrumb items={[{ label: t("search.title") }, { label: debounced || "..." }]} />
      <PageHeader title={t("search.title")} description={t("search.desc")}>
        <button className="btn-ghost px-3 py-1.5 text-xs" onClick={() => {
          if (confirm(t("settings.reindex_confirm_msg"))) {
            api.reindexSearch().then((d) => toast.info(d.message || d.status)).catch((e: Error) => toast.info(e.message));
          }
        }}>{t("settings.reindex")}</button>
      </PageHeader>
      <div className="flex gap-2 mb-6">
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("search.placeholder")}
          className="input flex-1 px-4" autoFocus />
      </div>

      {!debounced && <EmptyState title={t("search.empty")} description={t("search.empty_desc")} />}
      {results.isLoading && debounced && <div className="space-y-2">{Array.from({ length: 5 }).map((_, i) => <div key={i} className="h-20 rounded-md bg-gray-200 animate-pulse dark:bg-slate-700" />)}</div>}

      {data && debounced && (
        <>
          <div className="flex gap-1 mb-4 border-b border-gray-200 dark:border-gray-700">
            {tabs.map(tab => (
              <button key={tab.key} onClick={() => setKind(tab.key)}
                className={`px-4 py-2 text-sm border-b-2 -mb-px transition-colors ${kind === tab.key ? "border-blue-500 text-blue-600 font-medium" : "border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400"}`}>
                {tab.label} <span className="ml-1.5 text-xs bg-gray-100 dark:bg-gray-700 rounded-full px-1.5 py-0.5">{tab.count}</span>
              </button>
            ))}
          </div>

          {(kind === "all" || kind === "creators") && creatorsHits.length > 0 && (
            <div className="mb-6">
              {kind === "all" && <h3 className="text-sm font-medium text-gray-500 mb-2">{t("search.creators_section")}</h3>}
              <div className="space-y-2">
                {creatorsHits.map((c: any) => (
                  <Link key={c.id} href={`/admin/creators/${c.id}`}
                    className="card-interactive flex items-center gap-4 p-4 cursor-pointer">
                    <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-blue-500 to-purple-600 text-white font-semibold text-lg">
                      {(c.display_name || c.name).trim().slice(0, 2).toUpperCase()}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="font-medium text-gray-900 dark:text-white">{c.display_name || c.name}</div>
                      {c.display_name && c.display_name !== c.name && <div className="text-xs text-gray-500 font-mono">{c.name}</div>}
                      {c.description && <p className="text-xs text-gray-500 mt-1 line-clamp-1">{c.description}</p>}
                    </div>
                  </Link>
                ))}
              </div>
            </div>
          )}

          {(kind === "all" || kind === "works") && worksHits.length > 0 && (
            <div className="mb-6">
              {kind === "all" && <h3 className="text-sm font-medium text-gray-500 mb-2">{t("search.works_section")}</h3>}
              <div className="space-y-2">
                {worksHits.map((w: any) => (
                  <div key={w.id} className="card-interactive flex cursor-pointer gap-4 p-4" onClick={() => router.push(`/admin/works/${w.id}`)}>
                    {w.thumbnail_asset_id ? (
                      <img src={api.mediaUrl(w.thumbnail_asset_id, "thumb")} alt={w.title || ""} className="w-16 h-16 object-cover rounded shrink-0" loading="lazy" />
                    ) : (
                      <div className="flex h-16 w-16 shrink-0 items-center justify-center rounded-md border border-gray-200 bg-gray-50 text-xs text-gray-500">{t("search.na")}</div>
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium">{w.title || t("search.untitled")}</span>
                        {w.is_nsfw && <span className="badge bg-red-100 text-red-700 text-xs">{t("search.nsfw")}</span>}
                      </div>
                      <div className="mt-1 flex items-center gap-2 text-xs text-gray-500">
                        {w.source && <SourceBadge source={w.source} href={`/admin/works?source=${w.source}`} />}
                        {w.creator_name && (
                          <Link href={`/admin/creators/${w.creator_id || w.id}`} onClick={(e) => e.stopPropagation()}
                            className="text-blue-600 hover:underline">{w.creator_name}</Link>
                        )}
                        {w.posted_at && <span>{new Date(w.posted_at).toLocaleDateString()}</span>}
                      </div>
                      {w.tags && w.tags.length > 0 && (
                        <div className="flex gap-1 mt-1.5 flex-wrap">
                          {w.tags.slice(0, 8).map((tag: string) => (
                            <span key={tag} onClick={(e) => { e.stopPropagation(); router.push(`/admin/search?q=${encodeURIComponent(tag)}`); }}
                              className="badge cursor-pointer text-[10px] hover:border-blue-500 hover:text-blue-600">{tag}</span>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {(kind === "all" || kind === "tags") && tagsHits.length > 0 && (
            <div>
              {kind === "all" && <h3 className="text-sm font-medium text-gray-500 mb-2">{t("search.tags_section")}</h3>}
              <div className="flex flex-wrap gap-2">
                {tagsHits.map((tag: any) => (
                  <Link key={tag.id} href={`/admin/tags/${tag.id}`}
                    className="px-3 py-1.5 bg-gray-100 dark:bg-slate-700 rounded-full text-sm hover:bg-blue-100 hover:text-blue-700 dark:hover:bg-blue-900/30 dark:hover:text-blue-300 transition-colors">
                    #{tag.normalized_name}
                    {tag.category && <span className="text-xs text-gray-400 ml-1">({tag.category})</span>}
                  </Link>
                ))}
              </div>
            </div>
          )}

          {total === 0 && worksHits.length === 0 && creatorsHits.length === 0 && tagsHits.length === 0 && debounced && (
            <EmptyState title={t("search.no_results")} description={t("search.no_results_for").replace("{query}", debounced)} />
          )}
        </>
      )}
    </main>
  );
}

export default function SearchPage() {
  return <Suspense><SearchContent /></Suspense>;
}
