"use client";

import { useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { usePermissions } from "@/lib/usePermissions";
import { useI18nFormat } from "@/lib/i18n-format";
import { quoteSearchValue } from "@/lib/search-query";
import ErrorState from "@/components/ErrorState";
import { WorkMediaThumbnail } from "@/components/MediaAssetRenderer";
import { ToolbarSelect } from "@/features/gallery-ui/Controls";
import type { SlideItem } from "@/components/SlideshowPlayer";
import { useSlideshow } from "@/lib/useSlideshow";
import { BusinessPage, BusinessModal, BusinessConfirm, BusinessPermissionGuard } from "@/features/gallery-ui/BusinessPage";


const CATEGORIES = ["general", "artist", "series", "character", "meta"];
const LIMIT = 24;

function TagDetailContent() {
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

  const tag = useQuery({ queryKey: ["tag-detail", id], queryFn: () => api.getTagDetail(id) });
  const works = useQuery({
    queryKey: ["tag-works", id, page],
    queryFn: async () => {
      const result = await api.search("type:work tag:" + quoteSearchValue(tag.data?.normalized_name || ""), page * LIMIT, LIMIT, "works");
      return result.groups.works || { total: 0, items: [] };
    },
    enabled: !!tag.data?.normalized_name,
  });
  const updateTag = useMutation({
    mutationFn: () => api.updateTag(id, { normalized_name: formName.trim().toLowerCase(), category: formCategory || undefined }),
    onSuccess: async () => { setShowEdit(false); await queryClient.invalidateQueries({ queryKey: ["tag-detail", id] }); await queryClient.invalidateQueries({ queryKey: queryKeys.tags.all }); },
  });
  const deleteTag = useMutation({
    mutationFn: () => api.deleteTag(id),
    onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: queryKeys.tags.all }); router.push("/admin/tags"); },
  });
  const slideshow = useSlideshow();
  const slideItems: SlideItem[] = (works.data?.items || []).filter(w => !!w.thumbnail_asset_id).map(w => ({ assetId: w.thumbnail_asset_id as string, workId: w.id, title: w.title, creatorName: w.creator_name }));

  if (tag.isLoading) return <div className="gallery-tags"><BusinessPage title={t("tags.title")}><div className="tag-loading" role="status">{t("common.loading")}</div></BusinessPage></div>;
  if (tag.error) return <div className="gallery-tags"><BusinessPage title={t("tags.title")}><div className="tag-error"><ErrorState message={(tag.error as Error).message} onRetry={() => void tag.refetch()} /></div></BusinessPage></div>;
  if (!tag.data) return null;
  const td = tag.data;

  return (
    <div className="gallery-tags">
      <BusinessPage title={"#" + td.normalized_name} actions={canCurate ? <div className="tags-heading-actions"><button type="button" className="tags-button" onClick={() => { setFormName(td.normalized_name); setFormCategory(td.category || "general"); setShowEdit(true); }}>{t("common.edit")}</button><button type="button" className="tags-button danger" onClick={() => setShowDelete(true)}>{t("common.delete")}</button></div> : undefined}>
        <Link className="tag-detail-back" href="/admin/tags">← {t("tags.title")}</Link>
        <div className="tag-detail-layout">
          <aside className="tag-detail-aside">

            {td.category ? <span className="tag-detail-category">{td.category}</span> : null}
            <dl className="tag-facts"><div><dt>{t("tag_detail.work_count")}</dt><dd>{td.usage_count}</dd></div><div><dt>{t("tag_detail.created")}</dt><dd>{fmt.date(td.created_at)}</dd></div></dl>
            {td.top_creators?.length ? <section className="tag-creators"><h2>{t("tag_detail.top_creators")}</h2><div className="tag-creators-list">{td.top_creators.map((creator) => <Link key={creator.creator_id} className="tag-creator" href={"/admin/creators/" + encodeURIComponent(creator.creator_id)}><span className="tag-link">{creator.creator_name}</span><span className="tag-creator-count">{creator.work_count}</span></Link>)}</div></section> : null}
          </aside>
          <section className="tag-works">
            <div className="tag-works-head"><h2>{t("tag_detail.works_with_tag", { count: works.data?.total || 0 })}</h2>{slideItems.length ? <button type="button" className="tags-button" onClick={() => slideshow.open(slideItems)}>{t("slideshow.open")}</button> : null}</div>
            {works.isLoading ? <div className="tag-loading" role="status">{t("common.loading")}</div> : null}
            {works.error ? <div className="tag-error"><ErrorState message={(works.error as Error).message} onRetry={() => void works.refetch()} /></div> : null}
            {works.data && !works.data.items.length ? <div className="tags-empty"><h2>{t("tag_detail.no_works")}</h2></div> : null}
            {works.data && works.data.items.length ? <>
              <div className="tag-work-grid">{works.data.items.map((work) => <Link key={work.id} className="tag-work" href={"/admin/works/" + encodeURIComponent(work.id)}><div className="tag-work-media">{work.thumbnail_asset_id ? <WorkMediaThumbnail assetId={work.thumbnail_asset_id} hasVideo={work.has_video} alt={work.title || ""} className="h-full w-full object-cover" /> : null}</div><div className="tag-work-title">{work.title || t("works.untitled")}</div>{work.creator_name ? <div className="tag-work-creator">{work.creator_name}</div> : null}</Link>)}</div>
              {works.data.total > LIMIT ? <div className="tag-work-pagination"><button type="button" className="tags-button" disabled={page === 0} onClick={() => setPage(current => Math.max(0, current - 1))}>{t("common.prev")}</button><span className="tags-button">{t("works.page", { page: page + 1 })}</span><button type="button" className="tags-button" disabled={(page + 1) * LIMIT >= works.data.total} onClick={() => setPage(current => current + 1)}>{t("common.next")}</button></div> : null}
            </> : null}
          </section>
        </div>
      </BusinessPage>
      <BusinessModal open={showEdit} onClose={() => setShowEdit(false)} title={t("tags.edit_title")}>
        <form className="tags-form" onSubmit={event => { event.preventDefault(); if (formName.trim() && !updateTag.isPending) updateTag.mutate(); }}>
          <div className="tags-form-field"><label htmlFor="tag-edit-name">{t("tags.name_label")}</label><input id="tag-edit-name" value={formName} onChange={event => setFormName(event.target.value)} /></div>
          <div className="tags-form-field"><span>{t("tags.category_label")}</span><ToolbarSelect label={t("tags.category_label")} value={formCategory} onValueChange={setFormCategory} options={CATEGORIES.map(category => ({ value: category, label: category }))} /></div>
          {updateTag.error ? <p className="tags-form-error" role="alert">{(updateTag.error as Error).message}</p> : null}
          <div className="tags-form-actions"><button type="button" className="tags-button" onClick={() => setShowEdit(false)}>{t("tags.cancel")}</button><button type="submit" className="tags-button primary" disabled={!formName.trim() || updateTag.isPending}>{updateTag.isPending ? t("tags.saving") : t("tags.save")}</button></div>
        </form>
      </BusinessModal>
      <BusinessConfirm open={showDelete} title={t("tags.delete_title")} message={t("tags.delete_msg")} onConfirm={() => deleteTag.mutate()} onCancel={() => setShowDelete(false)} isPending={deleteTag.isPending} error={(deleteTag.error as Error | null)?.message} />
      {slideshow.node}
    </div>
  );
}

export default function TagDetailPage() { return <BusinessPermissionGuard module="library"><TagDetailContent /></BusinessPermissionGuard>; }
