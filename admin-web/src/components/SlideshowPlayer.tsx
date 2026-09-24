"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { Pause, Play, X } from "lucide-react";

import { WorkMediaThumbnail } from "@/components/MediaAssetRenderer";
import { ArrowIcon } from "@/components/WorkViewerParts";
import { api } from "@/lib/api";
import type { WorkAsset } from "@/lib/api/endpoints/works";
import { useT } from "@/lib/i18n";
import { resolveMediaKind } from "@/lib/media";
import { motionTokens, usePresence } from "@/lib/motion";
import { useSlideshowConfig } from "@/lib/slideshow/config";

export interface SlideItem {
  assetId: string;
  workId: string;
  title?: string | null;
  creatorName?: string | null;
}

type ResolvedSlide = {
  item: SlideItem;
  url: string;
  videoPoster: boolean;
};

const resolvedSlideCache = new Map<string, Promise<ResolvedSlide | null>>();

function slideKey(item: SlideItem) {
  return `${item.workId}:${item.assetId}`;
}

async function decodeImage(url: string): Promise<void> {
  const image = new Image();
  await new Promise<void>((resolve, reject) => {
    let settled = false;
    const done = (callback: () => void) => {
      if (settled) return;
      settled = true;
      callback();
    };
    image.onload = () => done(resolve);
    image.onerror = () => done(() => reject(new Error("image_decode_failed")));
    image.src = url;
    if (typeof image.decode === "function") {
      void image.decode().then(() => done(resolve)).catch(() => undefined);
    }
  });
}

async function resolveSlide(item: SlideItem, force = false): Promise<ResolvedSlide | null> {
  const key = slideKey(item);
  if (force) resolvedSlideCache.delete(key);
  const cached = resolvedSlideCache.get(key);
  if (cached) return cached;
  const request = (async () => {
    try {
      const assets: WorkAsset[] = await api.getWorkAssets(item.workId);
      const asset = assets.find((candidate) => candidate.id === item.assetId);
      if (!asset) return null;
      const videoPoster = resolveMediaKind(asset) === "video";
      const url = videoPoster
        ? asset.poster_url || asset.thumb_url
        : asset.preview_url || asset.original_url || asset.thumb_url;
      if (!url) return null;
      await decodeImage(url);
      return { item, url, videoPoster };
    } catch {
      return null;
    }
  })();
  resolvedSlideCache.set(key, request);
  return request;
}

function PlayIcon() {
  return <Play className="h-5 w-5" fill="currentColor" aria-hidden="true" />;
}

function PauseIcon() {
  return <Pause className="h-5 w-5" fill="currentColor" aria-hidden="true" />;
}

