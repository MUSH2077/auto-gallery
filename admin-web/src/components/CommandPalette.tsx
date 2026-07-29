"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, Search } from "lucide-react";

import {
  ADMIN_LINK_MODULE,
  ADMIN_NAV_LINKS,
  type AdminNavLink,
} from "@/lib/adminNavigation";
import { useT } from "@/lib/i18n";
import { usePresence } from "@/lib/motion";
import { usePermissions } from "@/lib/usePermissions";

type PaletteItem = AdminNavLink & { displayLabel: string; searchTarget?: boolean };

export default function CommandPalette({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const t = useT();
  const router = useRouter();
  const { has, isAdmin } = usePermissions();
  const { mounted, closing } = usePresence(open);
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);

  const availableLinks = useMemo(() => ADMIN_NAV_LINKS.filter((item) => {
    if (item.topbarOnly) return false;
    if (item.adminOnly && !isAdmin) return false;
    const module = ADMIN_LINK_MODULE[item.href];
    return !module || has(module);
  }), [has, isAdmin]);

  const items = useMemo<PaletteItem[]>(() => {
    const normalized = query.trim().toLocaleLowerCase();
    const matched: PaletteItem[] = availableLinks
      .map((item) => ({ ...item, displayLabel: t(item.labelKey) }))
      .filter((item) => {
        if (!normalized) return true;
        const haystack = [
          item.displayLabel,
          item.href,
          ...(item.keywords || []),
        ].join(" ").toLocaleLowerCase();
        return haystack.includes(normalized);
      })
      .slice(0, 10);
    if (normalized && has("library")) {
      matched.push({
        href: `/admin/search?q=${encodeURIComponent(query.trim())}`,
        labelKey: "search.title",
        icon: "image",
        context: "library",
        displayLabel: t("search.no_results_for").replace("{query}", query.trim()),
        searchTarget: true,
      });
    }
    return matched;
  }, [availableLinks, has, query, t]);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIndex(0);
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [open]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        setActiveIndex((index) => items.length ? (index + 1) % items.length : 0);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setActiveIndex((index) => items.length ? (index - 1 + items.length) % items.length : 0);
      } else if (event.key === "Enter" && items[activeIndex]) {
        event.preventDefault();
        router.push(items[activeIndex].href);
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [activeIndex, items, onClose, open, router]);

  if (!mounted) return null;

  return (
    <div className="fixed inset-0 z-[80] flex items-start justify-center px-3 pt-[12vh]" role="presentation">
      <button
        type="button"
        className={`absolute inset-0 bg-black/45 ${closing ? "overlay-backdrop-exit" : "overlay-backdrop"}`}
        onClick={onClose}
        aria-label={t("common.close")}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("search.title")}
        className={`relative w-full max-w-xl overflow-hidden rounded-xl border border-border bg-surface shadow-overlay ${closing ? "overlay-panel-exit" : "overlay-panel"}`}
      >
        <div className="flex items-center gap-3 border-b border-border px-4">
          <Search className="h-[18px] w-[18px] shrink-0 text-muted" strokeWidth={1.8} aria-hidden />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="h-12 min-w-0 flex-1 bg-transparent text-sm text-fg outline-none placeholder:text-placeholder"
            placeholder={t("search.placeholder")}
            aria-controls="admin-command-results"
            aria-activedescendant={items[activeIndex] ? `admin-command-${activeIndex}` : undefined}
          />
          <kbd className="rounded border border-border bg-subtle px-1.5 py-0.5 font-mono text-[10px] text-muted">ESC</kbd>
        </div>
        <div id="admin-command-results" role="listbox" className="max-h-[min(60vh,420px)] overflow-y-auto p-2">
          {items.length === 0 && (
            <div className="px-3 py-8 text-center text-sm text-muted">{t("search.no_results")}</div>
          )}
          {items.map((item, index) => (
            <button
              id={`admin-command-${index}`}
              key={`${item.href}-${item.searchTarget ? "search" : "route"}`}
              type="button"
              role="option"
              aria-selected={index === activeIndex}
              onMouseEnter={() => setActiveIndex(index)}
              onClick={() => {
                router.push(item.href);
                onClose();
              }}
              className={`flex min-h-11 w-full items-center gap-3 rounded-lg px-3 text-left text-sm ${
                index === activeIndex ? "bg-accent-subtle text-fg" : "text-muted hover:bg-subtle hover:text-fg"
              }`}
            >
              <span className="min-w-0 flex-1 truncate font-medium">{item.displayLabel}</span>
              {!item.searchTarget && <span className="hidden truncate font-mono text-[10px] text-placeholder sm:block">{item.href}</span>}
              <ArrowRight className="h-4 w-4 shrink-0" strokeWidth={1.8} aria-hidden />
            </button>
          ))}
        </div>
        <div className="flex items-center gap-3 border-t border-border bg-subtle px-4 py-2 text-[10px] text-muted">
          <span>↑↓ {t("common.select")}</span>
          <span>↵ {t("common.open")}</span>
          <span className="ml-auto">Ctrl/⌘ K</span>
        </div>
      </div>
    </div>
  );
}
