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
export default function MotionNumber({
  value,
  className,
  format = (number) => String(Math.round(number)),
  animateInitial = false,
  animationKey,
}: {
  value: number;
  className?: string;
  format?: (value: number) => string;
  animateInitial?: boolean;
  animationKey?: string | number;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const prev = useRef<number | null>(null);
  const formatRef = useRef(format);
  formatRef.current = format;

  useEffect(() => {
    const el = ref.current;
    const from = prev.current;
    prev.current = value;
    if (!el || (from === null && !animateInitial) || from === value) return;
    countUp(el, from ?? 0, value, { format: (number) => formatRef.current(number) });
  }, [animateInitial, animationKey, value]);

  return <span ref={ref} className={className}>{format(value)}</span>;
}
