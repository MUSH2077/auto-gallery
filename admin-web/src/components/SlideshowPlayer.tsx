"use client";

// Universal fullscreen slideshow (Task 6). One implementation, mounted once
// per host page via useSlideshow() and toggled open/closed — the creator
// detail, works list, and tag detail pages all render the same component.
//
// State machine:
//  - `index`: the logical current slide. Source of truth for the dwell
//    timer, the counter, and prev/next math.
//  - Two persistent slots (A/B), each rendering one SlideLayer. `frontSlot`
//    says which slot is opaque (the visible slide); navigating flips it and
//    assigns the new index to whichever slot was in back, so the outgoing
//    layer just fades via `.slide-layer`'s CSS transition while the
//    incoming slot's <img> remounts (keyed by a monotonic `gen` counter) to
//    fetch a fresh signed preview URL and restart the Ken Burns keyframe.
//  - `paused`: Space toggles it; the autoplay effect checks it before
//    scheduling the next dwell timeout.
//
// Teardown contract: the autoplay setTimeout is recreated by its effect on
// every index change and is cleared by that same effect's cleanup on pause,
// close, and unmount. The keydown listener + focus management is bound once
// per open session (deps: [open] only, reading live callbacks through refs
// so incidental parent re-renders — e.g. background polling on the host
// page — never rebind it or steal focus back mid-session) and is removed,
// with focus restored to the trigger, on close and unmount alike. React
// always runs effect cleanups on unmount regardless of document.hidden, so
// backgrounding the tab and then navigating away tears down cleanly too.

import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { api } from "@/lib/api";
import type { WorkAsset } from "@/lib/api/endpoints/works";
import { usePresence, motionTokens } from "@/lib/motion";
import { useShowcaseConfig } from "@/lib/showcase/config";
import { useT } from "@/lib/i18n";
import { ArrowIcon } from "@/components/WorkViewerParts";

export interface SlideItem {
  assetId: string;
  workId: string;
  title?: string | null;
  creatorName?: string | null;
}

// A signed preview URL that keeps failing (expired token, clock skew, the
// asset itself is unreachable) must not turn into an unbounded refetch loop
// hammering the backend. Mirrors ShowcaseCanvas's MAX_AUTO_REFETCH_STREAK
// (Task 5) — bounded per slide, resets the moment that slide loads cleanly.
const MAX_LOAD_RETRY_STREAK = 2;

async function fetchPreviewUrl(workId: string, assetId: string): Promise<string | null> {
  try {
    const assets: WorkAsset[] = await api.getWorkAssets(workId);
    return assets.find((a) => a.id === assetId)?.preview_url ?? null;
  } catch {
    return null;
  }
}

function PlayIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor" aria-hidden>
      <path d="M8 5v14l11-7z" />
    </svg>
  );
}

function PauseIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="currentColor" aria-hidden>
      <path d="M6 5h4v14H6zM14 5h4v14h-4z" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
      <path d="M6 6l12 12M18 6L6 18" />
    </svg>
  );
}

function SlideLayer({ item, gen, front, kenBurns, dwellMs }: {
  item: SlideItem;
  gen: number;
  front: boolean;
  kenBurns: boolean;
  dwellMs: number;
}) {
  const t = useT();
  const [url, setUrl] = useState<string | null>(null);
  const [broken, setBroken] = useState(false);
  const retryRef = useRef(0);
  const cancelledRef = useRef(false);
  const workId = item.workId;
  const assetId = item.assetId;

  useEffect(() => {
    cancelledRef.current = false;
    retryRef.current = 0;
    setUrl(null);
    setBroken(false);
    fetchPreviewUrl(workId, assetId).then((resolved) => {
      if (cancelledRef.current) return;
      if (resolved) setUrl(resolved);
      else setBroken(true);
    });
    return () => {
      cancelledRef.current = true;
    };
    // `gen` bumps every time this slot is assigned a slide (including being
    // reassigned the *same* index later in a loop) — re-fetching then keeps
    // the signed URL fresh rather than reusing one that may have expired.
  }, [gen, workId, assetId]);

  const handleError = useCallback(() => {
    if (cancelledRef.current) return;
    retryRef.current += 1;
    if (retryRef.current > MAX_LOAD_RETRY_STREAK) {
      setBroken(true);
      return;
    }
    fetchPreviewUrl(workId, assetId).then((resolved) => {
      if (cancelledRef.current) return;
      if (resolved) setUrl(resolved);
      else setBroken(true);
    });
  }, [workId, assetId]);

  return (
    <div className={`slide-layer absolute inset-0 flex items-center justify-center ${front ? "opacity-100" : "opacity-0"}`}>
      {url && !broken && (
        <img
          key={gen}
          src={url}
          alt={item.title || ""}
          className={`max-h-screen max-w-full object-contain ${front && kenBurns ? "slide-kenburns" : ""}`}
          style={front && kenBurns ? ({ "--slide-dwell": `${dwellMs}ms` } as CSSProperties) : undefined}
          onError={handleError}
        />
      )}
      {broken && <div className="text-sm text-white/50">{t("works.na")}</div>}
    </div>
  );
}

