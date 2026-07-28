"use client";

import { useId, type ReactNode } from "react";

function compactMiddle(value: string, head = 18, tail = 14): string {
  if (value.length <= head + tail + 1) return value;
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

export function compactUrl(value: string): string {
  try {
    const url = new URL(value);
    const parts = url.pathname.split("/").filter(Boolean);
    const tail = parts.at(-1);
    return tail ? `${url.host}/…/${tail}` : url.host;
  } catch {
    return compactMiddle(value);
  }
}

export function OverflowText({
  value,
  display,
  className = "",
  children,
}: {
  value: string;
  display?: string;
  className?: string;
  children?: ReactNode;
}) {
  const tooltipId = useId();
  return (
    <span
      className={`group/overflow relative block min-w-0 max-w-full ${className}`}
      tabIndex={0}
      aria-describedby={tooltipId}
    >
      <span className="block truncate">{children || display || value}</span>
      <span
        id={tooltipId}
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-0 z-50 mb-1 hidden max-w-[min(32rem,80vw)] break-words rounded-md border border-border bg-surface px-2 py-1.5 text-left text-xs font-normal leading-4 text-fg shadow-overlay group-hover/overflow:block group-focus/overflow:block"
      >
        {value}
      </span>
    </span>
  );
}

export function CompactUrl({
  value,
  className = "",
}: {
  value?: string | null;
  className?: string;
}) {
  if (!value) return <span className="text-muted">—</span>;
  return (
    <OverflowText
      value={value}
      display={compactUrl(value)}
      className={`font-mono text-[11px] text-muted ${className}`}
    />
  );
}

export function ErrorSummary({
  value,
  calm = false,
}: {
  value?: string | null;
  calm?: boolean;
}) {
  if (!value) return null;
  const firstLine = value.split("\n").find(Boolean) || value;
  return (
    <details
      className={`group/error rounded-md border px-2.5 py-1.5 text-xs ${
        calm
          ? "border-border bg-subtle text-muted"
          : "border-danger/25 bg-danger-subtle text-danger"
      }`}
    >
      <summary className="cursor-pointer list-none truncate font-medium [&::-webkit-details-marker]:hidden">
        {firstLine.slice(0, 180)}
      </summary>
      <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words border-t border-current/15 pt-2 font-mono text-[11px] leading-4">
        {value}
      </pre>
    </details>
  );
}
