"use client";
// Shared motion hooks. usePresence gives conditionally-rendered overlays an
// exit phase (React unmounts instantly otherwise); useEnterOnce guards
// entrance animations against replaying on poll-driven re-renders — the #1
// motion risk in this app (see docs/frontend-motion-audit.md §3.3.1).

import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";

import { motionConfig } from "./config";
import { motionTokens, staggerDelay } from "./tokens";

/**
 * Keep an element mounted through its exit animation.
 *
 * ```tsx
 * const { mounted, closing } = usePresence(open);
 * if (!mounted) return null;
 * return <div className={`popover ${closing ? "popover-exit" : ""}`}>…</div>;
 * ```
 *
 * `closing` flips true when `open` goes false; the element unmounts after
 * `exitDuration` (0 under reduced motion — exits must never linger).
 */
export function usePresence(
  open: boolean,
  exitDuration: number = motionTokens.duration.fast,
): { mounted: boolean; closing: boolean } {
  const [mounted, setMounted] = useState(open);
  const [closing, setClosing] = useState(false);

  useEffect(() => {
    if (open) {
      setMounted(true);
      setClosing(false);
      return;
    }
    if (!mounted) return;
    setClosing(true);
    const ms = motionConfig.prefersReduced() ? 0 : exitDuration;
    const timer = setTimeout(() => {
      setMounted(false);
      setClosing(false);
    }, ms);
    return () => clearTimeout(timer);
    // `mounted` is only a re-entry guard here; reacting to it would clear
    // the exit timer the moment it fires.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, exitDuration]);

  return { mounted, closing };
}

/**
 * Marks list keys as "seen" after each commit and keeps their entrance state
 * alive through the shared animation window. This prevents an immediate
 * progress/state re-render from cancelling a just-started animation while
 * ensuring later poll/refetch renders do not replay old keys.
 * Render-phase pure (seen is only mutated in the effect), so StrictMode
 * double renders stay consistent.
 *
 * ```tsx
 * const isNew = useEnterOnce(items.map((it) => it.id));
 * items.map((it) => <Row key={it.id} className={isNew(it.id) ? "fade-in" : ""} />)
 * ```
 */
export function useEnterOnce(
  keys: ReadonlyArray<string | number>,
  maxStaggerDelay: number = motionTokens.stagger.cap,
): (key: string | number) => boolean {
  const seen = useRef<Set<string | number>>(new Set());
  const entering = useRef<Set<string | number>>(new Set());
  const timers = useRef<Set<ReturnType<typeof setTimeout>>>(new Set());

  useEffect(() => {
    const newKeys = keys.filter((key) => !seen.current.has(key));
    if (!newKeys.length) return;
    for (const key of newKeys) {
      seen.current.add(key);
      entering.current.add(key);
    }
    const timer = setTimeout(() => {
      for (const key of newKeys) entering.current.delete(key);
      timers.current.delete(timer);
    }, motionTokens.duration.enter + maxStaggerDelay + motionTokens.duration.instant);
    timers.current.add(timer);
  }, [keys, maxStaggerDelay]);

  useEffect(() => () => {
    for (const timer of timers.current) clearTimeout(timer);
    timers.current.clear();
    seen.current.clear();
    entering.current.clear();
  }, []);

  return useCallback(
    (key: string | number) => !seen.current.has(key) || entering.current.has(key),
    [],
  );
}

export interface StaggeredEntranceProps {
  className: string;
  style?: CSSProperties;
}

/**
 * Returns the shared one-shot stagger props for a dynamic collection.
 * Existing keys never replay after polling/refetch; newly observed keys enter
 * in their current visual order.
 */
export function useStaggeredEntrance(
  keys: ReadonlyArray<string | number>,
): (key: string | number, index: number) => StaggeredEntranceProps {
  const isNew = useEnterOnce(keys);
  return useCallback((key: string | number, index: number) => (
    isNew(key)
      ? {
          className: "page-item",
          style: { "--delay": staggerDelay(index) } as CSSProperties,
        }
      : { className: "" }
  ), [isNew]);
}
