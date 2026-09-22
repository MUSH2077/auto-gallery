"use client";

import Link from "next/link";
import { Star } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { WorkMediaThumbnail } from "@/components/MediaAssetRenderer";
import SourceBadge from "@/components/SourceBadge";
import type { WorkListItem } from "@/lib/api";
import type { WorkCardSize, WorksViewMode } from "@/lib/appearance";
import { useI18nFormat } from "@/lib/i18n-format";
import { useT } from "@/lib/i18n";
import { searchUrl } from "@/lib/search-query";
import type { StaggeredEntranceProps } from "@/lib/motion";

export type WorkCardPreview = {
  work: WorkListItem;
  anchor: DOMRect;
  assetIds: string[];
  pageIndex: number;
};

export interface WorkCardPresentation {
  layout: WorksViewMode;
  size: WorkCardSize;
  showCheckbox: boolean;
  showAi: boolean;
  showNsfw: boolean;
  showFavorite: boolean;
  blurNsfw: boolean;
}

export interface WorkCardProps {
  work: WorkListItem;
  presentation: WorkCardPresentation;
  onToggleFavorite: (id: string) => void;
  trashMode?: boolean;
  onRestore?: (id: string) => void;
  onPurge?: (id: string) => void;
  selectable?: boolean;
  selected?: boolean;
  onToggleSelect?: (id: string) => void;
  entrance?: StaggeredEntranceProps;
  previewEnabled: boolean;
  previewDelayMs: number;
  wheelThreshold: number;
  onOpenPreview: (preview: WorkCardPreview) => void;
  onScheduleClosePreview: () => void;
  onCancelClosePreview: () => void;
  onPreviewPage: (workId: string, pageIndex: number) => void;
  canCurate: boolean;
  canPurge: boolean;
  eager?: boolean;
}

const gridVisualHeight: Record<WorkCardSize, string> = {
  small: "h-28",
  medium: "h-36",
  large: "h-52",
};

const listVisualSize: Record<WorkCardSize, string> = {
  small: "h-12 w-12",
  medium: "h-16 w-16",
  large: "h-24 w-24",
};

const listPadding: Record<WorkCardSize, string> = {
  small: "p-2",
  medium: "p-3",
  large: "p-4",
};

function safeAspectRatio(work: WorkListItem) {
  const width = work.thumbnail_width || 0;
  const height = work.thumbnail_height || 0;
  return width > 0 && height > 0 ? width / height : 4 / 3;
}

