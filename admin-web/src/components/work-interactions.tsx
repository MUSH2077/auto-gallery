"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { WorkPreviewSize } from "@/lib/appearance";
import SourceBadge from "./SourceBadge";
import { useT } from "@/lib/i18n";
import { resolveMediaKind, type MediaAssetData } from "@/lib/media";

export type MediaAsset = MediaAssetData;

export function isArchiveAsset(asset: MediaAsset | null | undefined) {
  if (!asset) return false;
  return asset.mime_type === "application/zip" || (asset.file_name || "").toLowerCase().endsWith(".zip");
}

export function AssetImage({
  assetId,
  src,
  alt,
  size = "thumb",
  className = "",
  fallback,
  onLoad,
  onError,
  loading = "lazy",
}: {
  assetId?: string | null;
  src?: string | null;
  alt: string;
  size?: "thumb" | "preview" | "original";
  className?: string;
  fallback?: string;
  onLoad?: (event: React.SyntheticEvent<HTMLImageElement>) => void;
  onError?: () => void;
  loading?: "eager" | "lazy";
}) {
  const t = useT();
  const [failed, setFailed] = useState(false);
  const resolvedSrc = src || (assetId ? api.mediaUrl(assetId, size) : "");

  useEffect(() => {
    setFailed(false);
  }, [resolvedSrc]);

  if ((!assetId && !src) || failed) {
    return (
      <div className={`${className} flex items-center justify-center bg-subtle text-xs text-muted`}>
        {fallback || t("common.na")}
      </div>
    );
  }
  return (
    <img
      src={resolvedSrc}
      alt={alt}
      className={className}
      loading={loading}
      decoding="async"
      onLoad={onLoad}
      onError={() => {
        setFailed(true);
        onError?.();
      }}
    />
  );
}

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

function previewBounds(size: WorkPreviewSize) {
  if (typeof window === "undefined") return { maxWidth: 520, maxHeight: 520 };
  if (size === "medium") {
    return {
      maxWidth: Math.min(600, Math.max(320, window.innerWidth * 0.38)),
      maxHeight: Math.max(300, window.innerHeight * 0.68),
    };
  }
  if (size === "fit") {
    return {
      maxWidth: Math.min(980, Math.max(320, window.innerWidth - 28)),
      maxHeight: Math.max(340, window.innerHeight * 0.88),
    };
  }
  return {
    maxWidth: Math.min(760, Math.max(340, window.innerWidth * 0.48)),
    maxHeight: Math.max(340, window.innerHeight * 0.82),
  };
}

function fitPreviewFrame(ratio: number, size: WorkPreviewSize) {
  const footerHeight = 74;
  const bounds = previewBounds(size);
  const imageMaxHeight = Math.max(220, bounds.maxHeight - footerHeight);
  let frameWidth = bounds.maxWidth;
  let frameHeight = frameWidth / ratio;
  if (frameHeight > imageMaxHeight) {
    frameHeight = imageMaxHeight;
    frameWidth = frameHeight * ratio;
  }
  const viewportWidth = typeof window === "undefined" ? 1024 : window.innerWidth;
  frameWidth = clamp(frameWidth, Math.min(300, viewportWidth - 28), bounds.maxWidth);
  frameHeight = Math.min(frameHeight, imageMaxHeight);
  return { width: Math.round(frameWidth), imageHeight: Math.round(frameHeight), totalHeight: Math.round(frameHeight + footerHeight) };
}

function previewPosition(anchor: DOMRect | null, ratio: number, size: WorkPreviewSize) {
  if (typeof window === "undefined" || !anchor) return { top: 96, left: 96, width: 420 };
  const gutter = 14;
  const frame = fitPreviewFrame(ratio, size);
  const width = Math.min(frame.width, window.innerWidth - gutter * 2);
  const height = Math.min(frame.totalHeight, window.innerHeight - gutter * 2);
  const preferRight = anchor.right + gutter + width <= window.innerWidth;
  const leftCandidate = preferRight ? anchor.right + gutter : anchor.left - width - gutter;
  const left = clamp(leftCandidate, gutter, window.innerWidth - width - gutter);
  const top = clamp(anchor.top + anchor.height / 2 - height / 2, gutter, window.innerHeight - height - gutter);
  return { top, left, width, "--preview-image-height": `${Math.max(180, height - 74)}px` } as React.CSSProperties;
}

