"use client";
import { useMemo, useState } from "react";
import Link from "next/link";

interface DayData {
  date: string;
  total: number;
  [source: string]: number | string | string[];
}

interface TimelineData {
  creator_id: string;
  sources: string[];
  days: DayData[];
  total: number;
}

import { SOURCE_COLORS, CHART_COLORS } from "@/lib/sourceColors";

function sourceColor(source: string): string { return SOURCE_COLORS[source] || "#9CA3AF"; }

function shadeColor(source: string, count: number): string {
  if (count === 0) return "rgba(128,128,128,0.08)";
  const hex = sourceColor(source);
  const r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
  const alpha = [0, 0.2, 0.4, 0.65, 1.0][Math.min(count, 4)];
  return `rgba(${r},${g},${b},${alpha})`;
}

export default function WorkGrid({ data, loading }: { data?: TimelineData; loading?: boolean }) {
  const [selectedDay, setSelectedDay] = useState<DayData | null>(null);
  const [yearOffset, setYearOffset] = useState(0);

  const { weeks, months, dayMap } = useMemo(() => {
    if (!data?.days?.length) return { weeks: [], months: [], dayMap: new Map<string, DayData>() };
    const dayMap = new Map<string, DayData>();
    for (const d of data.days) dayMap.set(d.date, d);

    const allDates = data.days.map((d) => new Date(d.date + "T00:00:00Z"));
    let from = new Date(Math.min(...allDates.map((d) => d.getTime())));
    let to = new Date(Math.max(...allDates.map((d) => d.getTime())));
    const displayYear = to.getUTCFullYear() + yearOffset;
    from = new Date(Date.UTC(displayYear, 0, 1));
    to = new Date(Date.UTC(displayYear, 11, 31));

    const fromDay = from.getUTCDay();
    const startDate = new Date(from);
    startDate.setUTCDate(startDate.getUTCDate() - (fromDay === 0 ? 6 : fromDay - 1));

    const weeks: Date[][] = [];
    let current = new Date(startDate);
    while (current <= to || weeks.length < 53) {
      const week: Date[] = [];
      for (let d = 0; d < 7; d++) { week.push(new Date(current)); current.setUTCDate(current.getUTCDate() + 1); }
      weeks.push(week);
      if (weeks.length > 54) break;
    }

    const monthLabels: { label: string; col: number }[] = [];
    for (let w = 0; w < weeks.length; w++) {
      const midDay = weeks[w][3];
      if (midDay.getUTCDate() <= 7) monthLabels.push({ label: midDay.toLocaleString("en", { month: "short", timeZone: "UTC" }), col: w });
    }
    return { weeks, months: monthLabels, dayMap };
  }, [data, yearOffset]);

  if (loading) return <div className="animate-pulse"><div className="h-32 rounded-md bg-[#eaeef2] dark:bg-[#21262d]" /></div>;
  if (!data?.days?.length) return null;

  const cellSize = 11, gap = 2, step = cellSize + gap;

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <button onClick={() => setYearOffset(yearOffset - 1)} className="btn-ghost px-2 py-0.5 text-xs">←</button>
          <span className="text-sm font-medium dark:text-white">{new Date(Date.UTC(new Date().getUTCFullYear() + yearOffset, 0, 1)).getUTCFullYear()}</span>
          <button onClick={() => setYearOffset(yearOffset + 1)} className="btn-ghost px-2 py-0.5 text-xs">→</button>
        </div>
        <span className="text-xs text-[#57606a] dark:text-[#8b949e]">{data.total} works</span>
      </div>

      <div className="overflow-x-auto">
        <svg width={weeks.length * step + 40} height={7 * step + 20} className="text-xs">
          {months.map((m, i) => (
            <text key={i} x={m.col * step + 10} y={10} className="fill-[#57606a] dark:fill-[#8b949e]" fontSize="9">{m.label}</text>
          ))}
          {weeks.map((week, wi) =>
            week.map((day, di) => {
              const key = day.toISOString().slice(0, 10);
              const entry = dayMap.get(key);
              const has = entry && entry.total > 0;
              const x = wi * step + 10, y = di * step + 18;
              const fill = has ? shadeColor(data.sources.find((s) => (entry[s] as number) > 0) || "", entry.total) : "rgba(128,128,128,0.08)";

              const title = entry
                ? `${key}: ${data.sources.map((s) => { const c = entry[s] as number; return c ? `${s}×${c}` : ""; }).filter(Boolean).join(", ")}`
                : key;

              return <rect key={key} x={x} y={y} width={cellSize} height={cellSize} rx={2} fill={fill}
                className="cursor-pointer hover:stroke-[1.5] hover:stroke-blue-400" onClick={() => entry && setSelectedDay(entry)}>
                <title>{title}</title>
              </rect>;
            })
          )}
        </svg>
      </div>

      <div className="flex items-center gap-3 mt-3 text-xs flex-wrap">
        <span className="text-[#57606a] dark:text-[#8b949e]">Less</span>
        {[0, 1, 2, 3, 4].map((lvl) => (
          <div key={lvl} className="w-3 h-3 rounded-sm" style={{ backgroundColor: lvl === 0 ? "rgba(128,128,128,0.08)" : `rgba(100,100,100,${[0,0.2,0.4,0.65,1][lvl]})` }} />
        ))}
        <span className="text-[#57606a] dark:text-[#8b949e]">More</span>
        <span className="mx-2 text-[#d8dee4] dark:text-[#30363d]">|</span>
        {data.sources.map((s) => (
          <span key={s} className="flex items-center gap-1">
            <span className="w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: sourceColor(s) }} />
            <span className="text-[#57606a] dark:text-[#8b949e]">{s}</span>
          </span>
        ))}
      </div>

      {selectedDay && (
        <div className="mt-3 rounded-md border border-[#d8dee4] bg-[#f6f8fa] p-3 text-sm dark:border-[#30363d] dark:bg-[#0d1117]">
          <div className="flex items-center justify-between">
            <span className="font-medium dark:text-white">{selectedDay.date}</span>
            <button onClick={() => setSelectedDay(null)} className="text-[#57606a] hover:text-[#24292f] dark:text-[#8b949e] dark:hover:text-[#e6edf3]">×</button>
          </div>
          {data.sources.map((s) => {
            const cnt = (selectedDay[s] as number) || 0;
            if (!cnt) return null;
            const ids = (selectedDay[`${s}_ids`] as string[]) || [];
            return (
              <div key={s} className="mt-2">
                <span className="flex items-center gap-1 text-xs font-medium mb-1">
                  <span className="w-2 h-2 rounded-sm" style={{ backgroundColor: sourceColor(s) }} />
                  {s}: {cnt} work{cnt > 1 ? "s" : ""}
                </span>
                {ids.length > 0 && (
                  <div className="flex flex-wrap gap-1 ml-4">
                    {ids.slice(0, 20).map((wid) => (
                      <Link key={wid} href={`/admin/works/${wid}`}
                        className="rounded-md border border-[#d8dee4] bg-white px-2 py-0.5 font-mono text-xs transition-colors hover:border-[#0969da] hover:text-[#0969da] dark:border-[#30363d] dark:bg-[#161b22] dark:hover:border-[#58a6ff] dark:hover:text-[#58a6ff]">
                        {wid.length > 12 ? wid.slice(0, 8) + "..." + wid.slice(-4) : wid}
                      </Link>
                    ))}
                    {ids.length > 20 && <span className="text-xs text-[#57606a] dark:text-[#8b949e]">+{ids.length - 20} more</span>}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
