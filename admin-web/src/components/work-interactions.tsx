"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { derivativeIsPending, resolveMediaKind, type MediaAssetData } from "@/lib/media";

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
        {fallback || (failed ? t("media.image_load_failed") : t("common.na"))}
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
            ) : derivativeIsPending(asset.derivative_status) && !asset.thumb_url && !asset.poster_url ? (
              <span className="flex h-full w-full items-center justify-center bg-subtle px-1 text-center text-[9px] text-muted">
                {t("media.derivative_pending")}
              </span>
            ) : (
              <AssetImage
                src={asset.thumb_url || (kind === "video" ? asset.poster_url : asset.preview_url)}
                assetId={asset.id}
                alt={asset.file_name || t("works.page_number", { page: index + 1 })}
                className="h-full w-full object-cover"
                fallback={asset.derivative_status === "failed" ? t("media.derivative_failed") : undefined}
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
