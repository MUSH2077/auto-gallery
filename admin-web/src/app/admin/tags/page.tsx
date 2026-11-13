"use client";

import dynamic from "next/dynamic";
import { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useT } from "@/lib/i18n";

import ErrorState from "@/components/ErrorState";
import { ToolbarSelect } from "@/features/gallery-ui/Controls";
import { BusinessPage, BusinessModal, BusinessDangerZone, BusinessPermissionGuard } from "@/features/gallery-ui/BusinessPage";
import { usePermissions } from "@/lib/usePermissions";


function TagChartLoading() { const t = useT(); return <div className="tag-loading" role="status">{t("common.loading")}</div>; }
const TagBubbleChart = dynamic(() => import("@/components/TagBubbleChart"), { ssr: false, loading: () => <TagChartLoading /> });
const CATEGORIES = ["general", "artist", "series", "character", "meta"];
const PAGE_SIZE = 100;

function TagsContent() {
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
    router.replace(pathname + (next.toString() ? "?" + next.toString() : ""));
  };
  const tags = useQuery({
    queryKey: [...queryKeys.tags.all, "page", page, q, category, sortBy, sortOrder],
    queryFn: () => api.listTagsPage({ offset: (page - 1) * PAGE_SIZE, limit: PAGE_SIZE, q, category: category || undefined, sort_by: sortBy, sort_order: sortOrder }),
    placeholderData: previous => previous,
  });
  useEffect(() => {
    if (tags.isSuccess && !tags.isPlaceholderData && !tags.isFetching && tags.data && page > Math.max(1, Math.ceil(tags.data.total / PAGE_SIZE))) updateParams({ page: null });
  }, [page, tags.data?.total, tags.isFetching, tags.isPlaceholderData, tags.isSuccess]);
  const create = useMutation({
    mutationFn: () => api.createTag({ normalized_name: formName.trim().toLowerCase(), category: formCat || undefined }),
    onSuccess: created => {
      void qc.invalidateQueries({ queryKey: queryKeys.tags.all });
      setShowCreate(false); setFormName(""); setFormCat("general");
      updateParams({ q: created.normalized_name, category: created.category || null, page: null });
    },
  });
  return (
    <div className="gallery-tags">
      <BusinessPage
        title={t("tags.title")}
        description={t("tags.desc")}
        actions={canCurate ? <button type="button" className="tags-button primary" onClick={() => { setFormName(""); setFormCat("general"); setShowCreate(true); }}>{t("tags.new").replace(/^\+\s*/, "")}</button> : undefined}
      >
        <div className="tags-toolbar" aria-label={t("tags.title")}>
          <input className="tags-search" type="search" aria-label={t("tags.search")} placeholder={t("tags.search")} value={q} onChange={event => updateParams({ q: event.target.value, page: null })} />
          <ToolbarSelect label={t("tags.category_label")} value={category} onValueChange={value => updateParams({ category: value || null, page: null })} options={[{ value: "", label: t("common.all") }, ...CATEGORIES.map(value => ({ value, label: value }))]} />
          <ToolbarSelect label={t("common.sort")} value={sortBy + ":" + sortOrder} onValueChange={value => { const [nextSort, nextOrder] = value.split(":"); updateParams({ sort_by: nextSort, sort_order: nextOrder, page: null }); }} options={[{ value: "usage_count:desc", label: t("tags.sort_usage") }, { value: "name:asc", label: t("tags.sort_name") }]} />
        </div>
        {tags.isFetching && !tags.isLoading ? <div className="tags-summary" role="status">{t("common.refreshing")}</div> : null}
        {tags.isLoading ? <div className="tag-loading" role="status">{t("common.loading")}</div> : null}
        {tags.error ? <div className="tag-error"><ErrorState message={(tags.error as Error).message} onRetry={() => void tags.refetch()} /></div> : null}
        {tags.data && !tags.data.items.length ? <div className="tags-empty"><h2>{q ? t("tags.no_match", { query: q }) : t("tags.no_tags")}</h2><p>{t("tags.no_tags_desc")}</p></div> : null}
        {tags.data && tags.data.items.length > 0 ? <>
          <div className="tags-summary"><strong>{t("tags.loaded_range", { start: tags.data.offset + 1, end: tags.data.offset + tags.data.items.length, total: tags.data.total })}</strong><span>{t("tags.zoom_hint")}</span></div>
          <div className="tags-chart-frame"><TagBubbleChart tags={tags.data.items} ariaLabel={t("tags.title")} /></div>
          <div className="tags-table-wrap">
            <table className="tags-table"><thead><tr><th>{t("tags.name_label").replace(/\s*\*$/, "")}</th><th>{t("tags.category_label")}</th><th>{t("tag_detail.work_count")}</th></tr></thead><tbody>
              {tags.data.items.map(tag => <tr key={tag.id}><td><Link className="tag-link" href={"/admin/tags/" + encodeURIComponent(tag.id)}>{tag.normalized_name}</Link></td><td className="tag-category">{tag.category || "—"}</td><td className="tag-count">{tag.usage_count}</td></tr>)}
            </tbody></table>
          </div>
          <nav className="tags-pagination" aria-label={t("common.pagination")}>
            <button type="button" className="tags-button" disabled={page <= 1} onClick={() => updateParams({ page: page <= 2 ? null : String(page - 1) })}>{t("common.prev")}</button>
            <span>{t("common.page_of", { page, total: Math.max(1, Math.ceil(tags.data.total / PAGE_SIZE)) })}</span>
            <button type="button" className="tags-button" disabled={page >= Math.ceil(tags.data.total / PAGE_SIZE)} onClick={() => updateParams({ page: String(page + 1) })}>{t("common.next")}</button>
          </nav>
        </> : null}
        <BusinessDangerZone entity="tags" title={t("datamgmt.danger_clear_tags")} description={t("datamgmt.danger_clear_tags_desc")} />
      </BusinessPage>
      <BusinessModal open={showCreate} onClose={() => setShowCreate(false)} title={t("tags.create_title")}>
        <form className="tags-form" onSubmit={event => { event.preventDefault(); if (formName.trim() && !create.isPending) create.mutate(); }}>
          <div className="tags-form-field"><label htmlFor="tag-name">{t("tags.name_label")}</label><input id="tag-name" value={formName} onChange={event => setFormName(event.target.value)} placeholder={t("tags.name_placeholder")} /></div>
          <div className="tags-form-field"><span>{t("tags.category_label")}</span><ToolbarSelect label={t("tags.category_label")} value={formCat} onValueChange={setFormCat} options={CATEGORIES.map(c => ({ value: c, label: c }))} /></div>
          {create.error ? <p className="tags-form-error" role="alert">{(create.error as Error).message}</p> : null}
          <div className="tags-form-actions"><button type="button" className="tags-button" onClick={() => setShowCreate(false)}>{t("tags.cancel")}</button><button type="submit" className="tags-button primary" disabled={!formName.trim() || create.isPending}>{create.isPending ? t("tags.creating") : t("tags.create")}</button></div>
        </form>
      </BusinessModal>
    </div>
  );
}
export default function TagsPage() { return <BusinessPermissionGuard module="library"><TagsContent /></BusinessPermissionGuard>; }