export default function SlideshowPlayer({ items, startIndex, open, onClose }: {
  items: SlideItem[];
  startIndex: number;
  open: boolean;
  onClose: () => void;
}) {
  const t = useT();
  const { config } = useShowcaseConfig();
  const { mounted, closing } = usePresence(open, motionTokens.duration.base);

  const containerRef = useRef<HTMLDivElement>(null);
  const prevFocusRef = useRef<HTMLElement | null>(null);

  const [index, setIndex] = useState(startIndex);
  const [paused, setPaused] = useState(false);

  const [slotAIndex, setSlotAIndex] = useState(startIndex);
  const [slotBIndex, setSlotBIndex] = useState(startIndex);
  const [slotAGen, setSlotAGen] = useState(0);
  const [slotBGen, setSlotBGen] = useState(0);
  const [frontSlot, setFrontSlot] = useState<0 | 1>(0);
  const frontSlotRef = useRef<0 | 1>(0);
  const navGenRef = useRef(0);
  const indexRef = useRef(index);
  indexRef.current = index;

  const total = items.length;

  // Fresh session every time the caller opens the player — reset position,
  // pause state, and both slots so a previous session's images don't flash
  // before the new fetch resolves.
  useEffect(() => {
    if (!open) return;
    navGenRef.current += 1;
    const gen = navGenRef.current;
    setIndex(startIndex);
    setPaused(false);
    setSlotAIndex(startIndex);
    setSlotBIndex(startIndex);
    setSlotAGen(gen);
    setSlotBGen(gen);
    setFrontSlot(0);
    frontSlotRef.current = 0;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, startIndex]);

  const navigateTo = useCallback((newIndex: number) => {
    navGenRef.current += 1;
    const gen = navGenRef.current;
    const backSlot = frontSlotRef.current === 0 ? 1 : 0;
    if (backSlot === 0) {
      setSlotAIndex(newIndex);
      setSlotAGen(gen);
    } else {
      setSlotBIndex(newIndex);
      setSlotBGen(gen);
    }
    frontSlotRef.current = backSlot;
    setFrontSlot(backSlot);
    setIndex(newIndex);
  }, []);

  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const stepNext = useCallback(() => {
    if (total <= 0) return;
    const next = indexRef.current + 1;
    if (next >= total) {
      if (config.slideLoop) navigateTo(0);
      else onCloseRef.current();
      return;
    }
    navigateTo(next);
  }, [total, config.slideLoop, navigateTo]);

  const stepPrev = useCallback(() => {
    if (total <= 0) return;
    const prev = indexRef.current - 1;
    if (prev < 0) {
      if (config.slideLoop) navigateTo(total - 1);
      return; // no loop: stay put on the first slide
    }
    navigateTo(prev);
  }, [total, config.slideLoop, navigateTo]);

  // Autoplay: one setTimeout per slide. Cleared on index change, pause,
  // close, and unmount — never an interval, so there is nothing to drift.
  useEffect(() => {
    if (!open || paused || total <= 1) return;
    const timer = setTimeout(() => {
      stepNext();
    }, config.slideDwellMs);
    return () => clearTimeout(timer);
  }, [open, paused, index, total, config.slideDwellMs, stepNext]);

  // Focus + keyboard: bound once per open session on the dialog container
  // itself. Reads stepNext/stepPrev/onClose through refs so it is never
  // rebound by an index change, a pause toggle, or an unrelated parent
  // re-render (e.g. background query polling on the host page) — only by
  // `open` actually flipping.
  const stepNextRef = useRef(stepNext);
  stepNextRef.current = stepNext;
  const stepPrevRef = useRef(stepPrev);
  stepPrevRef.current = stepPrev;

  useEffect(() => {
    const el = containerRef.current;
    if (!open || !el) return;
    prevFocusRef.current = document.activeElement as HTMLElement | null;
    el.focus();

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onCloseRef.current();
        return;
      }
      if (e.key === "ArrowRight") {
        stepNextRef.current();
        return;
      }
      if (e.key === "ArrowLeft") {
        stepPrevRef.current();
        return;
      }
      if (e.key === " " || e.code === "Space" || e.key === "Spacebar") {
        e.preventDefault();
        setPaused((p) => !p);
      }
    };
    el.addEventListener("keydown", onKeyDown);
    return () => {
      el.removeEventListener("keydown", onKeyDown);
      prevFocusRef.current?.focus();
    };
  }, [open]);

  if (!mounted || total === 0) return null;

  const current = items[index];
  const kenBurns = config.slideTransition === "kenburns";

  return (
    <div
      ref={containerRef}
      role="dialog"
      aria-modal="true"
      aria-label={t("slideshow.open")}
      tabIndex={-1}
      className={`fixed inset-0 z-[60] bg-black outline-none ${closing ? "overlay-backdrop-exit" : "overlay-backdrop"}`}
    >
      <div className="relative h-full w-full">
        <SlideLayer item={items[slotAIndex]} gen={slotAGen} front={frontSlot === 0} kenBurns={kenBurns} dwellMs={config.slideDwellMs} />
        <SlideLayer item={items[slotBIndex]} gen={slotBGen} front={frontSlot === 1} kenBurns={kenBurns} dwellMs={config.slideDwellMs} />
      </div>

      {config.slideShowMeta && (
        <div className="pointer-events-none absolute inset-x-0 bottom-0 flex items-end justify-between gap-4 bg-gradient-to-t from-black/80 to-transparent p-4 text-white">
          <div className="min-w-0">
            {current?.title && <div className="truncate text-sm font-medium">{current.title}</div>}
            {current?.creatorName && <div className="truncate text-xs text-white/70">{current.creatorName}</div>}
          </div>
          <div className="tabular shrink-0 text-xs text-white/70">
            {t("slideshow.counter", { current: index + 1, total })}
          </div>
        </div>
      )}

      <div className="absolute right-3 top-3 flex gap-2">
        <button
          type="button"
          onClick={() => setPaused((p) => !p)}
          aria-label={paused ? t("slideshow.play") : t("slideshow.pause")}
          className="flex h-10 w-10 items-center justify-center rounded-md border border-white/20 bg-black/50 text-white shadow-lg hover:bg-black/70"
        >
          {paused ? <PlayIcon /> : <PauseIcon />}
        </button>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("slideshow.close")}
          className="flex h-10 w-10 items-center justify-center rounded-md border border-white/20 bg-black/50 text-white shadow-lg hover:bg-black/70"
        >
          <CloseIcon />
        </button>
      </div>

      {total > 1 && (
        <>
          <button
            type="button"
            onClick={stepPrev}
            aria-label={t("slideshow.prev")}
            className="absolute left-3 top-1/2 flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-md border border-white/20 bg-black/50 text-white shadow-lg hover:bg-black/70"
          >
            <ArrowIcon direction="left" />
          </button>
          <button
            type="button"
            onClick={stepNext}
            aria-label={t("slideshow.next")}
            className="absolute right-3 top-1/2 flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-md border border-white/20 bg-black/50 text-white shadow-lg hover:bg-black/70"
          >
            <ArrowIcon direction="right" />
          </button>
        </>
      )}
    </div>
  );
}

// Named export alongside the default so useSlideshow.tsx (Task 6 brief) can
// import it as `{ SlideshowPlayer }`; components/index.ts re-exports the
// default under the same name for the rest of the app.
export { SlideshowPlayer };
