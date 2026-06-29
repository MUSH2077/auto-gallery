"use client";

import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import SourceBadge from "./SourceBadge";

export interface MediaAsset {
  id: string;
  file_name?: string;
  width?: number;
  height?: number;
  mime_type?: string;
  thumb_url?: string;
  preview_url?: string;
  original_url?: string;
}

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
}: {
  assetId?: string | null;
  src?: string | null;
  alt: string;
  size?: "thumb" | "preview" | "original";
  className?: string;
  fallback?: string;
}) {
  const [failed, setFailed] = useState(false);
  if ((!assetId && !src) || failed) {
    return (
      <div className={`${className} flex items-center justify-center bg-subtle text-xs text-muted`}>
        {fallback || "N/A"}
      </div>
    );
  }
  return (
    <img
      src={src || api.mediaUrl(assetId || "", size)}
      alt={alt}
      className={className}
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
    />
  );
}

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

function previewPosition(anchor: DOMRect | null) {
  if (typeof window === "undefined" || !anchor) return { top: 96, left: 96, width: 420 };
  const gutter = 14;
  const width = Math.min(460, Math.max(320, window.innerWidth * 0.34));
  const height = Math.min(560, window.innerHeight - gutter * 2);
  const preferRight = anchor.right + gutter + width <= window.innerWidth;
  const left = preferRight ? anchor.right + gutter : Math.max(gutter, anchor.left - width - gutter);
  const top = clamp(anchor.top + anchor.height / 2 - height / 2, gutter, window.innerHeight - height - gutter);
  return { top, left, width };
}

export function WorkPreviewOverlay({
  anchor,
  title,
  creatorName,
  source,
  assetIds,
  pageIndex,
  assetCount,
  onMouseEnter,
  onMouseLeave,
  onWheelPage,
}: {
  anchor: DOMRect | null;
  title?: string | null;
  creatorName?: string | null;
  source?: string | null;
  assetIds: string[];
  pageIndex: number;
  assetCount: number;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  onWheelPage: (delta: number) => void;
}) {
  const style = useMemo(() => previewPosition(anchor), [anchor]);
  const currentId = assetIds[pageIndex] || assetIds[0];
  const canPage = assetIds.length > 1;

  return (
    <div
      className="fixed z-50 overflow-hidden rounded-md border border-border bg-surface shadow-overlay"
      style={style}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      onWheel={(event) => {
        if (!canPage) return;
        event.preventDefault();
        onWheelPage(event.deltaY > 0 ? 1 : -1);
      }}
    >
      <div className="relative flex aspect-[4/3] max-h-[520px] items-center justify-center bg-canvas">
        <AssetImage assetId={currentId} size="preview" alt={title || ""} className="h-full w-full object-contain no-outline" />
        {canPage && (
          <div className="absolute bottom-2 left-2 rounded-md bg-black/70 px-2 py-1 text-xs font-medium text-white">
            {pageIndex + 1} / {assetCount}
          </div>
        )}
      </div>
      <div className="border-t border-border px-3 py-2">
        <div className="flex items-center gap-2">
          {source && <SourceBadge source={source} />}
          <div className="min-w-0 flex-1 truncate text-sm font-medium">{title || "Untitled"}</div>
        </div>
        <div className="mt-1 flex items-center justify-between gap-3 text-xs text-muted">
          <span className="truncate">{creatorName || "Unknown creator"}</span>
          {canPage && <span className="shrink-0">Scroll to page</span>}
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
  if (!assets.length) return null;
  return (
    <div className={`flex gap-2 overflow-x-auto pb-1 ${className}`}>
      {assets.map((asset, index) => {
        const archive = isArchiveAsset(asset);
        const active = index === activeIndex;
        return (
          <button
            key={asset.id}
            type="button"
            onClick={() => onSelect(index)}
            className={`relative h-16 w-16 shrink-0 overflow-hidden rounded-md border-2 transition-colors ${
              active ? "border-accent" : "border-border hover:border-accent/60"
            }`}
            title={asset.file_name || `Page ${index + 1}`}
          >
            {archive ? (
              <span className="flex h-full w-full items-center justify-center bg-subtle font-mono text-[10px] font-semibold text-muted">
                ZIP
              </span>
            ) : (
              <AssetImage
                src={asset.thumb_url}
                assetId={asset.id}
                alt={asset.file_name || `Page ${index + 1}`}
                className="h-full w-full object-cover"
              />
            )}
            <span className="absolute bottom-0.5 right-0.5 rounded bg-black/70 px-1 text-[10px] font-medium text-white">
              {index + 1}
            </span>
          </button>
        );
      })}
    </div>
  );
}
