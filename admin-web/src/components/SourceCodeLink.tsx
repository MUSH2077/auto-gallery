"use client";

import { Code2 } from "lucide-react";

import { useT } from "@/lib/i18n";

const DEFAULT_SOURCE_CODE_URL = "https://github.com/MUSH2077/auto-gallery";
export const SOURCE_CODE_URL =
  process.env.NEXT_PUBLIC_SOURCE_CODE_URL || DEFAULT_SOURCE_CODE_URL;

export default function SourceCodeLink({
  compact = false,
  className = "",
}: {
  compact?: boolean;
  className?: string;
}) {
  const t = useT();
  const label = t("sidebar.source_code");

  return (
    <a
      href={SOURCE_CODE_URL}
      target="_blank"
      rel="noreferrer"
      aria-label={label}
      title={label}
      data-testid="source-code-link"
      className={`inline-flex min-h-11 items-center rounded-md text-muted transition-colors hover:bg-subtle hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-surface ${
        compact ? "justify-center px-0" : "gap-2 px-3 text-xs font-medium"
      } ${className}`}
    >
      <Code2 className="h-[18px] w-[18px] shrink-0" strokeWidth={1.8} aria-hidden />
      {compact ? <span className="sr-only">{label}</span> : <span>{label}</span>}
    </a>
  );
}
