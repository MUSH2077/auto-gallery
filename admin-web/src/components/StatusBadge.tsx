"use client";

import { useT } from "@/lib/i18n";
import { statusLabel } from "@/lib/i18n-format";

type Tone = "success" | "danger" | "active" | "warning" | "muted" | "stale";

const toneClasses: Record<Tone, string> = {
  success: "bg-success-subtle text-success border-success/30",
  danger: "bg-danger-subtle text-danger border-danger/30",
  active: "bg-accent-subtle text-accent border-accent/30",
  warning: "bg-warning-subtle text-warning border-warning/30",
  muted: "bg-subtle text-muted border-muted/30 dark:bg-border",
  stale: "bg-warning-subtle text-warning border-warning/30",
};

const statusTone: Record<string, Tone> = {
  up: "success",
  complete: "success",
  completed: "success",
  downloaded: "active",
  down: "danger",
  failed: "danger",
  error: "danger",
  stale: "stale",
  paused: "warning",
  pending: "warning",
  enqueued: "warning",
  configuring: "active",
  downloading: "active",
  importing: "active",
  running: "active",
  post_download: "active",
  enqueuing_import: "active",
  import_indexing: "active",
  cancelled: "muted",
  unknown: "muted",
};

export function getStatusTone(status?: string | null): Tone {
  return statusTone[(status || "unknown").toLowerCase()] || "muted";
}

export default function StatusBadge({
  status,
  label,
  className = "",
  pulse,
}: {
  status?: string | null;
  label?: string;
  className?: string;
  pulse?: boolean;
}) {
  const t = useT();
  const s = (status || "unknown").toLowerCase();
  const isActive = pulse ?? ["running", "downloading", "importing", "configuring", "post_download", "enqueuing_import", "import_indexing"].includes(s);
  const tone = getStatusTone(s);
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${toneClasses[tone]} ${className}`}>
      <span className={`h-1.5 w-1.5 rounded-full bg-current ${isActive ? "animate-pulse" : ""}`} />
      {label || statusLabel(t, s)}
    </span>
  );
}
