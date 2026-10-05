"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { ArrowRight } from "lucide-react";

import {
  ADMIN_LINK_MODULE,
  ADMIN_NAV_LINKS,
  hasAdminPermission,
  type AdminNavLink,
} from "@/lib/adminNavigation";
import { useT } from "@/lib/i18n";
import { usePresence } from "@/lib/motion";
import { useDialogFocus } from "@/lib/useDialogFocus";
import { usePermissions } from "@/lib/usePermissions";
import { SmartSearchInput } from "@/components/SmartSearchInput";

type PaletteItem = AdminNavLink & { displayLabel: string; searchTarget?: boolean };

export default function CommandPalette({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const t = useT();
  const pathname = usePathname();
  const params = useSearchParams().toString();
  const currentHref = params ? `${pathname}?${params}` : pathname;
  const previousHref = useRef(currentHref);
  const router = useRouter();
  const { has, isAdmin } = usePermissions();
  const { mounted, closing } = usePresence(open);
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(open && mounted, dialogRef, onClose);
  const itemRefs = useRef<Array<HTMLAnchorElement | null>>([]);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);

  const availableLinks = useMemo(() => ADMIN_NAV_LINKS.filter((item) => {
    if (item.topbarOnly) return false;
    if (item.adminOnly && !isAdmin) return false;
    return hasAdminPermission(ADMIN_LINK_MODULE[item.href], has);
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
    if (normalized && (has("library") || has("subscriptions"))) {
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
  }, [open]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  useEffect(() => {
    if (previousHref.current === currentHref) return;
    previousHref.current = currentHref;
    if (open) onClose();
  }, [currentHref, open, onClose]); // Close on completed path or query navigation.

  useEffect(() => {
    if (open) itemRefs.current[activeIndex]?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, open]);

  if (!mounted) return null;

  return (
    <div className="fixed inset-0 z-[80] flex items-start justify-center px-3 pt-[12vh]" role="presentation">
      <button
        type="button"
        tabIndex={-1}
        className={`absolute inset-0 bg-black/45 ${closing ? "overlay-backdrop-exit" : "overlay-backdrop"}`}
        onClick={onClose}
        aria-label={t("common.close")}
      />
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={t("search.title")}
        onKeyDownCapture={(event) => {
          if (event.defaultPrevented || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
          if (event.key === "Escape") {
            event.preventDefault();
            onClose();
            return;
          }
          const inputFocused = event.target === inputRef.current;
          const focusedIndex = itemRefs.current.findIndex(item => item !== null && item === event.target);
          // Buttons keep their native activation. Enter on a link activates
          // that link, independent of hover or the input's selected result.
          if (!inputFocused && focusedIndex < 0) return;
          if ((event.key === "ArrowDown" || event.key === "ArrowUp") && items.length) {
            event.preventDefault();
            const index = inputFocused ? activeIndex : focusedIndex;
            const next = (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
            setActiveIndex(next);
            if (!inputFocused) itemRefs.current[next]?.focus();
          } else if (event.key === "Enter" && inputFocused && items[activeIndex]) {
            event.preventDefault();
            itemRefs.current[activeIndex]?.click();
          }
        }}
        className={`relative w-full max-w-xl overflow-hidden rounded-xl border border-border bg-surface shadow-overlay ${closing ? "overlay-panel-exit" : "overlay-panel"}`}
      >
        <div className="flex items-center gap-3 border-b border-border px-3 py-2">
          <SmartSearchInput
            ref={inputRef}
            value={query}
            onChange={setQuery}
            scope="global"
            showTokens={false}
            className="min-w-0 flex-1"
            placeholder={t("search.placeholder")}
            ariaLabel={t("search.placeholder")}
            keyboardNavigation={false}
            showSuggestions={false}
            aria-controls="admin-command-results"
            aria-expanded={items.length > 0}
            aria-activedescendant={items[activeIndex] ? `admin-command-${activeIndex}` : undefined}
          />
          <kbd className="rounded border border-border bg-subtle px-1.5 py-0.5 font-mono text-[10px] text-muted">ESC</kbd>
        </div>
        <div id="admin-command-results" role="listbox" className="max-h-[min(60vh,420px)] overflow-y-auto p-2">
          {items.length === 0 && (
            <div className="px-3 py-8 text-center text-sm text-muted">{t("search.no_results")}</div>
          )}
          {items.map((item, index) => (
            <Link
              ref={(node) => { itemRefs.current[index] = node; }}
              id={`admin-command-${index}`}
              key={`${item.href}-${item.searchTarget ? "search" : "route"}`}
              href={item.href}
              onClick={(event) => {
                event.preventDefault();
                const target = new URL(item.href, window.location.origin);
                if (target.pathname === pathname && target.searchParams.toString() === params) onClose();
                else router.push(item.href);
              }}
              role="option"
              aria-selected={index === activeIndex}
              onFocus={() => setActiveIndex(index)}
              onMouseEnter={() => {
                if (document.activeElement === inputRef.current) setActiveIndex(index);
              }}
              className={`flex min-h-11 w-full items-center gap-3 rounded-lg px-3 text-left text-sm ${
                index === activeIndex ? "bg-accent-subtle text-fg" : "text-muted hover:bg-subtle hover:text-fg"
              }`}
            >
              <span className="min-w-0 flex-1 truncate font-medium">{item.displayLabel}</span>
              {!item.searchTarget && <span className="hidden truncate font-mono text-[10px] text-placeholder sm:block">{item.href}</span>}
              <ArrowRight className="h-4 w-4 shrink-0" strokeWidth={1.8} aria-hidden />
            </Link>
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
