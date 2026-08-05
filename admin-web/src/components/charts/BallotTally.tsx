"use client";

import Link from "next/link";
import { useMemo, type CSSProperties } from "react";

import MotionNumber from "@/components/MotionNumber";
import { useI18nFormat } from "@/lib/i18n-format";
import { motionConfig, useViewportReveal } from "@/lib/motion";

import type { ChartDatum } from "./types";
import { useChartTheme } from "./useChartTheme";

export default function BallotTally({
  data,
  total,
}: {
  data: ChartDatum[];
  total: number;
}) {
  const fmt = useI18nFormat();
  const theme = useChartTheme();
  const dataKey = useMemo(() => data.map((item) => item.id).join("|"), [data]);
  const reveal = useViewportReveal<HTMLDivElement>(dataKey);
  const animate = reveal.revealed && motionConfig.shouldAnimate();

  return (
    <div ref={reveal.ref} className="space-y-2" data-chart-kind="ballot-tally">
      {data.slice(0, 6).map((item, rowIndex) => {
        const percent = total > 0 ? Math.min(100, Math.max(0, (item.value / total) * 100)) : 0;
        const color = theme.colorFor(item.colorRole || "accent");
        const tally = (
          <span
            aria-hidden
            className="relative col-span-2 col-start-1 row-start-2 block h-6 min-w-0 overflow-hidden border-b border-border/70"
            style={{
              backgroundImage: "repeating-linear-gradient(to right, rgb(var(--ag-border)) 0 1px, transparent 1px 1%)",
            }}
          >
            <span
              className={`absolute inset-y-0 left-0 origin-left ${animate ? "chart-tally-enter" : ""}`}
              style={{
                width: `${percent}%`,
                backgroundColor: color,
                maskImage: "repeating-linear-gradient(to right, black 0 1px, transparent 1px 1%)",
                WebkitMaskImage: "repeating-linear-gradient(to right, black 0 1px, transparent 1px 1%)",
                "--chart-delay": `${Math.min(rowIndex * 110, 550)}ms`,
              } as CSSProperties}
            />
          </span>
        );
        const row = (
          <>
            <span className="min-w-0 truncate text-xs font-semibold text-fg">#{item.label}</span>
            {tally}
            <span className="text-right font-mono text-xs font-semibold tabular-nums text-fg">
              {reveal.revealed ? (
                <MotionNumber
                  value={percent}
                  animateInitial
                  animationKey={dataKey}
                  format={(value) => `${fmt.number(value, { maximumFractionDigits: 1 })}%`}
                />
              ) : `${fmt.number(percent, { maximumFractionDigits: 1 })}%`}
            </span>
          </>
        );
        const className = "grid min-h-11 w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 rounded-md px-2 py-1 transition-colors hover:bg-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus";
        return item.href ? (
          <Link
            key={item.id}
            href={item.href}
            className={className}
            aria-label={item.description || `${item.label}: ${fmt.number(item.value)}, ${fmt.number(percent, { maximumFractionDigits: 1 })}%`}
          >
            {row}
          </Link>
        ) : (
          <div key={item.id} className={className} aria-label={item.description}>
            {row}
          </div>
        );
      })}
    </div>
  );
}
