"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ImageOff, Play, Video } from "lucide-react";

import { api } from "@/lib/api";
import { useT } from "@/lib/i18n";
import {
  formatMediaDuration,
  derivativeIsPending,
  isBrowserPlayableVideo,
  resolveMediaKind,
  type MediaAssetData,
} from "@/lib/media";
import { AssetImage } from "./work-interactions";

export function VideoBadge({ compact = false }: { compact?: boolean }) {
  const t = useT();
  return (
    <span
      className="inline-flex items-center gap-1 rounded bg-black/75 px-1.5 py-0.5 text-[10px] font-semibold text-white"
      aria-label={t("media.video_badge")}
    >
      <Video className="h-3 w-3" aria-hidden />
      {compact ? null : t("media.video_badge")}
    </span>
  );
}

export function WorkMediaThumbnail({
  assetId,
  hasVideo = false,
  alt,
  className = "",
  fallback,
  eager = false,
}: {
  assetId?: string | null;
  hasVideo?: boolean;
  alt: string;
  className?: string;
  fallback?: string;
  eager?: boolean;
}) {
  const t = useT();
  return (
    <span className="relative block h-full w-full overflow-hidden">
      <AssetImage
        assetId={assetId}
        alt={alt}
        className={className}
        fallback={fallback || t("works.na")}
        loading={eager ? "eager" : "lazy"}
      />
      {hasVideo ? (
        <span className="pointer-events-none absolute bottom-1 right-1">
          <VideoBadge />
        </span>
      ) : null}
    </span>
  );
}

export function AssetPreviewMedia({
  asset,
  alt,
  className,
  onLoad,
  onError,
}: {
  asset: MediaAssetData;
  alt: string;
  className: string;
  onLoad?: (event: React.SyntheticEvent<HTMLImageElement>) => void;
  onError?: () => void;
}) {
  const t = useT();
  const kind = resolveMediaKind(asset);
  const derivativePending = derivativeIsPending(asset.derivative_status);
  const derivativeFailed = asset.derivative_status === "failed";
  if (kind === "archive" || kind === "unknown") {
    return (
      <span className={`${className} flex items-center justify-center bg-subtle font-mono text-xs font-semibold text-muted`}>
        {kind === "archive" ? "ZIP" : t("common.na")}
      </span>
    );
  }
  const src = kind === "video"
    ? asset.poster_url || asset.thumb_url
    : asset.preview_url || asset.original_url || asset.thumb_url;
  const derivativeLabel = derivativeFailed
    ? t("media.derivative_failed")
    : derivativePending
      ? t("media.derivative_pending")
      : null;
  return (
    <span className="relative block h-full w-full">
      <AssetImage
        src={src}
        assetId={src ? undefined : asset.id}
        alt={alt}
        className={className}
        fallback={derivativeLabel || (kind === "video" ? t("media.poster_unavailable") : t("works.original_unavailable"))}
        onLoad={onLoad}
        onError={onError}
      />
      {derivativeLabel ? (
        <span className="pointer-events-none absolute left-2 top-2 rounded bg-black/75 px-2 py-1 text-[10px] font-medium text-white">
          {derivativeLabel}
        </span>
      ) : null}
      {kind === "video" ? (
        <span className="pointer-events-none absolute bottom-2 right-2">
          <VideoBadge />
        </span>
      ) : null}
    </span>
  );
}