export default function SlideshowPlayer({ items, startIndex, open, onClose }: {
  items: SlideItem[];
  startIndex: number;
  open: boolean;
  onClose: () => void;
}) {
  const t = useT();
  const { config } = useSlideshowConfig();
  const { mounted, closing } = usePresence(open, motionTokens.duration.base);
  const [containerEl, setContainerEl] = useState<HTMLDivElement | null>(null);
  const [index, setIndex] = useState(startIndex);
  const [paused, setPaused] = useState(false);
  const [loading, setLoading] = useState(true);
  const [broken, setBroken] = useState(false);
  const [currentVisual, setCurrentVisual] = useState<ResolvedSlide | null>(null);
  const [previousVisual, setPreviousVisual] = useState<ResolvedSlide | null>(null);
  const [controlsVisible, setControlsVisible] = useState(true);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const requestSequence = useRef(0);
  const indexRef = useRef(index);
  const currentVisualRef = useRef<ResolvedSlide | null>(null);
  const transitionTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const controlsTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const touchStartRef = useRef<{ x: number; y: number } | null>(null);
  const total = items.length;

  indexRef.current = index;
  currentVisualRef.current = currentVisual;

  const armControls = useCallback(() => {
    setControlsVisible(true);
    if (controlsTimerRef.current) clearTimeout(controlsTimerRef.current);
    controlsTimerRef.current = setTimeout(() => setControlsVisible(false), 3200);
  }, []);

  const preloadNeighbors = useCallback((activeIndex: number) => {
    if (total <= 1) return;
    for (const candidate of [(activeIndex + 1) % total, (activeIndex - 1 + total) % total]) {
      void resolveSlide(items[candidate]);
    }
  }, [items, total]);

  const navigateTo = useCallback(async (nextIndex: number, force = false) => {
    if (!items[nextIndex]) return;
    const sequence = ++requestSequence.current;
    setLoading(true);
    setBroken(false);
    const resolved = await resolveSlide(items[nextIndex], force);
    if (sequence !== requestSequence.current) return;
    setLoading(false);
    if (!resolved) {
      indexRef.current = nextIndex;
      setIndex(nextIndex);
      setBroken(true);
      return;
    }
    if (transitionTimerRef.current) clearTimeout(transitionTimerRef.current);
    setPreviousVisual(currentVisualRef.current);
    setCurrentVisual(resolved);
    setIndex(nextIndex);
    preloadNeighbors(nextIndex);
    transitionTimerRef.current = setTimeout(() => setPreviousVisual(null), motionTokens.duration.slow + 40);
  }, [items, preloadNeighbors]);

  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const stepNext = useCallback(() => {
    if (total <= 0) return;
    const next = indexRef.current + 1;
    if (next >= total) {
      if (config.slideLoop) void navigateTo(0);
      else onCloseRef.current();
      return;
    }
    void navigateTo(next);
  }, [config.slideLoop, navigateTo, total]);

  const stepPrev = useCallback(() => {
    if (total <= 0) return;
    const previous = indexRef.current - 1;
    if (previous < 0) {
      if (config.slideLoop) void navigateTo(total - 1);
      return;
    }
    void navigateTo(previous);
  }, [config.slideLoop, navigateTo, total]);

  useEffect(() => {
    if (!open || !items[startIndex]) return;
    requestSequence.current += 1;
    setIndex(startIndex);
    setPaused(false);
    setBroken(false);
    setCurrentVisual(null);
    setPreviousVisual(null);
    setControlsVisible(true);
    void navigateTo(startIndex);
    armControls();
  }, [armControls, items, navigateTo, open, startIndex]);

  useEffect(() => {
    if (!open || paused || loading || total <= 1) return;
    const timer = setTimeout(stepNext, config.slideDwellMs);
    return () => clearTimeout(timer);
  }, [config.slideDwellMs, index, loading, open, paused, stepNext, total]);

  const stepNextRef = useRef(stepNext);
  const stepPrevRef = useRef(stepPrev);
  stepNextRef.current = stepNext;
  stepPrevRef.current = stepPrev;

  useEffect(() => {
    const element = containerEl;
    if (!open || !element) return;
    previousFocusRef.current = document.activeElement as HTMLElement | null;
    element.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      armControls();
      if (event.key === "Escape") onCloseRef.current();
      else if (event.key === "ArrowRight") stepNextRef.current();
      else if (event.key === "ArrowLeft") stepPrevRef.current();
      else if (event.key === " " || event.code === "Space" || event.key === "Spacebar") {
        event.preventDefault();
        setPaused((value) => !value);
      } else if (event.key === "Tab") {
        const focusable = element.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
        );
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (document.activeElement === element) {
          event.preventDefault();
          (event.shiftKey ? last : first).focus();
        } else if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previousFocusRef.current?.focus();
    };
  }, [armControls, containerEl, open]);

  useEffect(() => () => {
    requestSequence.current += 1;
    if (transitionTimerRef.current) clearTimeout(transitionTimerRef.current);
    if (controlsTimerRef.current) clearTimeout(controlsTimerRef.current);
  }, []);

  if (!mounted || total === 0) return null;

  const current = items[index];
  const kenBurns = config.slideTransition === "kenburns";
  const controlsClass = controlsVisible ? "opacity-100" : "pointer-events-none opacity-0";

  return (
    <div
      ref={setContainerEl}
      role="dialog"
      aria-modal="true"
      aria-label={t("slideshow.open")}
      tabIndex={-1}
      onPointerMove={armControls}
      onPointerDown={armControls}
      onTouchStart={(event) => {
        const touch = event.touches[0];
        if (touch) touchStartRef.current = { x: touch.clientX, y: touch.clientY };
      }}
      onTouchEnd={(event) => {
        const start = touchStartRef.current;
        const touch = event.changedTouches[0];
        touchStartRef.current = null;
        if (!start || !touch) return;
        const deltaX = touch.clientX - start.x;
        const deltaY = touch.clientY - start.y;
        if (Math.abs(deltaX) < 48 || Math.abs(deltaX) <= Math.abs(deltaY)) return;
        if (deltaX < 0) stepNext(); else stepPrev();
      }}
      className={`fixed inset-0 z-[60] bg-black outline-none ${closing ? "overlay-backdrop-exit" : "overlay-backdrop"}`}
    >
      <div data-testid="slideshow-stage" className="relative h-full w-full overflow-hidden">
        {currentVisual ? (
          <img
            data-testid="slideshow-backdrop"
            src={currentVisual.url}
            alt=""
            loading="eager"
            decoding="async"
            className="no-outline absolute inset-[-5%] h-[110%] w-[110%] scale-110 object-cover opacity-35 blur-3xl"
            aria-hidden="true"
          />
        ) : (
          <div data-testid="slideshow-backdrop" className="absolute inset-0 bg-black" aria-hidden="true" />
        )}
        <div className="absolute inset-0 bg-black/35" aria-hidden="true" />

        {previousVisual ? (
          <div className="slide-layer absolute inset-0 flex items-center justify-center opacity-0" aria-hidden="true">
            <img src={previousVisual.url} alt="" loading="eager" decoding="async" className="no-outline max-h-[calc(100vh-8rem)] max-w-full object-contain" />
          </div>
        ) : null}

        <div
          key={currentVisual ? slideKey(currentVisual.item) : "loading"}
          data-slideshow-foreground="true"
          className="slideshow-foreground-enter absolute inset-x-4 bottom-24 top-4 flex items-center justify-center sm:inset-x-16 sm:bottom-28 sm:top-8"
        >
          {currentVisual ? (
            <img
              src={currentVisual.url}
              alt={currentVisual.item.title || ""}
              loading="eager"
              decoding="async"
              className={`no-outline h-full w-full object-contain drop-shadow-2xl ${kenBurns && !currentVisual.videoPoster ? "slide-kenburns" : ""}`}
              style={kenBurns && !currentVisual.videoPoster ? ({ "--slide-dwell": `${config.slideDwellMs}ms` } as CSSProperties) : undefined}
              onError={() => void navigateTo(indexRef.current, true)}
            />
          ) : loading ? (
            <span className="rounded-md bg-black/65 px-4 py-2 text-sm text-white/80">{t("common.loading")}</span>
          ) : null}
        </div>

        {currentVisual?.videoPoster ? (
          <a
            href={`/admin/works/${currentVisual.item.workId}`}
            className="absolute bottom-32 left-1/2 z-20 -translate-x-1/2 rounded-md border border-white/25 bg-black/75 px-4 py-2 text-sm font-medium text-white hover:bg-black focus-visible:ring-2 focus-visible:ring-white"
          >
            {t("media.open_video")}
          </a>
        ) : null}

        {broken ? (
          <div className="absolute inset-0 z-20 flex items-center justify-center">
            <div className="rounded-lg border border-white/20 bg-black/80 p-5 text-center text-sm text-white">
              <p>{t("works.na")}</p>
              <button type="button" className="mt-3 rounded-md border border-white/25 px-3 py-2" onClick={() => void navigateTo(indexRef.current, true)}>
                {t("common.retry")}
              </button>
            </div>
          </div>
        ) : null}
      </div>

      <div className={`absolute right-3 top-3 z-30 flex gap-2 transition-opacity duration-200 ${controlsClass}`}>
        <button
          type="button"
          onClick={() => setPaused((value) => !value)}
          aria-label={paused ? t("slideshow.play") : t("slideshow.pause")}
          className="flex h-10 w-10 items-center justify-center rounded-md border border-white/20 bg-black/60 text-white shadow-lg hover:bg-black/80"
        >
          {paused ? <PlayIcon /> : <PauseIcon />}
        </button>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("slideshow.close")}
          className="flex h-10 w-10 items-center justify-center rounded-md border border-white/20 bg-black/60 text-white shadow-lg hover:bg-black/80"
        >
          <X className="h-5 w-5" aria-hidden="true" />
        </button>
      </div>

      {total > 1 ? (
        <>
          <button
            type="button"
            onClick={stepPrev}
            aria-label={t("slideshow.prev")}
            className={`absolute left-3 top-1/2 z-30 flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-md border border-white/20 bg-black/60 text-white shadow-lg transition-opacity duration-200 hover:bg-black/80 ${controlsClass}`}
          >
            <ArrowIcon direction="left" />
          </button>
          <button
            type="button"
            onClick={stepNext}
            aria-label={t("slideshow.next")}
            className={`absolute right-3 top-1/2 z-30 flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-md border border-white/20 bg-black/60 text-white shadow-lg transition-opacity duration-200 hover:bg-black/80 ${controlsClass}`}
          >
            <ArrowIcon direction="right" />
          </button>
        </>
      ) : null}

      <div className={`absolute inset-x-0 bottom-0 z-30 border-t border-white/15 bg-black/75 px-3 py-3 text-white transition-opacity duration-200 ${controlsClass}`}>
        <div className="mx-auto flex max-w-5xl items-end gap-4">
          {config.slideShowMeta ? (
            <div className="hidden min-w-0 flex-1 sm:block">
              {current?.title ? <div className="truncate text-sm font-medium">{current.title}</div> : null}
              {current?.creatorName ? <div className="truncate text-xs text-white/70">{current.creatorName}</div> : null}
            </div>
          ) : <div className="hidden flex-1 sm:block" />}
          <ul
            role="list"
            aria-label={t("slideshow.thumbnails")}
            className="flex min-w-0 flex-1 items-center gap-2 overflow-x-auto [scrollbar-width:none] sm:max-w-xl"
          >
            {items.map((item, itemIndex) => (
              <li key={slideKey(item)} className="shrink-0">
                <button
                  type="button"
                  aria-label={t("slideshow.go_to", { index: itemIndex + 1 })}
                  aria-current={itemIndex === index ? "true" : undefined}
                  onClick={() => void navigateTo(itemIndex)}
                  className={`h-12 w-12 overflow-hidden rounded-md border bg-black outline-none transition-[border-color,opacity,transform] focus-visible:ring-2 focus-visible:ring-white ${itemIndex === index ? "border-white opacity-100" : "border-white/25 opacity-55 hover:opacity-90"}`}
                >
                  <WorkMediaThumbnail assetId={item.assetId} alt="" className="h-full w-full object-cover" />
                </button>
              </li>
            ))}
          </ul>
          <div className="shrink-0 text-xs tabular-nums text-white/75">
            {t("slideshow.counter", { current: index + 1, total })}
          </div>
        </div>
      </div>
    </div>
  );
}

export { SlideshowPlayer };
