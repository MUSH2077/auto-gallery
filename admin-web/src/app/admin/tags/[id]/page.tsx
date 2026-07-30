"use client";
import { useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { Breadcrumb } from "@/components/Breadcrumb";
import { ConfirmDialog, ErrorState, EmptyState, Modal, PageShell, type SlideItem } from "@/components";
import { useSlideshow } from "@/lib/useSlideshow";
import { usePermissions } from "@/lib/usePermissions";
import { useI18nFormat } from "@/lib/i18n-format";
import { quoteSearchValue } from "@/lib/search-query";

const CATEGORIES = ["general", "artist", "series", "character", "meta"];

export default function TagDetailPage() {
  const t = useT();
  const fmt = useI18nFormat();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { has } = usePermissions();
  const canCurate = has("curation");
  const params = useParams();
  const id = params.id as string;
  const [page, setPage] = useState(0);
  const [showEdit, setShowEdit] = useState(false);
  const [showDelete, setShowDelete] = useState(false);
  const [formName, setFormName] = useState("");
  const [formCategory, setFormCategory] = useState("general");
  const limit = 24;

  const tag = useQuery({
    queryKey: ["tag-detail", id],
    queryFn: () => api.getTagDetail(id),
  });

  const works = useQuery({
    queryKey: ["tag-works", id, page],
    queryFn: async () => {
      const result = await api.search(
        `type:work tag:${quoteSearchValue(tag.data?.normalized_name || "")}`,
        page * limit,
        limit,
        "works",
      );
      return result.groups.works || { total: 0, items: [] };
    },
    enabled: !!tag.data?.normalized_name,
  });

  const updateTag = useMutation({
    mutationFn: () => api.updateTag(id, {
      normalized_name: formName.trim().toLowerCase(),
      category: formCategory || undefined,
    }),
    onSuccess: async () => {
      setShowEdit(false);
      await queryClient.invalidateQueries({ queryKey: ["tag-detail", id] });
      await queryClient.invalidateQueries({ queryKey: queryKeys.tags.all });
    },
  });

  const deleteTag = useMutation({
    mutationFn: () => api.deleteTag(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.tags.all });
      router.push("/admin/tags");
    },
  });

  const slideshow = useSlideshow();
  const slideItems: SlideItem[] = (works.data?.items || [])
    .filter((w) => !!w.thumbnail_asset_id)
    .map((w) => ({ assetId: w.thumbnail_asset_id as string, workId: w.id, title: w.title, creatorName: w.creator_name }));

  if (tag.isLoading) return <PageShell><div className="animate-pulse space-y-4"><div className="h-8 w-1/3 rounded bg-subtle" /><div className="h-64 rounded bg-subtle" /></div></PageShell>;
  if (tag.error) return <PageShell><ErrorState message={(tag.error as Error).message} onRetry={() => tag.refetch()} /></PageShell>;
  if (!tag.data) return null;
  const td = tag.data;

  return (
    <PageShell>
      <Breadcrumb items={[
        { label: t("tags.title"), href: "/admin/tags" },
        { label: td.normalized_name },
      ]} />

      <div className="grid grid-cols-1 md:grid-cols-[280px_1fr] gap-6">
        <aside className="space-y-4">
          <div className="card p-4">
            <h1 className="text-2xl font-bold text-fg dark:text-white">#{td.normalized_name}</h1>
            {td.category && <span className="inline-block mt-2 px-2.5 py-0.5 rounded-full text-xs font-medium bg-accent-subtle text-accent">{td.category}</span>}
            <dl className="mt-4 space-y-2 text-sm">
              <div className="flex justify-between"><dt className="text-muted">{t("tag_detail.work_count")}</dt><dd className="font-semibold">{td.usage_count}</dd></div>
              <div className="flex justify-between"><dt className="text-muted">{t("tag_detail.created")}</dt><dd className="text-xs">{fmt.date(td.created_at)}</dd></div>
            </dl>
            {canCurate && (
              <div className="mt-4 flex gap-2 border-t border-border pt-4">
                <button
                  type="button"
                  className="btn-ghost flex-1 text-sm"
                  onClick={() => {
                    setFormName(td.normalized_name);
                    setFormCategory(td.category || "general");
                    setShowEdit(true);
                  }}
                >
                  {t("common.edit")}
                </button>
                <button type="button" className="btn-danger flex-1 text-sm" onClick={() => setShowDelete(true)}>
                  {t("common.delete")}
                </button>
              </div>
            )}
          </div>

          {td.top_creators && td.top_creators.length > 0 && (
            <div className="card p-4">
              <h2 className="text-sm font-semibold mb-3">{t("tag_detail.top_creators")}</h2>
              <div className="space-y-2">
                {td.top_creators.map((c: any) => (
                  <Link key={c.creator_id} href={`/admin/creators/${c.creator_id}`}
                    className="flex items-center justify-between rounded-md px-2 py-1.5 hover:bg-subtle dark:hover:bg-subtle transition-colors">
                    <span className="text-sm text-accent hover:underline truncate">{c.creator_name}</span>
                    <span className="text-xs text-muted ml-2 shrink-0">{c.work_count}</span>
                  </Link>
                ))}
              </div>
            </div>
          )}
        </aside>

        <section>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-base font-semibold">{t("tag_detail.works_with_tag", { count: works.data?.total || 0 })}</h2>
            {slideItems.length > 0 && (
              <button type="button" onClick={() => slideshow.open(slideItems)} className="btn-ghost">
                {t("slideshow.open")}
              </button>
            )}
          </div>
          {works.isLoading && <div className="grid grid-cols-2 md:grid-cols-3 gap-4">{Array.from({ length: 6 }).map((_, i) => <div key={i} className="h-48 animate-pulse rounded-md bg-subtle" />)}</div>}
          {works.data && works.data.items.length === 0 && <EmptyState title={t("tag_detail.no_works")} />}
          {works.data && works.data.items.length > 0 && (
            <>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
                {works.data.items.map((w: any) => (
                  <Link key={w.id} href={`/admin/works/${w.id}`} className="group overflow-hidden rounded-md border border-border bg-white hover:border-accent/30 dark:border-border dark:bg-subtle transition-colors">
                    <div className="aspect-[4/3] bg-subtle">
                      {w.thumbnail_asset_id ? (
                        <img src={api.mediaUrl(w.thumbnail_asset_id, "thumb")} alt={w.title || ""} className="h-full w-full object-cover" loading="lazy" decoding="async" />
                      ) : (
                        <div className="flex h-full items-center justify-center text-xs text-muted">{t("works.na")}</div>
                      )}
                    </div>
                    <div className="p-3">
                      <div className="truncate text-sm font-medium group-hover:text-accent dark:group-hover:text-accent">{w.title || t("works.untitled")}</div>
                      {w.creator_name && <div className="text-xs text-muted mt-1">{w.creator_name}</div>}
                    </div>
                  </Link>
                ))}
              </div>
              {(works.data?.total || 0) > limit && (
                <div className="flex gap-2 justify-center mt-4">
                  <button disabled={page === 0} onClick={() => setPage(p => Math.max(0, p - 1))} className="px-3 py-1 text-sm border rounded disabled:opacity-30">{t("works.prev")}</button>
                  <span className="px-3 py-1 text-sm text-muted">{t("works.page", { page: page + 1 })}</span>
                  <button onClick={() => setPage(p => p + 1)} disabled={(page + 1) * limit >= (works.data?.total || 0)} className="px-3 py-1 text-sm border rounded disabled:opacity-30">{t("works.next")}</button>
                </div>
              )}
            </>
          )}
        </section>
      </div>
      <Modal open={showEdit} onClose={() => setShowEdit(false)} title={t("tags.edit_title")}>
        <div className="space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium">{t("tags.name_label")}</label>
            <input value={formName} onChange={(event) => setFormName(event.target.value)} className="input w-full" />
          </div>
          <div>
            <label className="mb-1 block text-sm font-medium">{t("tags.category_label")}</label>
            <select value={formCategory} onChange={(event) => setFormCategory(event.target.value)} className="select w-full">
              {CATEGORIES.map((category) => <option key={category} value={category}>{category}</option>)}
            </select>
          </div>
          {updateTag.error && <p className="text-sm text-danger">{(updateTag.error as Error).message}</p>}
          <div className="flex justify-end gap-2">
            <button type="button" className="btn-ghost" onClick={() => setShowEdit(false)}>{t("tags.cancel")}</button>
            <button
              type="button"
              className="btn-primary"
              disabled={!formName.trim() || updateTag.isPending}
              onClick={() => updateTag.mutate()}
            >
              {updateTag.isPending ? t("tags.saving") : t("tags.save")}
            </button>
          </div>
        </div>
      </Modal>
      <ConfirmDialog
        open={showDelete}
        title={t("tags.delete_title")}
        message={t("tags.delete_msg")}
        onConfirm={() => deleteTag.mutate()}
        onCancel={() => setShowDelete(false)}
        isPending={deleteTag.isPending}
        error={(deleteTag.error as Error)?.message}
      />
      {slideshow.node}
    </PageShell>
  );
}
