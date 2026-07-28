"use client";

import { useT } from "@/lib/i18n";

interface ProgressData {
  stage?: string;
  current?: number;
  total?: number;
  percent?: number;
  message?: string;
}

export function RealProgressBar({ progress }: { progress: ProgressData | null }) {
  const t = useT();
  if (!progress) return null;

  const computedPercent = progress.percent != null
    ? progress.percent
    : progress.total && progress.total > 0 && progress.current != null
      ? (progress.current / progress.total) * 100
      : null;
  const hasPercent = computedPercent != null;
  const pct = hasPercent ? Math.round(Math.min(100, Math.max(0, computedPercent ?? 0))) : null;
  const stage = progress.stage ?? "processing";
  const stageLabel = t(`progress.stage.${stage}`, t(`status.${stage}`, stage.replaceAll("_", " ")));
  const detail = progress.message || stageLabel;
  const hasCount = progress.current != null && progress.total != null;

  return (
    <div className="flex w-56 shrink-0 flex-col gap-1">
      <div className="flex items-center justify-between gap-2 text-[11px] leading-tight">
        <span className="min-w-0 truncate font-medium text-fg" title={detail}>
          {detail}
        </span>
        {pct != null && <span className="shrink-0 font-mono text-muted">{pct}%</span>}
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-border dark:bg-border">
        {hasPercent ? (
          // scaleX (not width): transform-only smoothing; the track's
          // overflow-hidden clips the rounded fill cleanly.
          <div
            className="h-full w-full rounded-full bg-accent transition-transform duration-slow ease-out"
            style={{ transform: `scaleX(${(pct ?? 0) / 100})`, transformOrigin: "left" }}
          />
        ) : (
          <div className="h-full w-3/5 animate-pulse rounded-full bg-accent" />
        )}
      </div>
      <div className="flex items-center justify-between gap-2 text-[10px] text-muted">
        <span className="min-w-0 truncate" title={stageLabel}>{stageLabel}</span>
        {hasCount && (
          <span className="shrink-0 font-mono tabular-nums">
            {progress.current}/{progress.total}
          </span>
        )}
      </div>
    </div>
  );
}
