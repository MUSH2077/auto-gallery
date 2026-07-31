"use client";

import type { SyncOutcome } from "@/lib/api";
import { useT } from "@/lib/i18n";

const toneClasses: Record<SyncOutcome["code"], string> = {
  new_content: "border-success/30 bg-success-subtle text-success",
  no_changes: "border-accent/25 bg-accent-subtle text-accent",
  no_content: "border-border bg-subtle text-muted",
};

export function SyncOutcomeBadge({ outcome }: { outcome: SyncOutcome }) {
  const t = useT();
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${toneClasses[outcome.code]}`}>
      <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
      {t(`sync_outcome.${outcome.code}`)}
    </span>
  );
}

export function SyncOutcomeNotice({
  outcome,
  compact = false,
}: {
  outcome: SyncOutcome;
  compact?: boolean;
}) {
  const t = useT();
  if (compact) return <SyncOutcomeBadge outcome={outcome} />;
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-2 rounded-md border border-border bg-subtle px-3 py-2 text-xs text-muted">
      <SyncOutcomeBadge outcome={outcome} />
      <span>{t(`sync_outcome.${outcome.code}_desc`)}</span>
    </div>
  );
}
