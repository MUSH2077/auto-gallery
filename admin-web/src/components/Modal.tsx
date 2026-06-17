"use client";
import { ReactNode, useEffect, useRef } from "react";

export default function Modal({ open, onClose, title, children }: {
  open: boolean; onClose: () => void; title: string; children: ReactNode;
}) {
  const modalRef = useRef<HTMLDivElement>(null);
  const prevFocus = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open || !modalRef.current) return;
    prevFocus.current = document.activeElement as HTMLElement;
    const first = modalRef.current.querySelector<HTMLElement>(
      'input, button, select, textarea, [tabindex]:not([tabindex="-1"])'
    );
    first?.focus();

    const trap = (e: KeyboardEvent) => {
      if (e.key === "Escape") { onClose(); return; }
      if (e.key !== "Tab" || !modalRef.current) return;
      const els = modalRef.current.querySelectorAll<HTMLElement>(
        'input:not([disabled]), button:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      );
      if (!els.length) return;
      if (e.shiftKey && document.activeElement === els[0]) { e.preventDefault(); els[els.length-1].focus(); }
      else if (!e.shiftKey && document.activeElement === els[els.length-1]) { e.preventDefault(); els[0].focus(); }
    };
    document.addEventListener("keydown", trap);
    return () => { document.removeEventListener("keydown", trap); prevFocus.current?.focus(); };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 bg-black/50 flex items-start justify-center z-50 px-4 pt-16"
      onClick={onClose} role="dialog" aria-modal="true" aria-label={title}>
      <div ref={modalRef} className="w-full max-w-lg max-h-[82vh] overflow-y-auto rounded-md border border-ag-border bg-white shadow-xl dark:border-ag-border dark:bg-ag-surface"
        onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-ag-border px-4 py-3 dark:border-ag-border">
          <h2 className="text-base font-semibold text-[#24292f] dark:text-ag-text">{title}</h2>
          <button onClick={onClose} aria-label=t("common.close")
            className="btn-icon border-0 text-lg leading-none">&times;</button>
        </div>
        <div className="px-4 py-4">{children}</div>
      </div>
    </div>
  );
}
