"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell, EmptyState, ErrorState, Modal, PermissionGuard, TagBubbleChart } from "@/components";
import { usePermissions } from "@/lib/usePermissions";

const CATEGORIES = ["general", "artist", "series", "character", "meta"];

export default function TagsPage() {
  const t = useT();
  const qc = useQueryClient();
  const { has } = usePermissions();
  const canCurate = has("curation");
  const [page, setPage] = useState(0);
  const [showCreate, setShowCreate] = useState(false);
  const [formName, setFormName] = useState("");
  const [formCat, setFormCat] = useState("general");
  const limit = 50;

  const tags = useQuery({
    queryKey: [...queryKeys.tags.all, page],
    queryFn: () => api.listTags(page * limit, limit),
  });

  const create = useMutation({
    mutationFn: () => api.createTag({ normalized_name: formName.trim().toLowerCase(), category: formCat || undefined }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: queryKeys.tags.all }); setShowCreate(false); setFormName(""); setFormCat("general"); },
  });

  return (
    <PermissionGuard module="library">
    <PageShell>
      <PageHeader title={t("tags.title")} description={tags.data?.length ? t("common.page").replace("{page}", String(page + 1)) : t("tags.desc")}>
        {canCurate && (
          <button onClick={() => { setFormName(""); setFormCat("general"); setShowCreate(true); }}
            className="btn-primary">{t("tags.new")}</button>
        )}
      </PageHeader>

      {tags.isLoading && (
        <div className="flex min-h-80 flex-wrap items-center justify-center gap-3">
          {Array.from({ length: 20 }).map((_, i) => {
            const size = 44 + (i % 5) * 12;
            return <div key={i} style={{ width: size, height: size }}
              className="animate-pulse rounded-full bg-subtle dark:bg-subtle" />;
          })}
        </div>
      )}
      {tags.error && <ErrorState message={(tags.error as Error).message} onRetry={() => tags.refetch()} />}
      {tags.data && !tags.data.length && <EmptyState title={t("tags.no_tags")} description={t("tags.no_tags_desc")} />}

      {tags.data && tags.data.length > 0 && (
        <div className="rounded-md border border-border bg-white p-3 dark:border-border dark:bg-surface sm:p-5">
          <TagBubbleChart tags={tags.data} ariaLabel={t("tags.title")} />
          <p className="mt-5 text-center text-xs text-muted">
            {t("tags.total").replace("{count}", String(tags.data.length))}
          </p>
        </div>
      )}

      <Modal open={showCreate} onClose={() => setShowCreate(false)} title={t("tags.create_title")}>
        <div className="space-y-4">
          <div><label className="block text-sm font-medium mb-1">{t("tags.name_label")}</label>
            <input value={formName} onChange={(e) => setFormName(e.target.value)}
              className="input w-full" placeholder={t("tags.name_placeholder")} /></div>
          <div><label className="block text-sm font-medium mb-1">{t("tags.category_label")}</label>
            <select value={formCat} onChange={(e) => setFormCat(e.target.value)} className="select w-full">
              {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select></div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setShowCreate(false)} className="btn-ghost">{t("tags.cancel")}</button>
            <button onClick={() => create.mutate()} disabled={!formName.trim() || create.isPending}
              className="btn-primary">
              {create.isPending ? t("tags.creating") : t("tags.create")}
            </button>
          </div>
          {create.error && <p className="text-red-600 text-sm">{(create.error as Error).message}</p>}
        </div>
      </Modal>

      {/* Pagination */}
      {tags.data && tags.data.length > 0 && (
        <div className="flex gap-2 justify-center mt-4">
          <button disabled={page === 0} onClick={() => setPage(page - 1)}
            className="btn-ghost px-3 py-1 text-sm disabled:opacity-30">{t("common.prev")}</button>
          <span className="px-3 py-1 text-sm text-muted">{t("common.page").replace("{page}", String(page + 1))}</span>
          <button onClick={() => setPage(page + 1)} disabled={!tags.data || tags.data.length < limit}
            className="btn-ghost px-3 py-1 text-sm disabled:opacity-30">{t("common.next")}</button>
        </div>
      )}
    </PageShell>
    </PermissionGuard>
  );
}