export function WorkCard({
  work,
  presentation,
  onToggleFavorite,
  trashMode = false,
  onRestore,
  onPurge,
  selectable = false,
  selected = false,
  onToggleSelect,
  entrance,
  previewEnabled,
  previewDelayMs,
  wheelThreshold,
  onOpenPreview,
  onScheduleClosePreview,
  onCancelClosePreview,
  onPreviewPage,
  canCurate,
  canPurge,
  eager = false,
}: WorkCardProps) {
  const t = useT();
  const fmt = useI18nFormat();
  const cardRef = useRef<HTMLElement | null>(null);
  const hoverTimer = useRef<number | null>(null);
  const wheelDelta = useRef(0);
  const [pageIndex, setPageIndex] = useState(0);
  const assetIds = work.preview_asset_ids?.length
    ? work.preview_asset_ids
    : work.thumbnail_asset_id
      ? [work.thumbnail_asset_id]
      : [];
  const hasMultiple = assetIds.length > 1;
  const currentAssetId = assetIds[pageIndex] || assetIds[0];
  const isList = presentation.layout === "list";
  const isMasonry = presentation.layout === "masonry";
  const showCheckbox = selectable && presentation.showCheckbox;
  const shouldBlur = work.is_nsfw && presentation.blurNsfw;

  const setPreviewPage = (next: number) => {
    if (!assetIds.length) return;
    const normalized = (next + assetIds.length) % assetIds.length;
    setPageIndex(normalized);
    onPreviewPage(work.id, normalized);
  };

  const openPreview = () => {
    if (!previewEnabled || !cardRef.current || !assetIds.length || window.matchMedia("(pointer: coarse)").matches) return;
    onOpenPreview({
      work,
      anchor: cardRef.current.getBoundingClientRect(),
      assetIds,
      pageIndex,
    });
  };

  const clearHoverTimer = () => {
    if (hoverTimer.current !== null) window.clearTimeout(hoverTimer.current);
    hoverTimer.current = null;
  };

  useEffect(() => () => clearHoverTimer(), []);

  useEffect(() => {
    const card = cardRef.current;
    if (!card || !hasMultiple) return;
    const onWheel = (event: WheelEvent) => {
      wheelDelta.current += event.deltaY;
      if (Math.abs(wheelDelta.current) < wheelThreshold) return;
      event.preventDefault();
      setPreviewPage(pageIndex + (wheelDelta.current > 0 ? 1 : -1));
      wheelDelta.current = 0;
    };
    card.addEventListener("wheel", onWheel, { passive: false });
    return () => card.removeEventListener("wheel", onWheel);
  }, [assetIds.length, hasMultiple, pageIndex, wheelThreshold]);

  const visual = (
    <div
      data-nsfw-blurred={shouldBlur ? "true" : undefined}
      className={`media-motion-visual pointer-events-none relative z-10 shrink-0 overflow-hidden bg-subtle text-xs text-muted ${
        isList
          ? `${listVisualSize[presentation.size]} rounded-md`
          : isMasonry
            ? "w-full"
            : `w-full ${gridVisualHeight[presentation.size]}`
      }`}
      style={isMasonry ? { aspectRatio: safeAspectRatio(work) } : undefined}
    >
      <WorkMediaThumbnail
        assetId={currentAssetId}
        hasVideo={work.has_video}
        alt={work.title || ""}
        className={`h-full w-full object-cover transition-[filter,transform] duration-200 ${shouldBlur ? "scale-[1.04] blur-lg" : ""}`}
        fallback={currentAssetId ? t("media.derivative_pending") : t("works.na")}
        eager={eager}
      />
      {showCheckbox ? (
        <label className="pointer-events-auto absolute left-1 top-1 z-20 flex h-7 w-7 items-center justify-center rounded bg-black/65 text-white shadow-sm">
          <span className="sr-only">{t("works.select_work")}</span>
          <input
            type="checkbox"
            checked={selected}
            onChange={(event) => {
              event.stopPropagation();
              onToggleSelect?.(work.id);
            }}
            onClick={(event) => event.stopPropagation()}
            className="h-4 w-4 rounded border-white"
          />
        </label>
      ) : null}
      {canCurate && presentation.showFavorite ? (
        <button
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            onToggleFavorite(work.id);
          }}
          className={`pointer-events-auto absolute right-0 top-0 z-20 flex h-11 w-11 items-center justify-center ${work.is_favorite ? "text-warning" : "text-white/70 hover:text-warning"} drop-shadow`}
          title={work.is_favorite ? t("works.unfavorite") : t("works.favorite")}
          aria-label={work.is_favorite ? t("works.unfavorite") : t("works.favorite")}
        >
          <Star className="h-5 w-5" fill={work.is_favorite ? "currentColor" : "none"} aria-hidden />
        </button>
      ) : null}
      {work.asset_count > 1 ? (
        <span className="absolute bottom-1 left-1 rounded bg-black/70 px-1.5 py-0.5 text-xs font-medium text-white">{work.asset_count}p</span>
      ) : null}
      {work.is_ai_generated && presentation.showAi ? (
        <span className={`absolute rounded bg-warning px-1.5 py-0.5 text-xs text-on-primary ${showCheckbox ? "left-9 top-1" : "left-1 top-1"}`}>{t("works.ai_badge")}</span>
      ) : null}
      {work.is_nsfw && presentation.showNsfw ? (
        <span className={`absolute rounded bg-danger/90 px-1.5 py-0.5 text-xs font-medium text-white ${work.is_ai_generated && presentation.showAi ? "left-1 top-9" : showCheckbox ? "left-9 top-1" : "left-1 top-1"}`}>{t("works.nsfw_badge")}</span>
      ) : null}
      {work.has_ugoira ? (
        <span className="absolute bottom-1 right-1 rounded bg-accent px-1.5 py-0.5 text-[10px] font-medium text-on-primary">{t("works.gif_badge")}</span>
      ) : null}
      {trashMode && !showCheckbox ? (
        <span className="absolute left-1 top-1 rounded bg-danger/90 px-1.5 py-0.5 text-xs font-medium text-white">{t("works.trash_badge")}</span>
      ) : null}
    </div>
  );

  const details = (
    <div className={`pointer-events-none relative z-10 min-w-0 ${isList ? "flex flex-1 items-center gap-3" : "p-3"}`}>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-fg">{work.title || t("works.untitled")}</div>
        <div className="mt-1 flex min-w-0 items-center gap-1.5">
          {work.source ? <SourceBadge source={work.source} href={searchUrl("/admin/works", `source:${work.source}`)} /> : null}
          {work.creator_name && work.creator_id ? (
            <Link
              href={`/admin/creators/${work.creator_id}`}
              className="pointer-events-auto relative z-20 truncate text-xs text-accent hover:underline"
            >
              {work.creator_name}
            </Link>
          ) : null}
          {work.posted_at ? <span className="shrink-0 text-xs text-muted">{fmt.date(work.posted_at)}</span> : null}
        </div>
      </div>
      {trashMode && canCurate ? (
        <div className={`pointer-events-auto relative z-20 flex shrink-0 gap-2 ${isList ? "" : "mt-3"}`}>
          <button type="button" onClick={(event) => { event.stopPropagation(); onRestore?.(work.id); }} className="rounded border border-border px-2 py-1 text-xs hover:bg-subtle">
            {t("works.restore")}
          </button>
          {canPurge ? (
            <button type="button" onClick={(event) => { event.stopPropagation(); onPurge?.(work.id); }} className="rounded bg-danger px-2 py-1 text-xs text-white hover:bg-danger">
              {t("works.purge")}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );

  return (
    <article
      ref={cardRef}
      data-work-card={work.id}
      data-card-layout={presentation.layout}
      data-card-size={presentation.size}
      className={`card-interactive media-motion-card group relative break-inside-avoid overflow-hidden ${entrance?.className || ""} ${selected ? "ring-2 ring-accent" : ""} ${isList ? `flex items-center gap-3 ${listPadding[presentation.size]}` : ""}`}
      style={{
        ...entrance?.style,
        contentVisibility: "auto",
        containIntrinsicSize: isList ? "auto 72px" : "auto 260px",
      }}
      onMouseEnter={() => {
        onCancelClosePreview();
        clearHoverTimer();
        hoverTimer.current = window.setTimeout(openPreview, previewDelayMs);
      }}
      onMouseLeave={() => {
        clearHoverTimer();
        onScheduleClosePreview();
      }}
    >
      <Link
        href={`/admin/works/${work.id}`}
        aria-label={t("common.open_item", { name: work.title || t("works.untitled") })}
        className="absolute inset-0 z-0 rounded-md focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      />
      {visual}
      {details}
    </article>
  );
}
