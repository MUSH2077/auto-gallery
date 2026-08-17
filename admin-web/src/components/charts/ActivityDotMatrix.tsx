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
import { motionConfig, useViewportReveal } from "@/lib/motion";

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
  month: number;
}

interface SourcePoint {
  source: string;
  count: number;
  ids: string[];
}

function utcDateKey(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function buildCalendar(year: number, entries: Map<string, ActivityDay>): {
  days: CalendarDay[];
  weeks: number;
  months: { key: string; labelDate: Date; week: number; weekSpan: number; index: number }[];
} {
  const first = new Date(Date.UTC(year, 0, 1));
  const last = new Date(Date.UTC(year, 11, 31));
  const mondayOffset = (first.getUTCDay() + 6) % 7;
  const gridStart = new Date(first);
  gridStart.setUTCDate(gridStart.getUTCDate() - mondayOffset);
  const days: CalendarDay[] = [];
  const months: { key: string; labelDate: Date; week: number; weekSpan: number; index: number }[] = [];
  const cursor = new Date(first);
  while (cursor <= last) {
    const sinceStart = Math.round((cursor.getTime() - gridStart.getTime()) / 86_400_000);
    const week = Math.floor(sinceStart / 7);
    const weekday = (cursor.getUTCDay() + 6) % 7;
    const key = utcDateKey(cursor);
    if (cursor.getUTCDate() === 1) {
      months.push({
        key: `${year}-${cursor.getUTCMonth()}`,
        labelDate: new Date(cursor),
        week,
        weekSpan: 1,
        index: cursor.getUTCMonth(),
      });
    }
    days.push({
      key,
      date: new Date(cursor),
      entry: entries.get(key),
      week,
      weekday,
      month: cursor.getUTCMonth(),
    });
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  const weeks = Math.max(...days.map((day) => day.week)) + 1;
  return {
    days,
    weeks,
    months: months.map((month, index) => ({
      ...month,
      weekSpan: Math.max(1, (months[index + 1]?.week ?? weeks) - month.week),
    })),
  };
}

function pointsForDay(day: ActivityDay | undefined, sources: string[]): SourcePoint[] {
  if (!day) return [];
  return sources
    .map((source) => ({
      source,
      count: Number(day[source] || 0),
      ids: (day[`${source}_ids`] as string[] | undefined) || [],
    }))
    .filter((point) => point.count > 0)
    .sort((left, right) => left.source.localeCompare(right.source));
}

function pointPosition(index: number, total: number): { x: number; y: number } {
  if (total === 1) return { x: 8, y: 8 };
  if (total === 2) return { x: index === 0 ? 5.3 : 10.7, y: 8 };
  if (total === 3) {
    return [
      { x: 8, y: 4.8 },
      { x: 5.2, y: 10.1 },
      { x: 10.8, y: 10.1 },
    ][index];
  }
  const angle = -Math.PI / 2 + (index / total) * Math.PI * 2;
  return { x: 8 + Math.cos(angle) * 3.7, y: 8 + Math.sin(angle) * 3.7 };
}

function SourceCircleCluster({
  points,
  maximum,
  colorFor,
  entering,
  delay,
}: {
  points: SourcePoint[];
  maximum: number;
  colorFor: (source: string) => string;
  entering: boolean;
  delay: number;
}) {
  if (!points.length) {
    return <span className="block h-[28%] w-[28%] rounded-full bg-border" aria-hidden />;
  }
  return (
    <svg viewBox="0 0 16 16" className="h-full w-full overflow-visible" aria-hidden>
      {points.map((point, index) => {
        const position = pointPosition(index, points.length);
        const radius = radiusForValue(
          point.count,
          maximum,
          points.length > 4 ? 1.1 : 1.45,
          points.length > 4 ? 2.1 : 3.25,
        );
        return (
          <circle
            key={point.source}
            data-activity-source={point.source}
            data-activity-count={point.count}
            cx={position.x}
            cy={position.y}
            r={radius}
            fill={colorFor(point.source)}
            stroke="rgb(var(--ag-surface))"
            strokeWidth="0.75"
            className={entering ? "activity-source-enter" : undefined}
            style={{
              "--chart-delay": `${Math.min(delay + index * 44, 760)}ms`,
            } as CSSProperties}
          />
        );
      })}
    </svg>
  );
}

function SourceDetails({
  day,
  sources,
  colorFor,
}: {
  day: CalendarDay;
  sources: string[];
  colorFor: (source: string) => string;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const points = pointsForDay(day.entry, sources);
  return (
    <div className="rounded-md border border-border bg-subtle px-3 py-2 text-sm">
      <div className="font-semibold text-fg">{fmt.date(`${day.key}T00:00:00Z`)}</div>
      {points.length ? (
        <div className="mt-2 space-y-2">
          {points.map((point) => (
            <div key={point.source} className="min-w-0">
              <div className="flex min-h-6 items-center gap-2 text-xs font-medium text-fg">
                <span
                  className="h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: colorFor(point.source) }}
                  aria-hidden
                />
                {t("workgrid.day_source_count", { source: point.source, count: point.count })}
              </div>
              {point.ids.length ? (
                <div className="mt-1 flex flex-wrap gap-1">
                  {point.ids.slice(0, 20).map((workId) => (
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
          ))}
        </div>
      ) : <p className="mt-1 text-xs text-muted">{t("charts.no_activity_day")}</p>}
    </div>
  );
}

export default function ActivityDotMatrix({
  data,
  year,
  availableYears,
  onYearChange,
}: {
  data: ActivityTimeline;
  year: number;
  availableYears: number[];
  onYearChange: (year: number) => void;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const theme = useChartTheme();
  const entries = useMemo(
    () => new Map(data.days.map((day) => [day.date, day])),
    [data.days],
  );
  const calendar = useMemo(() => buildCalendar(year, entries), [entries, year]);
  const maximumSourceCount = useMemo(
    () => calendar.days.reduce((maximum, day) => (
      Math.max(
        maximum,
        ...pointsForDay(day.entry, data.sources).map((point) => point.count),
      )
    ), 0),
    [calendar.days, data.sources],
  );
  const firstActiveIndex = useMemo(
    () => Math.max(0, calendar.days.findIndex((day) => Boolean(day.entry?.total))),
    [calendar.days],
  );
  const [activeIndex, setActiveIndex] = useState(firstActiveIndex);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [isYearListboxOpen, setIsYearListboxOpen] = useState(false);
  const [activeYearIndex, setActiveYearIndex] = useState(() => Math.max(0, availableYears.indexOf(year)));
  const yearPickerContainerRef = useRef<HTMLDivElement>(null);
  const yearPickerRef = useRef<HTMLButtonElement>(null);
  const yearListboxRef = useRef<HTMLDivElement>(null);
  const shouldReturnYearFocusRef = useRef(false);
  const reveal = useViewportReveal<HTMLDivElement>(year);
  const animate = reveal.revealed && motionConfig.shouldAnimate();

  useEffect(() => {
    setActiveIndex(firstActiveIndex);
    setSelectedIndex(null);
  }, [calendar.days, firstActiveIndex, year]);

  useEffect(() => {
    setActiveYearIndex(Math.max(0, availableYears.indexOf(year)));
  }, [availableYears, year]);

  useEffect(() => {
    if (!isYearListboxOpen) return;
    const frame = window.requestAnimationFrame(() => yearListboxRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [isYearListboxOpen]);

  useEffect(() => {
    if (!isYearListboxOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (yearPickerContainerRef.current?.contains(event.target as Node)) return;
      closeYearListbox(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [isYearListboxOpen]);

  useEffect(() => {
    if (isYearListboxOpen || !shouldReturnYearFocusRef.current) return;
    shouldReturnYearFocusRef.current = false;
    yearPickerRef.current?.focus();
  }, [isYearListboxOpen]);

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
  const activeDay = calendar.days[activeIndex];
  const selectedDay = selectedIndex === null ? null : calendar.days[selectedIndex];

  const closeYearListbox = (returnFocus = true) => {
    shouldReturnYearFocusRef.current = returnFocus;
    setIsYearListboxOpen(false);
  };

  const selectYear = (nextYear: number) => {
    onYearChange(nextYear);
    closeYearListbox();
  };

  const onYearListboxKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!availableYears.length) return;
    let nextIndex = activeYearIndex;
    if (event.key === "ArrowUp") nextIndex -= 1;
    else if (event.key === "ArrowDown") nextIndex += 1;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = availableYears.length - 1;
    else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectYear(availableYears[activeYearIndex]);
      return;
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeYearListbox();
      return;
    } else {
      return;
    }
    event.preventDefault();
    setActiveYearIndex(Math.max(0, Math.min(availableYears.length - 1, nextIndex)));
  };

  const labelForDay = (day: CalendarDay) => {
    const sourceSummary = pointsForDay(day.entry, data.sources)
      .map((point) => `${point.source} ${fmt.number(point.count)}`)
      .join(", ");
    return day.entry?.total
      ? t("charts.activity_day_label", {
        date: fmt.date(`${day.key}T00:00:00Z`),
        count: day.entry.total,
        sources: sourceSummary,
      })
      : t("charts.activity_day_empty", { date: fmt.date(`${day.key}T00:00:00Z`) });
  };

  const onGridKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const movements: Record<string, number> = {
      ArrowLeft: -7,
      ArrowRight: 7,
      ArrowUp: -1,
      ArrowDown: 1,
    };
    let next = activeIndex;
    if (event.key in movements) next += movements[event.key];
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = calendar.days.length - 1;
    else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setSelectedIndex(activeIndex);
      return;
    } else if (event.key === "Escape") {
      event.preventDefault();
      setSelectedIndex(null);
      return;
    } else {
      return;
    }
    event.preventDefault();
    setActiveIndex(Math.max(0, Math.min(calendar.days.length - 1, next)));
  };

  return (
    <div ref={reveal.ref} data-chart-kind="activity-dot-matrix">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div ref={yearPickerContainerRef} className="relative inline-flex items-center gap-1 rounded-md border border-border bg-subtle p-1">
          <button
            type="button"
            className="btn-icon"
            onClick={() => previousYear !== null && onYearChange(previousYear)}
            disabled={previousYear === null}
            aria-label={t("charts.previous_year")}
          >
            ←
          </button>
          <button
            ref={yearPickerRef}
            type="button"
            aria-label={t("charts.year")}
            aria-haspopup="listbox"
            aria-expanded={isYearListboxOpen}
            aria-controls="activity-year-listbox"
            className="inline-flex min-h-11 items-center gap-1 rounded-md px-2 text-sm font-semibold text-fg hover:bg-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
            onClick={() => setIsYearListboxOpen((open) => !open)}
          >
            {year}
            <span aria-hidden>▾</span>
          </button>
          {isYearListboxOpen ? (
            <div
              ref={yearListboxRef}
              id="activity-year-listbox"
              role="listbox"
              tabIndex={-1}
              aria-label={t("charts.year")}
              aria-activedescendant={`activity-year-option-${availableYears[activeYearIndex]}`}
              className="absolute left-0 top-full z-20 mt-1 min-w-full overflow-hidden rounded-md border border-border bg-surface p-1 shadow-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
              onKeyDown={onYearListboxKeyDown}
            >
              {availableYears.map((option, index) => (
                <button
                  key={option}
                  id={`activity-year-option-${option}`}
                  type="button"
                  role="option"
                  aria-selected={option === year}
                  className={`flex min-h-11 w-full items-center rounded px-2 text-left text-sm font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus ${
                    index === activeYearIndex ? "bg-accent-subtle text-accent" : "text-fg hover:bg-subtle"
                  }`}
                  onMouseMove={() => setActiveYearIndex(index)}
                  onClick={() => selectYear(option)}
                >
                  {option}
                </button>
              ))}
            </div>
          ) : null}
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
        <span className="text-xs font-medium text-muted">
          {t("workgrid.work_count", { count: data.total })}
        </span>
      </div>

      <div data-calendar-scroll className="max-w-full overflow-x-auto pb-2">
        <div
          role="grid"
          tabIndex={0}
          data-calendar-grid="shared"
          aria-label={t("creator_detail.works_timeline")}
          aria-activedescendant={activeDay ? `activity-day-${activeDay.key}` : undefined}
          aria-describedby="activity-grid-instructions"
          className="grid w-max gap-1 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus"
          style={{
            gridTemplateColumns: `1rem repeat(${calendar.weeks}, 14px)`,
            gridTemplateRows: "1.25rem repeat(7, 14px)",
          }}
          onKeyDown={onGridKeyDown}
        >
          <span aria-hidden style={{ gridColumn: 1, gridRow: 1 }} />
          {calendar.months.map((month) => (
            <span
              key={month.key}
              data-calendar-month={month.index}
              className="truncate text-[10px] font-semibold uppercase tracking-wide text-muted"
              style={{ gridColumn: `${month.week + 2} / span ${month.weekSpan}`, gridRow: 1 }}
            >
              {new Intl.DateTimeFormat(fmt.locale, { month: "short", timeZone: "UTC" }).format(month.labelDate)}
            </span>
          ))}
          {weekdayLabels.map((label, weekday) => (
            <span
              key={`${label}:${weekday}`}
              data-calendar-weekday={weekday}
              className="flex items-center text-[10px] font-medium text-muted"
              style={{ gridColumn: 1, gridRow: weekday + 2 }}
              aria-hidden
            >
              {label}
            </span>
          ))}
          {calendar.days.map((day, index) => {
            const points = pointsForDay(day.entry, data.sources);
            return (
              <div
                key={day.key}
                id={`activity-day-${day.key}`}
                role="gridcell"
                aria-label={labelForDay(day)}
                aria-selected={selectedIndex === index}
                className={`activity-calendar-cell relative h-3.5 w-3.5 cursor-pointer rounded-sm ${
                  activeIndex === index ? "bg-accent-subtle ring-1 ring-accent" : "hover:bg-subtle"
                }`}
                style={{ gridColumn: day.week + 2, gridRow: day.weekday + 2 }}
                onClick={() => {
                  setActiveIndex(index);
                  setSelectedIndex(index);
                }}
              >
                <SourceCircleCluster
                  points={points}
                  maximum={maximumSourceCount}
                  colorFor={(source) => theme.colorFor(`source:${source}`)}
                  entering={animate}
                  delay={day.week * 11 + day.weekday * 3}
                />
              </div>
            );
          })}
        </div>
      </div>
      <p id="activity-grid-instructions" className="sr-only">
        {t("charts.activity_keyboard_hint")}
      </p>
      {selectedDay ? (
        <div className="mt-3" aria-live="polite">
          <SourceDetails
            day={selectedDay}
            sources={data.sources}
            colorFor={(source) => theme.colorFor(`source:${source}`)}
          />
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-muted">
        {data.sources.map((source) => (
          <span key={source} className="inline-flex min-h-6 items-center gap-1.5">
            <span
              className="h-2.5 w-2.5 rounded-full"
              style={{ backgroundColor: theme.colorFor(`source:${source}`) }}
              aria-hidden
            />
            {source}
          </span>
        ))}
      </div>
    </div>
  );
}
