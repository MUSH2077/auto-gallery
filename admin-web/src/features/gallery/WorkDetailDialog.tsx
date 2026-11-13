"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useIsMutating, useMutation, useMutationState, useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useWorkDetails } from "@/features/works/useWorkDetails";
import { useWorkMedia } from "@/features/works/useWorkMedia";
import { useAppearanceSettings } from "@/lib/appearance";
import { AssetViewer } from "@/components/MediaAssetRenderer";
import type { AssetData } from "@/components/WorkViewerParts";
import { AssetFilmstrip } from "@/components/work-interactions";
import ErrorState from "@/components/ErrorState";
import { useGalleryFormat } from "@/features/gallery-ui/format";
import { Modal } from "@/features/gallery-ui/Modal";
import { WorkDetailView, type DetailFact } from "@/features/gallery-ui/DetailViews";
import { Icon } from "@/features/gallery-ui/Icon";
import { useGalleryCopy } from "@/features/gallery-ui/copy";
import { quoteSearchValue, searchUrl } from "@/lib/search-query";
import { adminRoutes } from "@/lib/adminRoutes";

function safeExternalUrl(value?: string | null) {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

export default function WorkDetailDialog({ id, onClose, operationBusy = false }: {
  id: string;
  operationBusy?: boolean;
  onClose: () => void;
}) {
  const c = useWorkDetails(id);
  const m = useWorkMedia(id);
  const fmt = useGalleryFormat();
  const { settings } = useAppearanceSettings();
  const t = c.t;
  const { copy } = useGalleryCopy();
  const w = c.work.data;
  const isCurating = useIsMutating({ mutationKey: ["works", "curation", id] }) > 0;
  const curationStates = useMutationState({
    filters: { mutationKey: ["works", "curation", id], exact: true },
    select: mutation => ({ status: mutation.state.status, error: mutation.state.error }),
  });
  const latestCuration = curationStates[curationStates.length - 1];
  const sharedCurationError = latestCuration?.status === "error" ? latestCuration.error : null;
  const sourceRecords = c.sources.data || [];
  const primarySource = sourceRecords[0];
  const source = primarySource?.source;
  const rawMetadata = primarySource?.raw_metadata || {};
  const rawTags = Array.isArray(rawMetadata.tags) ? rawMetadata.tags.filter((tag): tag is string => typeof tag === "string") : [];
  const workTags = c.workTags.data || [];
  const history = useQuery({
    queryKey: queryKeys.curation.subject("work", id),
    queryFn: () => api.listCurationCommits({ subject_type: "work", subject_id: id, limit: 20 }),
    enabled: c.canCurate,
  });
  const creatorWorks = useQuery({
    queryKey: ["gallery", "more-from-creator", c.work.data?.creator_id],
    queryFn: () => api.search(`creator:${c.work.data?.creator_id} sort:posted-desc`, 0, 6, "works"),
    enabled: Boolean(c.work.data?.creator_id),
    staleTime: 30_000,
  });
  const [curationConfirm, setCurationConfirm] = useState<"trash" | "restore" | "purge" | null>(null);
  const [curationError, setCurationError] = useState<string | null>(null);
  const curationToken = useRef(0);
  const wheelDelta = useRef(0);

  const purgeWork = useMutation({
    mutationKey: ["works", "curation", id],
    mutationFn: () => api.purgeWorks([id], "Purge one trashed work"),
    onSuccess: () => {
      void c.qc.invalidateQueries({ queryKey: ["works"] });
      void c.qc.invalidateQueries({ queryKey: ["curation"] });
    },
  });

  const requestCuration = () => {
    if (!w || operationBusy || isCurating || c.toggleFavorite.isPending) return;
    curationToken.current += 1;
    setCurationError(null);
    setCurationConfirm(w.curation_state?.visibility === "trashed" ? "restore" : "trash");
  };
  const requestPurge = () => {
    if (!w || !c.has("admin") || operationBusy || isCurating || purgeWork.isPending) return;
    curationToken.current += 1;
    setCurationError(null);
    setCurationConfirm("purge");
  };
  const closeCuration = () => {
    curationToken.current += 1;
    setCurationConfirm(null);
    setCurationError(null);
  };
  const runCuration = async () => {
    if (!curationConfirm || operationBusy || isCurating || c.qc.isMutating({ mutationKey: ["works", "curation", id] }) > 0) return;
    const current = curationConfirm;
    const token = curationToken.current;
    if (current !== "purge") setCurationConfirm(null);
    try {
      if (current === "purge") await purgeWork.mutateAsync();
      else await c.curateWork.mutateAsync(current);
      if (token === curationToken.current) {
        setCurationConfirm(null);
        if (current === "purge") onClose();
      }
    } catch (error) {
      if (token === curationToken.current) setCurationError(error instanceof Error ? error.message : String(error));
    }
  };
  const toggleFavorite = () => {
    if (!operationBusy && !isCurating && !curationConfirm) c.toggleFavorite.toggle();
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || curationConfirm || m.fullAsset || (event.key !== "ArrowLeft" && event.key !== "ArrowRight")) return;
      const dialogs = Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"][aria-modal="true"], dialog[open]'));
      const top = dialogs[dialogs.length - 1];
      if (!top?.classList.contains("detail-sheet")) return;
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, video, button, a, [role='slider']")) return;
      if (!m.totalPages) return;
      event.preventDefault();
      m.setActiveIndex(current => Math.max(0, Math.min(m.totalPages - 1, current + (event.key === "ArrowLeft" ? -1 : 1))));
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [curationConfirm, m.fullAsset, m.setActiveIndex, m.totalPages]);

  useEffect(() => {
    const viewer = m.viewerRef.current;
    if (!viewer || m.totalPages <= 1) return;
    const onWheel = (event: WheelEvent) => {
      if ((event.target as HTMLElement).closest("video")) return;
      wheelDelta.current += event.deltaY;
      if (Math.abs(wheelDelta.current) < 80) return;
      event.preventDefault();
      const delta = wheelDelta.current > 0 ? 1 : -1;
      wheelDelta.current = 0;
      m.setActiveIndex(current => Math.max(0, Math.min(m.totalPages - 1, current + delta)));
    };
    viewer.addEventListener("wheel", onWheel, { passive: false });
    return () => viewer.removeEventListener("wheel", onWheel);
  }, [w?.id, m.setActiveIndex, m.totalPages]);

  if (!w) {
    return <Modal title={t("gallery.workDetail")} onClose={onClose} sheet>
      {c.work.isPending ? <div className="state-view" role="status">{t("common.loading")}</div> :
        c.work.isError ? <ErrorState error={c.work.error} onRetry={() => void c.work.refetch()} /> : null}
    </Modal>;
  }

  const activeAsset = m.assets.data?.[m.activeIndex];
  const media = <div ref={m.viewerRef} className="detail-media-viewer" data-blurred={Boolean(w.is_nsfw && settings.blurNsfw)}>
    {m.assets.isPending ? <p className="state-view" role="status">{t("common.loading")}</p> :
      m.assets.isError ? <ErrorState error={m.assets.error} onRetry={() => void m.assets.refetch()} /> :
      activeAsset ? <>
        <AssetViewer key={activeAsset.id} workId={id} asset={activeAsset} onOpenImage={() => m.setFullAsset(activeAsset)} />
        {m.totalPages > 1 ? <div className="asset-navigation">
          <button type="button" className="button" disabled={m.activeIndex === 0} onClick={() => m.setActiveIndex(index => Math.max(0, index - 1))}>{t("gallery.previousAsset")}</button>
          <span>{m.activeIndex + 1} / {m.totalPages}</span>
          <button type="button" className="button" disabled={m.activeIndex + 1 >= m.totalPages} onClick={() => m.setActiveIndex(index => Math.min(m.totalPages - 1, index + 1))}>{t("gallery.nextAsset")}</button>
        </div> : null}
      </> : <p className="state-view">{t("work_detail.no_assets")}</p>}
  </div>;

  const sourceLabel = source === "pixiv" ? "Pixiv" : source === "manual" ? t("gallery.localImport") : source || "—";
  const postedDate = (typeof rawMetadata.create_date === "string" ? rawMetadata.create_date : undefined) || primarySource?.posted_at || w.posted_at;
  const dimensions = typeof rawMetadata.width === "number" && typeof rawMetadata.height === "number" ? `${rawMetadata.width} × ${rawMetadata.height}` : null;
  const rawTools = Array.isArray(rawMetadata.tools) ? rawMetadata.tools.filter((tool): tool is string => typeof tool === "string") : [];
  const visibleCreatorWorks = creatorWorks.data?.groups?.works?.items?.filter(item => item.id !== id).slice(0, 4) || [];
  const facts: DetailFact[] = [
    { label: t("gallery.source"), value: sourceLabel },
    { label: t("gallery.importDate"), value: fmt.date(w.created_at) },
    { label: t("gallery.resources"), value: t("gallery.resourceCount", { count: w.asset_count }) },
    { label: t("gallery.content"), value: <>{t(w.is_nsfw ? "gallery.sensitiveContent" : "gallery.normalContent")}{w.is_ai_generated ? `，${t("gallery.ai")}` : ""}</> },
  ];
  const extraDetails = <details className="detail-more-information"><summary>{t("gallery.moreWorkInformation")}</summary><div className="detail-extra-groups">
    <details className="detail-extra-group">
      <summary>{t("work_detail.description")}</summary>
      <div className="detail-extra-body">
        <dl>
          <div><dt>{t("work_detail.work_id")}</dt><dd translate="no">{primarySource?.source_work_id || id}</dd></div>
          <div><dt>{t("work_detail.posted")}</dt><dd>{postedDate ? fmt.date(postedDate) : t("work_detail.unknown")}</dd></div>
          <div><dt>{t("work_detail.dimensions")}</dt><dd>{dimensions || "—"}</dd></div>
          <div><dt>{t("work_detail.pages")}</dt><dd>{typeof rawMetadata.page_count === "number" ? rawMetadata.page_count : w.asset_count || 1}</dd></div>
        </dl>
        <p className="detail-description">{w.description || primarySource?.description || "—"}</p>
      </div>
    </details>
    <details className="detail-extra-group">
      <summary>{t("work_detail.tags")}{workTags.length || rawTags.length ? ` (${workTags.length + rawTags.length})` : ""}</summary>
      <div className="detail-extra-body detail-chip-list">
        {c.workTags.isPending ? <span role="status">{t("common.loading")}</span> : c.workTags.isError ? <span role="alert">{t("work_detail.tags_load_error")} <button type="button" className="text-button" onClick={() => void c.workTags.refetch()}>{t("common.retry")}</button></span> : null}
        {!c.workTags.isPending && !c.workTags.isError && workTags.map(tag => <Link key={tag.id} href={searchUrl("/admin/works", `type:work tag:${quoteSearchValue(tag.normalized_name)}`)} className="text-link">{tag.normalized_name}{tag.category ? ` (${tag.category})` : ""}</Link>)}
        {rawTags.map((tag, index) => <Link key={`${tag}-${index}`} href={searchUrl("/admin/works", `type:work tag:${quoteSearchValue(tag)}`)} className="text-link">{tag}</Link>)}
        {!workTags.length && !rawTags.length ? <span className="muted">—</span> : null}
      </div>
    </details>
    {rawTools.length ? <details className="detail-extra-group"><summary>{t("work_detail.tools")}</summary><div className="detail-extra-body">{rawTools.join(" · ")}</div></details> : null}
    <details className="detail-extra-group">
      <summary>{t("work_detail.source_records", { count: sourceRecords.length })}</summary>
      <div className="detail-extra-body detail-record-list">
        {c.sources.isPending ? <span role="status">{t("common.loading")}</span> : c.sources.isError ? <span role="alert">{t("work_detail.sources_load_error")} <button type="button" className="text-button" onClick={() => void c.sources.refetch()}>{t("common.retry")}</button></span> : sourceRecords.length ? sourceRecords.map(record => {
          const externalUrl = safeExternalUrl(record.source_url);
          return <div key={record.id}>
            <strong>{record.source}</strong>{record.source_work_id ? <span translate="no"> · {record.source_work_id}</span> : null}
            {externalUrl ? <><br /><a className="text-link" href={externalUrl} target="_blank" rel="noopener noreferrer">{t("work_detail.view_on", { source: record.source })}</a></> : record.source_url ? <><br /><span className="muted">{record.source_url}</span></> : null}
            {record.title ? <><br /><span>{record.title}</span></> : null}
          </div>;
        }) : <span className="muted">{t("work_detail.no_source_records")}</span>}
      </div>
    </details>
    {c.hasPixivSource ? <details className="detail-extra-group"><summary>{t("work_detail.pixiv_live_state")}</summary><div className="detail-extra-body">
      {c.remoteState.isPending ? t("work_detail.pixiv_live_loading") : c.remoteState.isError ? <span>{t("work_detail.pixiv_live_unavailable")} <button type="button" className="text-button" onClick={() => void c.remoteState.refetch()}>{t("common.retry")}</button></span> : c.remoteState.data ? <div className="detail-record-list"><span>{fmt.number(c.remoteState.data.total_views)} {t("work_detail.live_views")} · {fmt.number(c.remoteState.data.total_bookmarks)} {t("work_detail.live_bookmarks")}</span><span>{c.remoteState.data.is_bookmarked ? t("work_detail.pixiv_bookmarked") : t("work_detail.pixiv_not_bookmarked")}</span><span className="muted">{t("work_detail.remote_fetched")} {fmt.date(c.remoteState.data.fetched_at)}</span></div> : t("work_detail.pixiv_live_unavailable")}
    </div></details> : null}
    {m.totalPages > 1 && m.assets.data ? <details className="detail-extra-group"><summary>{t("work_detail.pages_section", { count: m.totalPages })}</summary><div className="detail-extra-body"><AssetFilmstrip assets={m.assets.data} activeIndex={m.activeIndex} onSelect={m.setActiveIndex} className="detail-asset-filmstrip" /></div></details> : null}
    {c.canCurate ? <details className="detail-extra-group">
      <summary>{t("work_detail.history")}</summary>
      <div className="detail-extra-body detail-record-list">
        {history.isPending ? <span role="status">{t("common.loading")}</span> : history.isError ? <span role="alert">{t("work_detail.history_load_error")} <button type="button" className="text-button" onClick={() => void history.refetch()}>{t("common.retry")}</button></span> : history.data?.items.length ? <>
          {history.data.items.slice(0, 5).map(commit => <div key={commit.id}><strong>{commit.message}</strong><br /><span className="muted">{fmt.date(commit.occurred_at)} · {commit.trigger}</span></div>)}
          {history.data.total > 5 ? <Link className="text-link" href={`${adminRoutes.curation}?subject_type=work&subject_id=${encodeURIComponent(id)}`}>{t("work_detail.open_curation")}</Link> : null}
        </> : <span className="muted">{t("work_detail.no_history")}</span>}
      </div>
    </details> : null}
    {visibleCreatorWorks.length ? <details className="detail-extra-group"><summary>{t("work_detail.more_from_creator")}</summary><div className="detail-extra-body detail-record-list">{visibleCreatorWorks.map(item => <Link key={item.id} className="text-link" href={`/admin/works/${encodeURIComponent(item.id)}`}>{item.title || t("works.untitled")}</Link>)}<Link className="text-link" href={searchUrl("/admin/works", `creator:${w.creator_id} sort:posted-desc`)}>{t("work_detail.view_all_from_creator")}</Link></div></details> : null}
  </div></details>;

  const canAdmin = c.has("admin");
  const isFavorite = c.toggleFavorite.optimistic ?? Boolean(w.is_favorite);
  const detailActions = c.canCurate ? w.curation_state?.visibility === "trashed" ? <>
    <button type="button" className="button primary" disabled={operationBusy || isCurating} aria-busy={isCurating || undefined} onClick={requestCuration}>{t("works.restore")}</button>
    {canAdmin ? <button type="button" className="text-button danger-text" disabled={operationBusy || isCurating} onClick={requestPurge}>{t("deletion.permanent_title")}</button> : null}
  </> : <>
    <button type="button" className={"button detail-favorite-toggle" + (isFavorite ? " is-favorite" : "")} aria-label={t(isFavorite ? "gallery.unfavoriteWork" : "gallery.favoriteWork")} aria-pressed={isFavorite} aria-busy={c.toggleFavorite.isPending || undefined} disabled={operationBusy || isCurating} onClick={toggleFavorite}>
      <Icon name="heart" />{isFavorite ? copy("saved") : t("gallery.favoriteWork")}
    </button>
    <button type="button" className="text-button danger-text" disabled={operationBusy || isCurating} aria-disabled={c.toggleFavorite.isPending || undefined} onClick={requestCuration}>{t("works.move_to_trash")}</button>
  </> : <p className="muted">{t("gallery.notCurator")}</p>;

  const feedback = <>
    {!curationConfirm && sharedCurationError ? <p className="operation-feedback is-error" role="alert">{sharedCurationError instanceof Error ? sharedCurationError.message : String(sharedCurationError)}</p> : null}
    {c.toggleFavorite.isError ? <p className="operation-feedback is-error" role="alert">{c.toggleFavorite.error instanceof Error ? c.toggleFavorite.error.message : String(c.toggleFavorite.error)}</p> : null}
  </>;

  return <>
    <Modal title={w.title || t("gallery.workDetail")} onClose={onClose} sheet>
      <WorkDetailView media={media} creator={w.creator_name || "—"} facts={facts} actions={detailActions} feedback={feedback}>{extraDetails}</WorkDetailView>
    </Modal>
    {m.fullAsset ? <FullImageDialog asset={m.fullAsset} onClose={() => m.setFullAsset(null)} openLabel={t("work_detail.open_original")} /> : null}
    {curationConfirm ? <Modal title={t(curationConfirm === "purge" ? "deletion.permanent_title" : curationConfirm === "trash" ? "works.move_to_trash" : "works.restore")} onClose={closeCuration}>
      <p className="confirm-message">{t(curationConfirm === "purge" ? "works.purge_confirm" : curationConfirm === "trash" ? "works.batch_trash_confirm" : "works.restore", { count: 1 })}</p>
      {isCurating ? <p className="confirm-pending" role="status">{t("common.processing")}</p> : null}
      {curationError ? <div className="confirm-error" role="alert"><p>{curationError}</p></div> : null}
      <div className="confirm-actions">
        <button type="button" className="button" onClick={closeCuration}>{isCurating ? t("common.close") : t("common.cancel")}</button>
        <button type="button" className="button primary" aria-disabled={isCurating || undefined} aria-busy={isCurating || undefined} onClick={() => { if (!isCurating) void runCuration(); }}>{curationError ? t("common.retry") : t("common.confirm")}</button>
      </div>
    </Modal> : null}
  </>;
}

function FullImageDialog({ asset, onClose, openLabel }: { asset: AssetData; onClose: () => void; openLabel: string }) {
  const source = asset.original_url || asset.preview_url || asset.thumb_url || "";
  return <Modal title={asset.file_name} onClose={onClose} sheet>
    <div className="detail-full-media">
      <img src={source} alt={asset.file_name} loading="eager" />
      {asset.original_url ? <p><a href={asset.original_url} target="_blank" rel="noopener noreferrer">{openLabel}</a></p> : null}
    </div>
  </Modal>;
}
