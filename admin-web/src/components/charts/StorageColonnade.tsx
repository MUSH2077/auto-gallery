"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { useT } from "@/lib/i18n";
import { useI18nFormat } from "@/lib/i18n-format";
import { useStaggeredEntrance } from "@/lib/motion";

import { useChartTheme } from "./useChartTheme";

export interface StorageColonnadeChild {
  id: string;
  label: string;
  value: number;
  href?: string;
  source: string;
  sourceLabel: string;
  workCount: number;
}

export interface StorageColonnadeGroup {
  id: string;
  label: string;
  value: number;
  href: string;
  workCount: number;
  children: StorageColonnadeChild[];
}

export default function StorageColonnade({
  groups,
  formatValue,
  worksLabel,
  repositoriesLabel,
}: {
  groups: StorageColonnadeGroup[];
  formatValue: (value: number) => string;
  worksLabel: string;
  repositoriesLabel: string;
}) {
  const t = useT();
  const fmt = useI18nFormat();
  const theme = useChartTheme();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const maximum = useMemo(
    () => groups.reduce((current, group) => Math.max(current, group.value), 0),
    [groups],
  );
  const entrance = useStaggeredEntrance(groups.map((group) => group.id));

  const toggle = (id: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="space-y-2" data-chart-kind="storage-colonnade">
      {groups.map((group, index) => {
        const open = expanded.has(group.id);
        const contentId = `storage-colonnade-${group.id}`;
        const enter = entrance(group.id, index);
        const width = maximum > 0 ? (group.value / maximum) * 100 : 0;
        return (
          <div
            key={group.id}
            className={`${enter.className} overflow-hidden rounded-md border border-border bg-surface`}
            style={enter.style}
          >
            <div className="grid min-h-16 grid-cols-[auto_minmax(0,1fr)] items-center gap-2 px-2 py-2 sm:grid-cols-[auto_minmax(9rem,0.8fr)_minmax(10rem,1.2fr)_auto]">
              <button
                type="button"
                className="btn-icon"
                aria-controls={contentId}
                aria-expanded={open}
                aria-label={open
                  ? t("charts.collapse_group", { label: group.label })
                  : t("charts.expand_group", { label: group.label })}
                onClick={() => toggle(group.id)}
              >
                <span aria-hidden className={`transition-transform ${open ? "rotate-90" : ""}`}>›</span>
              </button>
              <div className="min-w-0">
                <Link href={group.href} className="block truncate text-sm font-semibold text-accent hover:underline">
                  <span className="mr-2 font-mono text-xs text-muted">{String(index + 1).padStart(2, "0")}</span>
                  {group.label}
                </Link>
                <div className="mt-1 text-xs text-muted">
                  {fmt.number(group.children.length)} {repositoriesLabel} · {fmt.number(group.workCount)} {worksLabel}
                </div>
              </div>
              <div className="col-span-2 min-w-0 pl-11 sm:col-span-1 sm:pl-0" aria-hidden>
                <div className="h-px w-full bg-border" />
                <div
                  className="-mt-[3px] h-[5px] origin-left rounded-full bg-accent"
                  style={{ transform: `scaleX(${width / 100})` }}
                />
              </div>
              <span className="col-span-2 pr-2 text-right font-mono text-xs font-semibold tabular-nums text-fg sm:col-span-1">
                {formatValue(group.value)}
              </span>
            </div>

            {open ? (
              <div id={contentId} className="border-t border-border bg-subtle/60 px-3 py-2 sm:pl-14">
                <div className="relative space-y-1 before:absolute before:bottom-4 before:left-[5px] before:top-4 before:w-px before:bg-border">
                  {group.children.map((child) => {
                    const childWidth = group.value > 0 ? (child.value / group.value) * 100 : 0;
                    const row = (
                      <>
                        <span
                          className="relative z-[1] h-2.5 w-2.5 shrink-0 rounded-full ring-2 ring-subtle"
                          style={{ backgroundColor: theme.colorFor(`source:${child.source}`) }}
                          aria-hidden
                        />
                        <span className="min-w-0 truncate text-xs font-medium text-fg">{child.label}</span>
                        <span className="hidden min-w-0 items-center gap-2 sm:flex" aria-hidden>
                          <span className="h-px flex-1 bg-border" />
                          <span
                            className="h-1.5 max-w-full rounded-full"
                            style={{
                              width: `${Math.max(2, childWidth)}%`,
                              backgroundColor: theme.colorFor(`source:${child.source}`),
                            }}
                          />
                        </span>
                        <span className="text-right text-[11px] text-muted">
                          {child.sourceLabel} · {fmt.number(child.workCount)} {worksLabel}
                        </span>
                        <span className="text-right font-mono text-xs tabular-nums text-fg">{formatValue(child.value)}</span>
                      </>
                    );
                    const className = "grid min-h-11 grid-cols-[auto_minmax(5rem,1fr)_auto] items-center gap-2 rounded-md px-1.5 hover:bg-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus sm:grid-cols-[auto_minmax(7rem,0.8fr)_minmax(6rem,1fr)_auto_auto]";
                    return child.href ? (
                      <Link
                        key={child.id}
                        href={child.href}
                        className={className}
                        aria-label={`${child.label}, ${child.sourceLabel}, ${formatValue(child.value)}, ${fmt.number(child.workCount)} ${worksLabel}`}
                      >
                        {row}
                      </Link>
                    ) : (
                      <div
                        key={child.id}
                        className={className}
                        aria-label={`${child.label}, ${child.sourceLabel}, ${formatValue(child.value)}, ${fmt.number(child.workCount)} ${worksLabel}`}
                      >
                        {row}
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
