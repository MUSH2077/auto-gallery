"use client";

import Link from "next/link";
import { useMemo } from "react";

import { useI18nFormat } from "@/lib/i18n-format";

import { niceUnit } from "./chartMath";
import type { ChartDatum } from "./types";
import { useChartTheme } from "./useChartTheme";

function TickField({ value, maximum, unit, color }: { value: number; maximum: number; unit: number; color: string }) {
  const fullTicks = Math.floor(value / unit);
  const remainder = value % unit;
  const maximumTicks = Math.max(1, Math.ceil(maximum / unit));
  const ticks = Array.from({ length: maximumTicks }, (_, index) => {
    const complete = index < fullTicks;
    const partial = index === fullTicks && remainder > 0;
    return (
      <span
        key={index}
        className="block h-5 w-px origin-bottom rounded-full"
        style={{
          backgroundColor: complete || partial ? color : "rgb(var(--ag-border))",
          transform: partial ? `scaleY(${Math.max(0.2, remainder / unit)})` : undefined,
          opacity: complete || partial ? 1 : 0.45,
        }}
      />
    );
  });

  return (
    <div
      aria-hidden
      className="grid h-7 min-w-0 flex-1 items-end gap-1 overflow-hidden border-b border-border/70"
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

  return (
    <div className="space-y-2" data-chart-kind="tick-rows" data-chart-unit={unit}>
      {data.map((item) => {
        const content = (
          <>
            <span className="min-w-0 truncate text-left text-xs font-semibold text-fg">{item.label}</span>
            <TickField value={item.value} maximum={maximum} unit={unit} color={theme.colorFor(item.colorRole)} />
            <span className="text-right font-mono text-xs font-semibold tabular-nums text-fg">
              {fmt.number(item.value)}
            </span>
          </>
        );
        const className = "grid min-h-11 w-full grid-cols-[minmax(5.5rem,7rem)_minmax(7rem,1fr)_4rem] items-center gap-3 rounded-md px-2 transition-colors hover:bg-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus";
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