export function AssetViewer({
  workId,
  asset,
  onOpenImage,
}: {
  workId: string;
  asset: MediaAssetData;
  onOpenImage: () => void;
}) {
  const t = useT();
  const kind = resolveMediaKind(asset);
  const derivativePending = derivativeIsPending(asset.derivative_status);
  const derivativeFailed = asset.derivative_status === "failed";
  if (kind === "video") {
    if (!isBrowserPlayableVideo(asset)) {
      return <UnsupportedVideoAsset asset={asset} />;
    }
    return <VideoAssetPlayer key={asset.id} workId={workId} asset={asset} />;
  }
  if (kind === "archive" || kind === "unknown") {
    return (
      <div className="mx-4 rounded-md border border-border bg-surface p-8 text-center">
        <div className="mx-auto mb-3 flex h-20 w-20 items-center justify-center rounded-md border border-border bg-subtle font-mono text-base font-semibold text-muted">
          {kind === "archive" ? "ZIP" : "?"}
        </div>
        <div className="text-sm font-medium text-fg">
          {kind === "archive" ? t("work_detail.archive_asset") : t("common.na")}
        </div>
        <div className="mt-1 max-w-md truncate text-xs text-muted">{asset.file_name}</div>
        {asset.original_url ? (
          <a href={asset.original_url} className="btn-ghost mt-4 inline-flex text-xs" target="_blank" rel="noopener noreferrer">
            {t("work_detail.download_original")}
          </a>
        ) : null}
      </div>
    );
  }
  return (
    <button
      type="button"
      onClick={onOpenImage}
      className="group relative flex h-full min-h-[58vh] w-full items-center justify-center p-3"
      title={t("work_detail.view_full")}
    >
      <AssetImage
        key={asset.id}
        src={asset.preview_url || ((derivativePending || derivativeFailed) ? asset.original_url : undefined)}
        assetId={asset.preview_url || ((derivativePending || derivativeFailed) && asset.original_url) ? undefined : asset.id}
        size="preview"
        alt={asset.file_name || ""}
        className="fade-in max-h-[72vh] max-w-full object-contain no-outline transition-transform duration-150 group-hover:scale-[1.005]"
        fallback={derivativeFailed ? t("media.derivative_failed") : derivativePending ? t("media.derivative_pending") : undefined}
      />
      {derivativePending || derivativeFailed ? (
        <span className="pointer-events-none absolute left-4 top-4 rounded bg-black/75 px-2.5 py-1 text-xs font-medium text-white">
          {derivativeFailed ? t("media.derivative_failed") : t("media.derivative_pending")}
        </span>
      ) : null}
    </button>
  );
}

function UnsupportedVideoAsset({ asset }: { asset: MediaAssetData }) {
  const t = useT();
  return (
    <div className="mx-4 flex min-h-[58vh] flex-col items-center justify-center gap-3 rounded-md border border-border bg-black px-6 text-center text-white">
      <ImageOff className="h-8 w-8 text-white/60" aria-hidden />
      <p className="max-w-md text-sm">{t("media.video_unsupported")}</p>
      {asset.original_url ? (
        <a
          href={asset.original_url}
          target="_blank"
          rel="noopener noreferrer"
          className="rounded-md border border-white/25 px-3 py-2 text-sm hover:bg-white/10"
        >
          {t("media.open_original")}
        </a>
      ) : null}
    </div>
  );
}

