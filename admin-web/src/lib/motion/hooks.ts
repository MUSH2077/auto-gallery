"use client";
// Shared motion hooks. usePresence gives conditionally-rendered overlays an
// exit phase (React unmounts instantly otherwise); useEnterOnce guards
// entrance animations against replaying on poll-driven re-renders — the #1
// motion risk in this app (see docs/frontend-motion-audit.md §3.3.1).

import { useCallback, useEffect, useRef, useState } from "react";

import { motionConfig } from "./config";
import { motionTokens } from "./tokens";

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
 * Returns a predicate that is true only the FIRST time a key is seen in this
 * component instance. Use it to play entrance animations once per item and
 * never again on refetch/poll re-renders:
 *
 * ```tsx
 * const isNew = useEnterOnce();
 * items.map((it) => <Row key={it.id} animate={isNew(it.id)} />)
 * ```
 */
export function useEnterOnce(): (key: string | number) => boolean {
  const seen = useRef<Set<string | number>>(new Set());
  return useCallback((key: string | number) => {
    if (seen.current.has(key)) return false;
    seen.current.add(key);
    return true;
  }, []);
}
