"use client";
import { useEffect, useRef } from "react";
import { countUp } from "@/lib/motion";

/**
 * Numeric readout that rolls to new values (dashboard metrics fed by
 * polling). First render is static — SSR output must match and initial
 * page load must not flash a count-up — only subsequent value CHANGES
 * animate. Reduced motion / low-end fall back to instant text inside
 * countUp itself.
 */
export default function MotionNumber({ value, className }: { value: number; className?: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const prev = useRef<number | null>(null);

  useEffect(() => {
    const el = ref.current;
    const from = prev.current;
    prev.current = value;
    if (!el || from === null || from === value) return;
    countUp(el, from, value);
  }, [value]);

  return <span ref={ref} className={className}>{value}</span>;
}
