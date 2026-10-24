"use client";

import { useEffect, useRef, useState } from "react";
import { useIsMutating, useMutation, useMutationState } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { useWorkDetails, type WorkSourceData } from "@/features/works/useWorkDetails";
import { useWorkMedia } from "@/features/works/useWorkMedia";
import { useAppearanceSettings } from "@/lib/appearance";
import { AssetViewer } from "@/components/MediaAssetRenderer";
import { type AssetData } from "@/components/WorkViewerParts";
import ErrorState from "@/components/ErrorState";
import { useGalleryFormat } from "@/features/gallery-ui/format";
import { Modal } from "@/features/gallery-ui/Modal";
import { WorkDetailView, type DetailFact } from "@/features/gallery-ui/DetailViews";
import { Icon } from "@/features/gallery-ui/Icon";
import { useGalleryCopy } from "@/features/gallery-ui/copy";

export default function WorkDetailDialog({ id, onClose, operationBusy = false }: {
  id: string;
  operationBusy?: boolean;
  onClose: () => void;
}) {
  const c = useWorkDetails(id);
  const m = useWorkMedia(id, { bindKeyboard: false, bindWheel: false });
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
  const source = (c.sources.data as WorkSourceData[] | undefined)?.[0]?.source;
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
  }, [m.setActiveIndex, m.totalPages]);

  if (!w) {
    return <Modal title={t("gallery.workDetail")} onClose={onClose} sheet>
      {c.work.isPending ? <div className="state-view" role="status">{t("common.loading")}</div> :
        c.work.isError ? <ErrorState error={c.work.error} onRetry={() => void c.work.refetch()} /> : null}
    </Modal>;
  }

  const activeAsset = m.assets.data?.[m.activeIndex] as AssetData | undefined;
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

  const facts: DetailFact[] = [
    { label: t("gallery.source"), value: source === "pixiv" ? "Pixiv" : source === "manual" ? t("gallery.localImport") : source || "—" },
    { label: t("gallery.importDate"), value: fmt.date(w.created_at) },
    { label: t("gallery.resources"), value: t("gallery.resourceCount", { count: w.asset_count }) },
    { label: t("gallery.content"), value: <>{t(w.is_nsfw ? "gallery.sensitiveContent" : "gallery.normalContent")}{w.is_ai_generated ? `，${t("gallery.ai")}` : ""}</> },
  ];

  const canAdmin = c.has("admin");
  const detailActions = c.canCurate ? w.curation_state?.visibility === "trashed" ? <>
    <button type="button" className="button primary" disabled={operationBusy || isCurating} aria-busy={isCurating || undefined} onClick={requestCuration}>{t("works.restore")}</button>
    {canAdmin ? <button type="button" className="text-button danger-text" disabled={operationBusy || isCurating} onClick={requestPurge}>{t("deletion.permanent_title")}</button> : null}
  </> : <>
    <button type="button" className={"button detail-favorite-toggle" + (w.is_favorite ? " is-favorite" : "")} aria-label={t(w.is_favorite ? "gallery.unfavoriteWork" : "gallery.favoriteWork")} aria-pressed={Boolean(w.is_favorite)} aria-busy={c.toggleFavorite.isPending || undefined} aria-disabled={operationBusy || isCurating || c.toggleFavorite.isPending || undefined} disabled={operationBusy || isCurating} onClick={toggleFavorite}>
      <Icon name="heart" />{w.is_favorite ? copy("saved") : t("gallery.favoriteWork")}
    </button>
    <button type="button" className="text-button danger-text" disabled={operationBusy || isCurating || c.toggleFavorite.isPending} onClick={requestCuration}>{t("works.move_to_trash")}</button>
  </> : <p className="muted">{t("gallery.notCurator")}</p>;

  const feedback = <>
    {!curationConfirm && sharedCurationError ? <p className="operation-feedback is-error" role="alert">{sharedCurationError instanceof Error ? sharedCurationError.message : String(sharedCurationError)}</p> : null}
    {c.toggleFavorite.isError ? <p className="operation-feedback is-error" role="alert">{c.toggleFavorite.error instanceof Error ? c.toggleFavorite.error.message : String(c.toggleFavorite.error)}</p> : null}
  </>;

  return <>
    <Modal title={w.title || t("gallery.workDetail")} onClose={onClose} sheet>
      <WorkDetailView media={media} creator={w.creator_name || "—"} facts={facts} actions={detailActions} feedback={feedback} />
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
