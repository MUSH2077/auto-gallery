"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
} from "react";

import { useI18nFormat } from "@/lib/i18n-format";
import { motionConfig, useViewportReveal } from "@/lib/motion";

import type { ChartSeriesPoint } from "./types";
import { useChartTheme } from "./useChartTheme";

const WIDTH = 720;
const HEIGHT = 210;
const PAD_X = 24;
const BASELINE = 166;
const TOP = 18;

export default function HairlineSeries({ data }: { data: ChartSeriesPoint[] }) {
  const fmt = useI18nFormat();
  const theme = useChartTheme();
  const [activeIndex, setActiveIndex] = useState(() => Math.max(0, data.length - 1));
  const svgRef = useRef<SVGSVGElement>(null);
  const dataKey = useMemo(() => data.map((point) => point.id).join("|"), [data]);
  const reveal = useViewportReveal<HTMLDivElement>(dataKey);
  const animate = reveal.revealed && motionConfig.shouldAnimate();

  useEffect(() => {
    setActiveIndex((current) => Math.min(Math.max(0, current), Math.max(0, data.length - 1)));
  }, [data.length]);

  const geometry = useMemo(() => {
    const maximum = data.reduce((current, point) => Math.max(current, point.value), 0);
    const x = (index: number) => (
      data.length <= 1
        ? WIDTH / 2
        : PAD_X + (index / (data.length - 1)) * (WIDTH - PAD_X * 2)
    );
    const y = (value: number) => (
      maximum <= 0
        ? BASELINE
        : BASELINE - (value / maximum) * (BASELINE - TOP)
    );
    const peakIndex = data.reduce(
      (best, point, index) => point.value > (data[best]?.value ?? -1) ? index : best,
      0,
    );
    return { maximum, x, y, peakIndex };
  }, [data]);

  if (!data.length) return null;

  const moveFocus = (next: number) => {
    setActiveIndex(Math.max(0, Math.min(data.length - 1, next)));
  };
  const onKeyDown = (event: KeyboardEvent<SVGSVGElement>) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowDown") {
      event.preventDefault();
      moveFocus(activeIndex - 1);
    } else if (event.key === "ArrowRight" || event.key === "ArrowUp") {
      event.preventDefault();
      moveFocus(activeIndex + 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      moveFocus(0);
    } else if (event.key === "End") {
      event.preventDefault();
      moveFocus(data.length - 1);
    }
  };
  const selectNearestPoint = (event: PointerEvent<SVGSVGElement>) => {
    const bounds = svgRef.current?.getBoundingClientRect();
    if (!bounds || bounds.width <= 0) return;
    const ratio = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
    moveFocus(Math.round(ratio * (data.length - 1)));
  };

  const path = data
    .map((point, index) => `${index === 0 ? "M" : "L"} ${geometry.x(index).toFixed(2)} ${geometry.y(point.value).toFixed(2)}`)
    .join(" ");
  const active = data[activeIndex] || data[data.length - 1];
  const axisIndices = [...new Set([0, Math.floor((data.length - 1) / 2), data.length - 1])];

  return (
    <div
      ref={reveal.ref}
      data-chart-kind="hairline-series"
      className={animate ? "chart-series-enter" : ""}
    >
      <svg
        ref={svgRef}
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        className="h-auto w-full touch-pan-y overflow-visible rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
        role="slider"
        tabIndex={0}
        aria-valuemin={0}
        aria-valuemax={data.length - 1}
        aria-valuenow={activeIndex}
        aria-valuetext={active.description || `${active.label}: ${fmt.number(active.value)}`}
        aria-label={active.description || `${active.label}: ${fmt.number(active.value)}`}
        onKeyDown={onKeyDown}
        onPointerMove={(event) => {
          if (event.pointerType === "mouse") selectNearestPoint(event);
        }}
        onPointerDown={selectNearestPoint}
      >
        <line x1={PAD_X} y1={BASELINE} x2={WIDTH - PAD_X} y2={BASELINE} stroke={theme.border} strokeWidth="1" />
        {data.map((point, index) => {
          const x = geometry.x(index);
          const y = geometry.y(point.value);
          const peak = index === geometry.peakIndex;
          return (
            <g key={point.id} aria-hidden>
              <line
                x1={x}
                y1={BASELINE}
                x2={x}
                y2={y}
                stroke={peak ? theme.accent : theme.muted}
                strokeWidth={peak ? 1.8 : 0.8}
                opacity={peak ? 1 : 0.64}
                className="chart-series-hair"
                style={{ "--chart-delay": `${Math.min(index * 12, 420)}ms` } as CSSProperties}
              />
              <circle
                cx={x}
                cy={y}
                r={peak ? 4.5 : 2.4}
                fill={peak ? theme.accent : theme.text}
                className="chart-series-point"
                style={{ "--chart-delay": `${Math.min(100 + index * 12, 520)}ms` } as CSSProperties}
              />
              {index === activeIndex ? (
                <circle cx={x} cy={y} r="8" fill="none" stroke={theme.accent} strokeWidth="1.5" />
              ) : null}
            </g>
          );
        })}
        <path
          d={path}
          pathLength="1"
          fill="none"
          stroke={theme.text}
          strokeWidth="1.5"
          strokeLinejoin="round"
          className="chart-series-path"
          aria-hidden
        />
        {axisIndices.map((index) => (
          <text
            key={`${data[index].id}:axis`}
            x={geometry.x(index)}
            y={BASELINE + 22}
            textAnchor={index === 0 ? "start" : index === data.length - 1 ? "end" : "middle"}
            fill={theme.muted}
            fontSize="11"
            fontWeight="600"
            aria-hidden
          >
            {data[index].label}
          </text>
        ))}
      </svg>
      <div className="mt-2 flex min-h-11 items-center justify-between gap-3 rounded-md bg-subtle px-3 py-2 text-sm" aria-live="polite">
        <span className="font-medium text-fg">{active.label}</span>
        <span className="font-mono font-semibold tabular-nums text-accent">{fmt.number(active.value)}</span>
      </div>
    </div>
  );
}
