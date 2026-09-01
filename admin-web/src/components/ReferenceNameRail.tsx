"use client";

import type { ReactNode } from "react";
import type { ReferenceNameAnchorRead } from "@/lib/api";

export function ReferenceListLayout({
  children,
  rail,
}: {
  children: ReactNode;
  rail?: ReactNode;
}) {
  return (
    <div className="reference-list-layout">
      <div className="min-w-0">{children}</div>
      {rail}
    </div>
  );
}

export default function ReferenceNameRail({
  items,
  ariaLabel,
  jumpLabel,
  emptyLabel,
  onSelect,
}: {
  items: ReferenceNameAnchorRead[];
  ariaLabel: string;
  jumpLabel: (label: string, count: number) => string;
  emptyLabel: (label: string) => string;
  onSelect: (anchor: ReferenceNameAnchorRead) => void;
}) {
  return (
    <nav className="reference-name-rail" aria-label={ariaLabel}>
      {items.map((item) => {
        const disabled = item.offset === null || item.count === 0;
        return (
          <button
            key={item.key}
            type="button"
            disabled={disabled}
            aria-label={disabled
              ? emptyLabel(item.label)
              : jumpLabel(item.label, item.count)}
            className="reference-name-anchor"
            onClick={() => onSelect(item)}
          >
            {item.label}
          </button>
        );
      })}
    </nav>
  );
}
