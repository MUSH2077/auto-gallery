"use client";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useT } from "@/lib/i18n";
import { usePresence } from "@/lib/motion";
import { isArchiveAsset } from "@/components";

export interface AssetData {
  id: string;
  file_name: string;
  file_path: string;
  width?: number;
  height?: number;
  mime_type?: string;
  thumb_sm_path?: string;
  thumb_url?: string;
  preview_url?: string;
  original_url?: string;
  created_at: string;
}

export function FullImageLightbox({ asset, onClose }: { asset: AssetData | null; onClose: () => void }) {
  const t = useT();
  const open = !!asset && !isArchiveAsset(asset);
  const { mounted, closing } = usePresence(open);
  // Keep the last asset around through the exit fade — `asset` is already
  // null while the overlay is animating out.
  const lastAsset = useRef<AssetData | null>(null);
  if (open) lastAsset.current = asset;
  const shown = open ? asset : lastAsset.current;

  useEffect(() => {
    if (!asset) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [asset, onClose]);

  if (!mounted || !shown) return null;

  return (
    <div
      className={`fixed inset-0 z-50 flex flex-col bg-black/95 ${closing ? "overlay-backdrop-exit" : "overlay-backdrop"}`}
      role="dialog"
      aria-modal="true"
      aria-label={shown.file_name}
      onClick={onClose}
    >
      <div className="flex items-center justify-between gap-3 border-b border-white/10 px-4 py-3 text-white">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">{shown.file_name}</div>
          {shown.width && shown.height && <div className="text-xs text-white/60">{shown.width} &times; {shown.height}</div>}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <a
            href={shown.original_url || ""}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded-md border border-white/20 px-3 py-1.5 text-sm text-white hover:bg-white/10"
            onClick={(e) => e.stopPropagation()}
          >
            {t("work_detail.open_original")}
          </a>
          <button onClick={onClose} className="rounded-md border border-white/20 px-3 py-1.5 text-sm text-white hover:bg-white/10" aria-label={t("common.close")}>
            {t("common.close")}
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-4" onClick={(e) => e.stopPropagation()}>
        <img
          src={shown.original_url || ""}
          alt={shown.file_name}
          className={`mx-auto h-auto max-h-none max-w-full object-contain ${closing ? "overlay-panel-exit" : "overlay-panel"}`}
        />
      </div>
    </div>
  );
}

export function ArrowIcon({ direction }: { direction: "left" | "right" }) {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      {direction === "left" ? <path d="m15 18-6-6 6-6" /> : <path d="m9 18 6-6-6-6" />}
    </svg>
  );
}

export function DisclosurePanel({
  storageKey,
  title,
  count,
  defaultOpen = false,
  children,
}: {
  storageKey: string;
  title: string;
  count?: number | string;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const t = useT();
  const [open, setOpen] = useState(() => {
    if (typeof window === "undefined") return defaultOpen;
    const stored = localStorage.getItem(storageKey);
    return stored === null ? defaultOpen : stored === "open";
  });
  // Lazy mount preserved: children (some run queries, e.g. History) only mount
  // on first open, then stay mounted so the collapse can animate.
  const [everOpen, setEverOpen] = useState(open);
  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next) setEverOpen(true);
    try { localStorage.setItem(storageKey, next ? "open" : "closed"); } catch {}
  };
  return (
    <section className="rounded-md border border-border bg-surface">
      <button type="button" onClick={toggle} className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left">
        <span className="text-sm font-semibold">{title}{count !== undefined ? <span className="ml-2 text-xs font-normal text-muted">({count})</span> : null}</span>
        <span className={`text-muted transition-transform ${open ? "rotate-90" : ""}`} aria-hidden>
          <ArrowIcon direction="right" />
        </span>
        <span className="sr-only">{open ? t("common.close") : t("common.open", "Open")}</span>
      </button>
      {/* grid-template-rows 0fr→1fr: animatable collapse without touching
          height (layout-anim red line); content clips via overflow-hidden. */}
      <div className={`grid transition-[grid-template-rows] duration-slow ease-expo ${open ? "grid-rows-[1fr]" : "grid-rows-[0fr]"}`}>
        <div className="overflow-hidden">
          {everOpen && <div className="border-t border-border px-4 py-3">{children}</div>}
        </div>
      </div>
    </section>
  );
}