function VideoAssetPlayer({ workId, asset }: { workId: string; asset: MediaAssetData }) {
  const t = useT();
  const videoRef = useRef<HTMLVideoElement>(null);
  const retryCount = useRef(0);
  const resumeAt = useRef(0);
  const shouldPlay = useRef(false);
  const [source, setSource] = useState<string | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "ready" | "error" | "unsupported">("idle");

  const requestTicket = useCallback(async (resume = false) => {
    if (resume && videoRef.current) {
      resumeAt.current = videoRef.current.currentTime || 0;
      shouldPlay.current = !videoRef.current.paused;
    } else {
      resumeAt.current = 0;
      shouldPlay.current = true;
    }
    setStatus("loading");
    setSource(null);
    try {
      const ticket = await api.createPlaybackTicket(workId, asset.id);
      setSource(ticket.url);
    } catch {
      setStatus("error");
    }
  }, [asset.id, workId]);

  useEffect(() => {
    const pauseWhenHidden = () => {
      if (document.hidden) videoRef.current?.pause();
    };
    document.addEventListener("visibilitychange", pauseWhenHidden);
    return () => {
      document.removeEventListener("visibilitychange", pauseWhenHidden);
      const video = videoRef.current;
      if (video) {
        video.pause();
        video.removeAttribute("src");
        video.load();
      }
    };
  }, []);

  const handleLoadedMetadata = () => {
    const video = videoRef.current;
    if (!video) return;
    if (resumeAt.current > 0 && Number.isFinite(video.duration)) {
      video.currentTime = Math.min(resumeAt.current, Math.max(0, video.duration - 0.1));
    }
    setStatus("ready");
    if (shouldPlay.current) {
      void video.play().catch(() => undefined);
    }
  };

  const handleMediaError = () => {
    const code = videoRef.current?.error?.code;
    if (code === MediaError.MEDIA_ERR_NETWORK && retryCount.current < 1) {
      retryCount.current += 1;
      void requestTicket(true);
      return;
    }
    setStatus(
      code === MediaError.MEDIA_ERR_DECODE || code === MediaError.MEDIA_ERR_SRC_NOT_SUPPORTED
        ? "unsupported"
        : "error",
    );
  };

  const duration = formatMediaDuration(asset.duration);
  const message = status === "loading"
    ? t("media.loading_video")
    : status === "unsupported"
      ? t("media.video_unsupported")
      : status === "error"
        ? t("media.video_load_failed")
        : "";

  return (
    <div className="relative flex min-h-[58vh] w-full items-center justify-center bg-black">
      <video
        ref={videoRef}
        src={source || undefined}
        poster={asset.poster_url || asset.thumb_url}
        controls={Boolean(source)}
        playsInline
        preload="metadata"
        className="max-h-[72vh] max-w-full"
        aria-label={asset.file_name || t("media.video_badge")}
        onLoadedMetadata={handleLoadedMetadata}
        onError={handleMediaError}
        onPlay={() => {
          shouldPlay.current = true;
        }}
        onEnded={() => {
          shouldPlay.current = false;
        }}
      />
      {!source && status !== "loading" ? (
        <button
          type="button"
          onClick={() => void requestTicket(false)}
          className="absolute inset-0 flex min-h-11 w-full items-center justify-center bg-black/10 text-white outline-none hover:bg-black/20 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent"
          aria-label={t("media.play_video")}
        >
          <span className="flex h-16 w-16 items-center justify-center rounded-full bg-black/75 shadow-overlay">
            <Play className="ml-1 h-7 w-7" fill="currentColor" aria-hidden />
          </span>
        </button>
      ) : null}
      {status === "loading" ? (
        <div className="absolute inset-0 flex items-center justify-center bg-black/55 text-sm text-white">
          {t("media.loading_video")}
        </div>
      ) : null}
      {status === "error" || status === "unsupported" ? (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/85 px-6 text-center text-white">
          <ImageOff className="h-8 w-8 text-white/60" aria-hidden />
          <p className="max-w-md text-sm">{message}</p>
          <div className="flex flex-wrap justify-center gap-2">
            <button type="button" className="rounded-md border border-white/25 px-3 py-2 text-sm hover:bg-white/10" onClick={() => {
              retryCount.current = 0;
              void requestTicket(true);
            }}>
              {t("common.retry")}
            </button>
            {source || asset.original_url ? (
              <a href={source || asset.original_url} target="_blank" rel="noopener noreferrer" className="rounded-md border border-white/25 px-3 py-2 text-sm hover:bg-white/10">
                {t("media.open_original")}
              </a>
            ) : null}
          </div>
        </div>
      ) : null}
      {duration ? (
        <span className="pointer-events-none absolute right-3 top-3 rounded bg-black/75 px-2 py-1 text-xs font-medium tabular-nums text-white">
          {duration}
        </span>
      ) : null}
      <span className="sr-only" aria-live="polite">{message}</span>
    </div>
  );
}
