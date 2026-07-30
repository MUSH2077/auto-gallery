"use client";

import Link from "next/link";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
} from "react";

import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { motionConfig, useEnterOnce } from "@/lib/motion";

import { radiusForValue } from "./chartMath";
import { useChartTheme } from "./useChartTheme";

export interface ActivityDay {
  date: string;
  total: number;
  [source: string]: number | string | string[];
}

export interface ActivityTimeline {
  creator_id: string;
  sources: string[];
  days: ActivityDay[];
  total: number;
}

interface CalendarDay {
  key: string;
  date: Date;
  entry?: ActivityDay;
  week: number;
  weekday: number;
}

function utcDateKey(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function buildCalendar(year: number, entries: Map<string, ActivityDay>): {
  days: CalendarDay[];
  weeks: number;
  months: { key: string; labelDate: Date; week: number }[];
} {
  const first = new Date(Date.UTC(year, 0, 1));
  const last = new Date(Date.UTC(year, 11, 31));
  const mondayOffset = (first.getUTCDay() + 6) % 7;
  const gridStart = new Date(first);
  gridStart.setUTCDate(gridStart.getUTCDate() - mondayOffset);
  const days: CalendarDay[] = [];
  const months: { key: string; labelDate: Date; week: number }[] = [];
  let cursor = new Date(first);
  while (cursor <= last) {
    const sinceStart = Math.round((cursor.getTime() - gridStart.getTime()) / 86_400_000);
    const week = Math.floor(sinceStart / 7);
    const weekday = (cursor.getUTCDay() + 6) % 7;
    const key = utcDateKey(cursor);
    if (cursor.getUTCDate() === 1) {
      months.push({ key: `${year}-${cursor.getUTCMonth()}`, labelDate: new Date(cursor), week });
    }
    days.push({ key, date: new Date(cursor), entry: entries.get(key), week, weekday });
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return {
    days,
    weeks: Math.max(...days.map((day) => day.week)) + 1,
    months,
  };
}

function sourceSegments(
  entry: ActivityDay | undefined,
  sources: string[],
  colorFor: (source: string) => string,
): { background: string; center?: string; segmented: boolean } {
  if (!entry?.total) return { background: "rgb(var(--ag-border))", segmented: false };
  const values = sources
    .map((source) => ({ source, count: Number(entry[source] || 0) }))
    .filter((item) => item.count > 0)
    .sort((left, right) => right.count - left.count);
  if (!values.length) return { background: "rgb(var(--ag-accent))", segmented: false };
  if (values.length === 1) {
    return { background: colorFor(values[0].source), segmented: false };
  }
  let cursor = 0;
  const stops: string[] = [];
  for (const item of values) {
    const start = cursor;
    cursor += (item.count / entry.total) * 100;
    stops.push(`${colorFor(item.source)} ${start.toFixed(2)}% ${cursor.toFixed(2)}%`);
  }
  return {
    background: `conic-gradient(${stops.join(", ")})`,
    center: colorFor(values[0].source),
    segmented: true,
  };
}

export default function ActivityDotMatrix({
  data,
  year,
  availableYears,
  onYearChange,
}: {
  data?: ActivityTimeline;
  year: number;
  availableYears: number[];
  onYearChange: (year: number) => void;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const theme = useChartTheme();
  const entries = useMemo(
    () => new Map((data?.days || []).map((day) => [day.date, day])),
    [data?.days],
  );
  const calendar = useMemo(() => buildCalendar(year, entries), [entries, year]);
  const maximum = useMemo(
    () => calendar.days.reduce((current, day) => Math.max(current, day.entry?.total || 0), 0),
    [calendar.days],
  );
  const [activeIndex, setActiveIndex] = useState(0);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [motionEnabled, setMotionEnabled] = useState(false);
  const dayRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const entryKeys = useMemo(
    () => calendar.days.filter((day) => day.entry?.total).map((day) => day.key),
    [calendar.days],
  );
  const isNew = useEnterOnce(entryKeys);

  useEffect(() => {
    setMotionEnabled(motionConfig.shouldAnimate());
  }, []);

  useEffect(() => {
    setActiveIndex(0);
    setSelectedIndex(null);
  }, [year]);

  const weekdayLabels = useMemo(
    () => Array.from({ length: 7 }, (_, index) => {
      const date = new Date(Date.UTC(2026, 7, 3 + index));
      return new Intl.DateTimeFormat(fmt.locale, { weekday: "narrow", timeZone: "UTC" }).format(date);
    }),
    [fmt.locale],
  );
  const yearIndex = availableYears.indexOf(year);
  const previousYear = yearIndex > 0 ? availableYears[yearIndex - 1] : null;
  const nextYear = yearIndex >= 0 && yearIndex < availableYears.length - 1
    ? availableYears[yearIndex + 1]
    : null;

  const moveFocus = (index: number) => {
    const bounded = Math.max(0, Math.min(calendar.days.length - 1, index));
    setActiveIndex(bounded);
    dayRefs.current[bounded]?.focus();
  };
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    const movements: Record<string, number> = {
      ArrowLeft: -7,
      ArrowRight: 7,
      ArrowUp: -1,
      ArrowDown: 1,
    };
    if (event.key in movements) {
      event.preventDefault();
      moveFocus(index + movements[event.key]);
    } else if (event.key === "Home") {
      event.preventDefault();
      moveFocus(0);
    } else if (event.key === "End") {
      event.preventDefault();
      moveFocus(calendar.days.length - 1);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setSelectedIndex(index);
    } else if (event.key === "Escape") {
      event.preventDefault();
      setSelectedIndex(null);
    }
  };

  const selected = selectedIndex === null ? null : calendar.days[selectedIndex];
  const visualWidth = `calc(${calendar.weeks} * var(--activity-cell-size) + ${Math.max(0, calendar.weeks - 1)} * var(--activity-cell-gap))`;

  return (
    <div data-chart-kind="activity-dot-matrix">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="inline-flex items-center gap-1 rounded-md border border-border bg-subtle p-1">
          <button
            type="button"
            className="btn-icon"
            onClick={() => previousYear !== null && onYearChange(previousYear)}
            disabled={previousYear === null}
            aria-label={t("charts.previous_year")}
          >
            ←
          </button>
          <label className="sr-only" htmlFor="activity-year">{t("charts.year")}</label>
          <select
            id="activity-year"
            value={year}
            onChange={(event) => onYearChange(Number(event.target.value))}
            className="min-h-11 rounded-md border-0 bg-transparent px-2 text-sm font-semibold text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          >
            {availableYears.map((option) => <option key={option} value={option}>{option}</option>)}
          </select>
          <button
            type="button"
            className="btn-icon"
            onClick={() => nextYear !== null && onYearChange(nextYear)}
            disabled={nextYear === null}
            aria-label={t("charts.next_year")}
          >
            →
          </button>
        </div>
        <span className="text-xs font-medium text-muted">{t("workgrid.work_count", { count: data?.total || 0 })}</span>
      </div>

      <div className="activity-matrix flex min-w-0 gap-2">
        <div className="mt-6 grid shrink-0 grid-rows-7 gap-[var(--activity-cell-gap)]" aria-hidden>
          {weekdayLabels.map((label, index) => (
            <span key={`${label}:${index}`} className="flex h-[var(--activity-cell-size)] items-center text-[10px] font-medium text-muted">
              {index % 2 === 0 ? label : ""}
            </span>
          ))}
        </div>
        <div className="min-w-0 flex-1 overflow-x-auto pb-2">
          <div style={{ width: visualWidth }}>
            <div
              className="relative mb-1 h-5"
              aria-hidden
            >
              {calendar.months.map((month) => (
                <span
                  key={month.key}
                  className="absolute top-0 text-[10px] font-semibold uppercase tracking-wide text-muted"
                  style={{ left: `calc(${month.week} * (var(--activity-cell-size) + var(--activity-cell-gap)))` }}
                >
                  {new Intl.DateTimeFormat(fmt.locale, { month: "short", timeZone: "UTC" }).format(month.labelDate)}
                </span>
              ))}
            </div>
            <div
              role="group"
              aria-label={t("creator_detail.works_timeline")}
              className="grid grid-rows-7 gap-[var(--activity-cell-gap)]"
              style={{
                gridTemplateColumns: `repeat(${calendar.weeks}, var(--activity-cell-size))`,
                gridAutoFlow: "column",
              }}
            >
              {calendar.days.map((day, index) => {
                const segments = sourceSegments(
                  day.entry,
                  data?.sources || [],
                  (source) => theme.colorFor(`source:${source}`),
                );
                const radius = radiusForValue(day.entry?.total || 0, maximum, 0.18, 1);
                const entering = Boolean(day.entry?.total) && motionEnabled && isNew(day.key);
                const sourceSummary = (data?.sources || [])
                  .map((source) => {
                    const count = Number(day.entry?.[source] || 0);
                    return count ? `${source} ${fmt.number(count)}` : "";
                  })
                  .filter(Boolean)
                  .join(", ");
                const label = day.entry?.total
                  ? t("charts.activity_day_label", {
                    date: fmt.date(`${day.key}T00:00:00Z`),
                    count: day.entry.total,
                    sources: sourceSummary,
                  })
                  : t("charts.activity_day_empty", { date: fmt.date(`${day.key}T00:00:00Z`) });
                return (
                  <button
                    key={day.key}
                    ref={(node) => { dayRefs.current[index] = node; }}
                    type="button"
                    tabIndex={index === activeIndex ? 0 : -1}
                    aria-label={label}
                    aria-pressed={selectedIndex === index}
                    className="activity-matrix-cell flex items-center justify-center rounded-sm focus-visible:z-10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
                    style={{ gridColumn: day.week + 1, gridRow: day.weekday + 1 }}
                    onFocus={() => {
                      setActiveIndex(index);
                      setSelectedIndex(index);
                    }}
                    onClick={() => setSelectedIndex(index)}
                    onKeyDown={(event) => onKeyDown(event, index)}
                  >
                    <span
                      aria-hidden
                      className={`activity-matrix-dot relative flex items-center justify-center rounded-full ${entering ? "chart-dot-enter" : ""}`}
                      style={{
                        "--dot-scale": radius,
                        "--chart-delay": `${Math.min(index * 2, 420)}ms`,
                        background: segments.background,
                        boxShadow: selectedIndex === index ? `0 0 0 2px ${theme.surface}, 0 0 0 4px ${theme.accent}` : undefined,
                      } as CSSProperties}
                    >
                      {segments.segmented ? (
                        <span
                          className="block h-[58%] w-[58%] rounded-full ring-1 ring-surface"
                          style={{ backgroundColor: segments.center }}
                        />
                      ) : null}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-muted">
        {(data?.sources || []).map((source) => (
          <span key={source} className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: theme.colorFor(`source:${source}`) }} aria-hidden />
            {source}
          </span>
        ))}
      </div>

      {selected ? (
        <div className="mt-3 min-h-11 rounded-md border border-border bg-subtle px-3 py-2 text-sm" aria-live="polite">
          <div className="flex items-center justify-between gap-3">
            <span className="font-semibold text-fg">{fmt.date(`${selected.key}T00:00:00Z`)}</span>
            <button
              type="button"
              className="btn-icon"
              onClick={() => {
                setSelectedIndex(null);
                dayRefs.current[activeIndex]?.focus();
              }}
              aria-label={t("common.close")}
            >
              ×
            </button>
          </div>
          {selected.entry?.total ? (
            <div className="mt-2 space-y-2">
              {(data?.sources || []).map((source) => {
                const count = Number(selected.entry?.[source] || 0);
                const ids = (selected.entry?.[`${source}_ids`] as string[] | undefined) || [];
                if (!count) return null;
                return (
                  <div key={source}>
                    <div className="flex items-center gap-1.5 text-xs font-medium text-fg">
                      <span className="h-2 w-2 rounded-full" style={{ backgroundColor: theme.colorFor(`source:${source}`) }} aria-hidden />
                      {t("workgrid.day_source_count", { source, count })}
                    </div>
                    {ids.length ? (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {ids.slice(0, 20).map((workId) => (
                          <Link
                            key={workId}
                            href={`/admin/works/${workId}`}
                            className="inline-flex min-h-11 items-center rounded-md border border-border bg-surface px-2 font-mono text-xs text-accent hover:border-accent"
                          >
                            {workId.length > 12 ? `${workId.slice(0, 8)}…${workId.slice(-4)}` : workId}
                          </Link>
                        ))}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          ) : <p className="mt-1 text-xs text-muted">{t("charts.no_activity_day")}</p>}
        </div>
      ) : null}
    </div>
  );
}
