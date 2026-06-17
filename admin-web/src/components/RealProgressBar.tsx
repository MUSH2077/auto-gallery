"use client";

interface ProgressData {
  stage?: string;
  current?: number;
  total?: number;
  percent?: number;
}

export function RealProgressBar({ progress }: { progress: ProgressData | null }) {
  if (!progress || progress.percent == null) return null;

  const pct = Math.min(100, Math.max(0, progress.percent));
  const stage = progress.stage ?? "processing";

  return (
    <div className="flex flex-col gap-0.5 w-36 shrink-0">
      <div className="w-full h-2 bg-gray-200 dark:bg-slate-600 rounded-full overflow-hidden">
        <div
          className="h-full bg-blue-500 rounded-full transition-all duration-500 ease-out"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="flex justify-between text-[10px] text-gray-400 tabular-nums">
        <span>{stage}</span>
        <span>
          {progress.current ?? "-"}/{progress.total ?? "-"} ({pct}%)
        </span>
      </div>
    </div>
  );
}
