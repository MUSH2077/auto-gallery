"use client";

import Link from "next/link";
import { useMemo, type CSSProperties } from "react";

import { useI18nFormat } from "@/lib/i18n-format";
import { motionConfig, useViewportReveal } from "@/lib/motion";

import { niceUnit } from "./chartMath";
import type { ChartDatum } from "./types";
import { useChartTheme } from "./useChartTheme";

function TickField({
  value,
  maximum,
  unit,
  color,
  entering,
  rowIndex,
}: {
  value: number;
  maximum: number;
  unit: number;
  color: string;
  entering: boolean;
  rowIndex: number;
}) {
  const fullTicks = Math.floor(value / unit);
  const remainder = value % unit;
  const maximumTicks = Math.max(1, Math.ceil(maximum / unit));
  const ticks = Array.from({ length: maximumTicks }, (_, index) => {
    const complete = index < fullTicks;
    const partial = index === fullTicks && remainder > 0;
    return (
      <span
        key={index}
        className={`block h-5 w-px origin-bottom rounded-full ${entering ? "chart-tick-enter" : ""}`}
        style={{
          backgroundColor: complete || partial ? color : "rgb(var(--ag-border))",
          "--tick-scale": partial ? Math.max(0.2, remainder / unit) : 1,
          transform: "scaleY(var(--tick-scale))",
          opacity: complete || partial ? 1 : 0.45,
          "--chart-delay": `${Math.min(rowIndex * 96 + index * 10, 720)}ms`,
        } as CSSProperties}
      />
    );
  });

  return (
    <div
      aria-hidden
      className="col-span-2 col-start-1 row-start-2 grid h-7 min-w-0 items-end gap-1 overflow-hidden border-b border-border/70"
      style={{ gridTemplateColumns: `repeat(${maximumTicks}, minmax(1px, 1fr))` }}
    >
      {ticks}
    </div>
  );
}

export default function TickRows({
  data,
  maximumTicks = 24,
}: {
  data: ChartDatum[];
  maximumTicks?: number;
}) {
  const fmt = useI18nFormat();
  const theme = useChartTheme();
  const maximum = useMemo(
    () => data.reduce((current, item) => Math.max(current, item.value), 0),
    [data],
  );
  const unit = niceUnit(maximum, maximumTicks);
  const dataKey = useMemo(() => data.map((item) => item.id).join("|"), [data]);
  const reveal = useViewportReveal<HTMLDivElement>(dataKey);
  const animate = reveal.revealed && motionConfig.shouldAnimate();

  return (
    <div ref={reveal.ref} className="space-y-2" data-chart-kind="tick-rows" data-chart-unit={unit}>
      {data.map((item, rowIndex) => {
        const content = (
          <>
            <span className="min-w-0 truncate text-left text-xs font-semibold text-fg">{item.label}</span>
            <TickField
              value={item.value}
              maximum={maximum}
              unit={unit}
              color={theme.colorFor(item.colorRole)}
              entering={animate}
              rowIndex={rowIndex}
            />
            <span className="text-right font-mono text-xs font-semibold tabular-nums text-fg">
              {fmt.number(item.value)}
            </span>
          </>
        );
        const className = "grid min-h-11 w-full grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 rounded-md px-2 py-1 transition-colors hover:bg-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus";
        return item.href ? (
          <Link
            key={item.id}
            href={item.href}
            className={className}
            aria-label={item.description || `${item.label}: ${fmt.number(item.value)}`}
          >
            {content}
          </Link>
        ) : (
          <div key={item.id} className={className} aria-label={item.description}>
            {content}
          </div>
        );
      })}
    </div>
  );
}
