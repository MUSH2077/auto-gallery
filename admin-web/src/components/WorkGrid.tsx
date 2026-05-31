"use client";
import { useMemo, useState } from "react";

interface DayData {
  date: string;
  total: number;
  [source: string]: number | string;
}

interface TimelineData {
  creator_id: string;
  sources: string[];
  days: DayData[];
  total: number;
}

const SOURCE_COLORS: Record<string, string> = {
  pixiv: "#0066FF", x: "#1DA1F2", twitter: "#1DA1F2", iwara: "#22C55E",
  danbooru: "#A855F7", pinterest: "#E60023", lofter: "#F97316",
  weibo: "#E6162D", bilibili: "#00A7D1", local: "#6B7280", manual: "#6B7280",
};

function sourceColor(source: string): string { return SOURCE_COLORS[source] || "#6B7280"; }

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

  if (loading) return <div className="animate-pulse"><div className="h-32 bg-gray-200 dark:bg-slate-700 rounded" /></div>;
  if (!data?.days?.length) return null;

  const cellSize = 11, gap = 2, step = cellSize + gap;

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <button onClick={() => setYearOffset(yearOffset - 1)} className="px-2 py-0.5 text-xs border rounded hover:bg-gray-100 dark:hover:bg-slate-700 dark:border-slate-600">←</button>
          <span className="text-sm font-medium dark:text-white">{new Date(Date.UTC(new Date().getUTCFullYear() + yearOffset, 0, 1)).getUTCFullYear()}</span>
          <button onClick={() => setYearOffset(yearOffset + 1)} className="px-2 py-0.5 text-xs border rounded hover:bg-gray-100 dark:hover:bg-slate-700 dark:border-slate-600">→</button>
        </div>
        <span className="text-xs text-gray-400">{data.total} works</span>
      </div>

      <div className="overflow-x-auto">
        <svg width={weeks.length * step + 40} height={7 * step + 20} className="text-xs">
          {months.map((m, i) => (
            <text key={i} x={m.col * step + 10} y={10} className="fill-gray-400 dark:fill-gray-500" fontSize="9">{m.label}</text>
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
        <span className="text-gray-400">Less</span>
        {[0, 1, 2, 3, 4].map((lvl) => (
          <div key={lvl} className="w-3 h-3 rounded-sm" style={{ backgroundColor: lvl === 0 ? "rgba(128,128,128,0.08)" : `rgba(100,100,100,${[0,0.2,0.4,0.65,1][lvl]})` }} />
        ))}
        <span className="text-gray-400">More</span>
        <span className="mx-2 text-gray-300 dark:text-gray-600">|</span>
        {data.sources.map((s) => (
          <span key={s} className="flex items-center gap-1">
            <span className="w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: sourceColor(s) }} />
            <span className="text-gray-500 dark:text-gray-400">{s}</span>
          </span>
        ))}
      </div>

      {selectedDay && (
        <div className="mt-3 p-3 bg-gray-50 dark:bg-slate-700/50 rounded text-sm">
          <div className="flex items-center justify-between">
            <span className="font-medium dark:text-white">{selectedDay.date}</span>
            <button onClick={() => setSelectedDay(null)} className="text-gray-400 hover:text-gray-600">×</button>
          </div>
          <div className="flex gap-3 mt-1 text-xs">
            {data.sources.map((s) => {
              const cnt = (selectedDay[s] as number) || 0;
              if (!cnt) return null;
              return <span key={s} className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm" style={{ backgroundColor: sourceColor(s) }} />{s}: {cnt}</span>;
            })}
          </div>
        </div>
      )}
    </div>
  );
}