export function WorkPreviewOverlay({
  anchor,
  title,
  creatorName,
  source,
  assetIds,
  assets,
  isLoading = false,
  isError = false,
  previewSize,
  pageIndex,
  assetCount,
  onMouseEnter,
  onMouseLeave,
  onWheelPage,
  onRefreshAssets,
}: {
  anchor: DOMRect | null;
  title?: string | null;
  creatorName?: string | null;
  source?: string | null;
  assetIds: string[];
  assets?: MediaAsset[];
  isLoading?: boolean;
  isError?: boolean;
  previewSize: WorkPreviewSize;
  pageIndex: number;
  assetCount: number;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  onWheelPage: (delta: number) => void;
  onRefreshAssets?: () => void;
}) {
  const t = useT();
  const [naturalRatio, setNaturalRatio] = useState<number | null>(null);
  const retriedSources = useRef<Set<string>>(new Set());
  const assetById = useMemo(() => new Map((assets || []).map((asset) => [asset.id, asset])), [assets]);
  const currentId = assetIds[pageIndex] || assetIds[0];
  const currentAsset = assetById.get(currentId) || assets?.[pageIndex] || assets?.[0];
  const currentKind = currentAsset ? resolveMediaKind(currentAsset) : "unknown";
  const currentSrc = currentKind === "video"
    ? currentAsset?.poster_url || currentAsset?.thumb_url || null
    : currentAsset?.original_url || currentAsset?.preview_url || null;
  const footerText = currentAsset?.file_name
    ? `${currentAsset.file_name}${creatorName ? ` · ${creatorName}` : ""}`
    : creatorName || t("works.unknown_creator");
  const ratio = naturalRatio || ((currentAsset?.width || 0) > 0 && (currentAsset?.height || 0) > 0 ? (currentAsset!.width! / currentAsset!.height!) : 4 / 3);
  const style = useMemo(() => previewPosition(anchor, ratio, previewSize), [anchor, ratio, previewSize]);
  const canPage = Math.max(assetIds.length, assets?.length || 0) > 1;

  useEffect(() => {
    setNaturalRatio(null);
  }, [currentSrc, currentId]);

  return (
    <div
      className="popover fixed z-50 overflow-hidden rounded-md border border-border bg-surface shadow-overlay"
      style={style}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      onWheel={(event) => {
        if (!canPage) return;
        event.preventDefault();
        onWheelPage(event.deltaY > 0 ? 1 : -1);
      }}
    >
      <div className="relative flex items-center justify-center bg-canvas" style={{ height: "var(--preview-image-height)" }}>
        {isLoading ? (
          <div className="h-full w-full animate-pulse bg-subtle" />
        ) : isError ? (
          <div className="flex h-full w-full items-center justify-center px-4 text-center text-xs text-muted">{t("works.preview_failed")}</div>
        ) : currentSrc ? (
          <AssetImage
            src={currentSrc}
            alt={title || currentAsset?.file_name || ""}
            className="h-full w-full object-contain no-outline"
            fallback={t("works.original_unavailable")}
            onLoad={(event) => {
              const image = event.currentTarget;
              if (image.naturalWidth > 0 && image.naturalHeight > 0) {
                setNaturalRatio(image.naturalWidth / image.naturalHeight);
              }
            }}
            onError={() => {
              if (!currentSrc || retriedSources.current.has(currentSrc)) return;
              retriedSources.current.add(currentSrc);
              onRefreshAssets?.();
            }}
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center px-4 text-center text-xs text-muted">{t("works.original_unavailable")}</div>
        )}
        {currentKind === "video" && currentSrc ? (
          <span className="pointer-events-none absolute bottom-2 right-2 rounded bg-black/75 px-2 py-1 text-xs font-semibold text-white">
            {t("media.video_badge")}
          </span>
        ) : null}
        {canPage && (
          <div className="absolute bottom-2 left-2 rounded-md bg-black/70 px-2 py-1 text-xs font-medium text-white">
            {pageIndex + 1} / {assetCount}
          </div>
        )}
      </div>
      <div className="border-t border-border px-3 py-2">
        <div className="flex items-center gap-2">
          {source && <SourceBadge source={source} />}
          <div className="min-w-0 flex-1 truncate text-sm font-medium">{title || t("work_detail.untitled")}</div>
        </div>
        <div className="mt-1 flex items-center justify-between gap-3 text-xs text-muted">
          <span className="truncate">{footerText}</span>
          {canPage && <span className="shrink-0">{t("works.scroll_to_page")}</span>}
        </div>
      </div>
    </div>
  );
}

export function AssetFilmstrip<T extends MediaAsset>({
  assets,
  activeIndex,
  onSelect,
  className = "",
}: {
  assets: T[];
  activeIndex: number;
  onSelect: (index: number) => void;
  className?: string;
}) {
  const t = useT();
  if (!assets.length) return null;
  return (
    <div className={`flex gap-2 overflow-x-auto pb-1 ${className}`}>
      {assets.map((asset, index) => {
        const archive = isArchiveAsset(asset);
        const kind = resolveMediaKind(asset);
        const active = index === activeIndex;
        return (
          <button
            key={asset.id}
            type="button"
            onClick={() => onSelect(index)}
            className={`relative h-16 w-16 shrink-0 overflow-hidden rounded-md border-2 transition-colors ${
              active ? "border-accent" : "border-border hover:border-accent/60"
            }`}
            title={asset.file_name || t("works.page_number", { page: index + 1 })}
          >
            {archive ? (
              <span className="flex h-full w-full items-center justify-center bg-subtle font-mono text-[10px] font-semibold text-muted">
                ZIP
              </span>
            ) : (
              <AssetImage
                src={asset.thumb_url || (kind === "video" ? asset.poster_url : asset.preview_url)}
                assetId={asset.id}
                alt={asset.file_name || t("works.page_number", { page: index + 1 })}
                className="h-full w-full object-cover"
              />
            )}
            {kind === "video" ? (
              <span className="absolute left-1 top-1 rounded bg-black/75 px-1 text-[9px] font-semibold text-white">
                {t("media.video_badge")}
              </span>
            ) : null}
            <span className="absolute bottom-0.5 right-0.5 rounded bg-black/70 px-1 text-[10px] font-medium text-white">
              {index + 1}
            </span>
          </button>
        );
      })}
    </div>
  );
}
