"use client";
import { useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { PageHeader, PageShell, EmptyState, ErrorState, Modal, Pagination, PermissionGuard, SmartSearchInput, TagBubbleChart } from "@/components";
import { usePermissions } from "@/lib/usePermissions";
import DomainDangerZone from "@/components/DomainDangerZone";

const CATEGORIES = ["general", "artist", "series", "character", "meta"];
const PAGE_SIZE = 100;

export default function TagsPage() {
  const t = useT();
  const qc = useQueryClient();
  const { has } = usePermissions();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const canCurate = has("curation");
  const [showCreate, setShowCreate] = useState(false);
  const [formName, setFormName] = useState("");
  const [formCat, setFormCat] = useState("general");
  const q = searchParams.get("q") || "";
  const category = searchParams.get("category") || "";
  const sortBy = searchParams.get("sort_by") === "name" ? "name" : "usage_count";
  const sortOrder = searchParams.get("sort_order") === "asc" ? "asc" : "desc";
  const page = Math.max(1, Number.parseInt(searchParams.get("page") || "1", 10) || 1);
  const updateParams = (values: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams.toString());
    for (const [key, value] of Object.entries(values)) value ? next.set(key, value) : next.delete(key);
    router.replace(`${pathname}?${next}`);
  };

  const tags = useQuery({
    queryKey: [...queryKeys.tags.all, "page", page, q, category, sortBy, sortOrder],
    queryFn: () => api.listTagsPage({ offset: (page - 1) * PAGE_SIZE, limit: PAGE_SIZE, q, category: category || undefined, sort_by: sortBy, sort_order: sortOrder }),
    placeholderData: (previous) => previous,
  });

  useEffect(() => {
    if (tags.isSuccess && !tags.isPlaceholderData && !tags.isFetching && tags.data && page > Math.max(1, Math.ceil(tags.data.total / PAGE_SIZE))) updateParams({ page: null });
  }, [page, tags.data?.total, tags.isFetching, tags.isPlaceholderData, tags.isSuccess]); // URL is the source of truth for the bounded page.

  const create = useMutation({
    mutationFn: () => api.createTag({ normalized_name: formName.trim().toLowerCase(), category: formCat || undefined }),
    onSuccess: (created) => { void qc.invalidateQueries({ queryKey: queryKeys.tags.all }); setShowCreate(false); setFormName(""); setFormCat("general"); updateParams({ q: created.normalized_name, category: created.category || null, page: null }); },
  });

  return (
    <PermissionGuard module="library">
    <PageShell>
      <PageHeader
        title={t("tags.title")}
        description={t("tags.desc")}
        primaryAction={canCurate ? (
          <button onClick={() => { setFormName(""); setFormCat("general"); setShowCreate(true); }}
            className="btn-primary">{t("tags.new")}</button>
        ) : undefined}
      />

      <div className="mb-4 grid gap-2 sm:grid-cols-3">
        <SmartSearchInput value={q} onChange={(value) => updateParams({ q: value, page: null })} placeholder={t("common.search")} />
        <select aria-label={t("tags.category_label")} className="select" value={category} onChange={(event) => updateParams({ category: event.target.value, page: null })}>
          <option value="">{t("common.all")}</option>{CATEGORIES.map((item) => <option key={item} value={item}>{item}</option>)}
        </select>
        <select aria-label={t("common.sort")} className="select" value={`${sortBy}:${sortOrder}`} onChange={(event) => { const [nextSort, nextOrder] = event.target.value.split(":"); updateParams({ sort_by: nextSort, sort_order: nextOrder, page: null }); }}>
          <option value="usage_count:desc">{t("tags.sort_usage")}</option><option value="name:asc">{t("tags.sort_name")}</option>
        </select>
      </div>

      <div data-page-primary-content>
      {tags.isFetching && !tags.isLoading && <div role="status" className="mb-2 text-xs text-muted">{t("common.refreshing")}</div>}
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
      {tags.data && !tags.data.items.length && <EmptyState title={t("tags.no_tags")} description={t("tags.no_tags_desc")} />}

      {tags.data && tags.data.items.length > 0 && (
        <div className="card p-3 sm:p-5">
          <TagBubbleChart tags={tags.data.items} ariaLabel={t("tags.title")} />
          <p className="mt-5 text-center text-xs text-muted">
            {t("tags.loaded_range", { start: tags.data.offset + 1, end: tags.data.offset + tags.data.items.length, total: tags.data.total })}
          </p>
          <Pagination page={page} pageSize={PAGE_SIZE} total={tags.data.total} onPageChange={(next) => updateParams({ page: next === 1 ? null : String(next) })} />
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

      <DomainDangerZone
        entity="tags"
        title={t("datamgmt.danger_clear_tags")}
        description={t("datamgmt.danger_clear_tags_desc")}
      />
      </div>
    </PageShell>
    </PermissionGuard>
  );
}
