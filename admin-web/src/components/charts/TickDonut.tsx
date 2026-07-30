"use client";

import { useMemo, useState } from "react";

import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";

import { stableTopWithOther } from "./chartMath";
import type { ChartDatum } from "./types";
import { useChartTheme } from "./useChartTheme";

interface DonutSegment extends ChartDatum {
  ticks: number;
  percent: number;
}

function allocateTicks(data: ChartDatum[], total: number): DonutSegment[] {
  if (total <= 0) return data.map((item) => ({ ...item, ticks: 0, percent: 0 }));
  const raw = data.map((item) => ({
    ...item,
    rawTicks: (item.value / total) * 100,
    percent: (item.value / total) * 100,
  }));
  const allocated = raw.map((item) => ({ ...item, ticks: Math.floor(item.rawTicks) }));
  let remaining = 100 - allocated.reduce((sum, item) => sum + item.ticks, 0);
  const remainderOrder = allocated
    .map((item, index) => ({ index, remainder: item.rawTicks - item.ticks }))
    .sort((left, right) => right.remainder - left.remainder);
  for (let index = 0; index < remaining; index += 1) {
    allocated[remainderOrder[index % remainderOrder.length].index].ticks += 1;
  }
  return allocated;
}

export default function TickDonut({
  data,
  otherLabel,
  formatValue,
}: {
  data: ChartDatum[];
  otherLabel: string;
  formatValue: (value: number) => string;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const theme = useChartTheme();
  const [activeId, setActiveId] = useState<string | null>(null);
  const total = data.reduce((sum, item) => sum + item.value, 0);
  const segments = useMemo(() => {
    const { top, remainder } = stableTopWithOther(data, (item) => item.value, 5);
    const chartData = remainder.length
      ? [
        ...top,
        {
          id: "other",
          label: otherLabel,
          value: remainder.reduce((sum, item) => sum + item.value, 0),
          colorRole: "neutral" as const,
          description: remainder.map((item) => item.label).join(", "),
        },
      ]
      : top;
    return allocateTicks(chartData, total);
  }, [data, otherLabel, total]);

  const ticks = useMemo(() => {
    const result: Array<{ segment: DonutSegment; index: number }> = [];
    for (const segment of segments) {
      for (let index = 0; index < segment.ticks; index += 1) {
        result.push({ segment, index: result.length });
      }
    }
    return result;
  }, [segments]);

  const selected = segments.find((segment) => segment.id === activeId) || null;

  return (
    <div className="grid items-center gap-5 2xl:grid-cols-[minmax(15rem,0.9fr)_minmax(12rem,1.1fr)]" data-chart-kind="tick-donut">
      <div className="relative mx-auto aspect-square w-full max-w-[19rem]">
        <svg viewBox="0 0 320 320" className="h-full w-full" aria-hidden>
          {ticks.map(({ segment, index }) => {
            const angle = (index / 100) * Math.PI * 2 - Math.PI / 2;
            const inner = 102;
            const outer = index % 10 === 0 ? 126 : 120;
            const active = activeId === null || activeId === segment.id;
            return (
              <line
                key={`${segment.id}:${index}`}
                x1={(160 + Math.cos(angle) * inner).toFixed(2)}
                y1={(160 + Math.sin(angle) * inner).toFixed(2)}
                x2={(160 + Math.cos(angle) * outer).toFixed(2)}
                y2={(160 + Math.sin(angle) * outer).toFixed(2)}
                stroke={theme.colorFor(segment.colorRole)}
                strokeWidth={index % 10 === 0 ? 2.2 : 1.4}
                strokeLinecap="round"
                opacity={active ? 1 : 0.18}
              />
            );
          })}
          <circle cx="160" cy="160" r="88" fill={theme.subtle} stroke={theme.border} />
          <text x="160" y="153" textAnchor="middle" fill={theme.text} fontSize="24" fontWeight="800">
            {selected ? fmt.number(selected.percent, { maximumFractionDigits: 1 }) : formatValue(total)}
          </text>
          <text x="160" y="176" textAnchor="middle" fill={theme.muted} fontSize="11" fontWeight="600">
            {selected ? `${selected.label} · %` : t("charts.hundred_ticks")}
          </text>
        </svg>
      </div>

      <div className="space-y-1">
        {segments.map((segment) => (
          <button
            key={segment.id}
            type="button"
            className="grid min-h-11 w-full grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 rounded-md px-2 text-left hover:bg-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
            onPointerEnter={() => setActiveId(segment.id)}
            onPointerLeave={() => setActiveId(null)}
            onFocus={() => setActiveId(segment.id)}
            onBlur={() => setActiveId(null)}
            onClick={() => setActiveId((current) => current === segment.id ? null : segment.id)}
            aria-pressed={activeId === segment.id}
            title={segment.description}
          >
            <span className="h-3 w-3 rounded-full" style={{ backgroundColor: theme.colorFor(segment.colorRole) }} aria-hidden />
            <span className="truncate text-sm font-semibold text-fg">{segment.label}</span>
            <span className="text-right text-xs tabular-nums text-muted">
              {formatValue(segment.value)} · {fmt.number(segment.percent, { maximumFractionDigits: 1 })}%
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
