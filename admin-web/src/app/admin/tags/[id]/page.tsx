"use client";
import { useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { Breadcrumb } from "@/components/Breadcrumb";
import { ErrorState, EmptyState } from "@/components";

export default function TagDetailPage() {
  const t = useT();
  const params = useParams();
  const id = params.id as string;
  const [page, setPage] = useState(0);
  const limit = 24;

  const tag = useQuery({
    queryKey: ["tag-detail", id],
    queryFn: () => api.getTagDetail(id),
  });

  const works = useQuery({
    queryKey: ["tag-works", id, page],
    queryFn: () => api.listWorks(page * limit, limit, { tag: tag.data?.normalized_name }),
    enabled: !!tag.data?.normalized_name,
  });

  if (tag.isLoading) return <main className="max-w-6xl mx-auto p-6"><div className="animate-pulse space-y-4"><div className="h-8 w-1/3 rounded bg-gray-200 dark:bg-slate-700" /><div className="h-64 rounded bg-gray-200 dark:bg-slate-700" /></div></main>;
  if (tag.error) return <main className="max-w-6xl mx-auto p-6"><ErrorState message={(tag.error as Error).message} onRetry={() => tag.refetch()} /></main>;
  if (!tag.data) return null;
  const td = tag.data;

  return (
    <main className="max-w-6xl mx-auto p-6">
      <Breadcrumb items={[
        { label: t("tags.title"), href: "/admin/tags" },
        { label: td.normalized_name },
      ]} />

      <div className="grid grid-cols-1 md:grid-cols-[280px_1fr] gap-6">
        <aside className="space-y-4">
          <div className="card p-4">
            <h1 className="text-2xl font-bold text-gray-900 dark:text-white">#{td.normalized_name}</h1>
            {td.category && <span className="inline-block mt-2 px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400">{td.category}</span>}
            <dl className="mt-4 space-y-2 text-sm">
              <div className="flex justify-between"><dt className="text-gray-500">{t("tag_detail.work_count")}</dt><dd className="font-semibold">{td.usage_count}</dd></div>
              <div className="flex justify-between"><dt className="text-gray-500">{t("tag_detail.created")}</dt><dd className="text-xs">{new Date(td.created_at).toLocaleDateString()}</dd></div>
            </dl>
          </div>

          {td.top_creators && td.top_creators.length > 0 && (
            <div className="card p-4">
              <h2 className="text-sm font-semibold mb-3">{t("tag_detail.top_creators")}</h2>
              <div className="space-y-2">
                {td.top_creators.map((c: any) => (
                  <Link key={c.creator_id} href={`/admin/creators/${c.creator_id}`}
                    className="flex items-center justify-between rounded-md px-2 py-1.5 hover:bg-gray-50 dark:hover:bg-slate-700 transition-colors">
                    <span className="text-sm text-blue-600 hover:underline truncate">{c.creator_name}</span>
                    <span className="text-xs text-gray-400 ml-2 shrink-0">{c.work_count}</span>
                  </Link>
                ))}
              </div>
            </div>
          )}
        </aside>

        <section>
          <h2 className="text-base font-semibold mb-4">{t("tag_detail.works_with_tag", { count: works.data?.total || 0 })}</h2>
          {works.isLoading && <div className="grid grid-cols-2 md:grid-cols-3 gap-4">{Array.from({ length: 6 }).map((_, i) => <div key={i} className="h-48 animate-pulse rounded-md bg-gray-200 dark:bg-slate-700" />)}</div>}
          {works.data && works.data.items.length === 0 && <EmptyState title={t("tag_detail.no_works")} />}
          {works.data && works.data.items.length > 0 && (
            <>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
                {works.data.items.map((w: any) => (
                  <Link key={w.id} href={`/admin/works/${w.id}`} className="group overflow-hidden rounded-md border border-gray-200 bg-white hover:border-blue-300 dark:border-gray-700 dark:bg-slate-800 transition-colors">
                    <div className="aspect-[4/3] bg-gray-100 dark:bg-slate-700">
                      {w.thumbnail_asset_id ? (
                        <img src={api.mediaUrl(w.thumbnail_asset_id, "thumb")} alt={w.title || ""} className="h-full w-full object-cover" loading="lazy" />
                      ) : (
                        <div className="flex h-full items-center justify-center text-xs text-gray-400">{t("works.na")}</div>
                      )}
                    </div>
                    <div className="p-3">
                      <div className="truncate text-sm font-medium group-hover:text-blue-600 dark:group-hover:text-blue-400">{w.title || t("works.untitled")}</div>
                      {w.creator_name && <div className="text-xs text-gray-400 mt-1">{w.creator_name}</div>}
                    </div>
                  </Link>
                ))}
              </div>
              {(works.data?.total || 0) > limit && (
                <div className="flex gap-2 justify-center mt-4">
                  <button disabled={page === 0} onClick={() => setPage(p => Math.max(0, p - 1))} className="px-3 py-1 text-sm border rounded disabled:opacity-30">{t("works.prev")}</button>
                  <span className="px-3 py-1 text-sm text-gray-500">{t("works.page", { page: page + 1 })}</span>
                  <button onClick={() => setPage(p => p + 1)} disabled={(page + 1) * limit >= (works.data?.total || 0)} className="px-3 py-1 text-sm border rounded disabled:opacity-30">{t("works.next")}</button>
                </div>
              )}
            </>
          )}
        </section>
      </div>
    </main>
  );
}
