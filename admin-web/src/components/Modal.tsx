"use client";
import { X } from "lucide-react";
import { ReactNode, useEffect, useId, useRef } from "react";
import { useT } from "@/lib/i18n";
import { usePresence } from "@/lib/motion";

export default function Modal({ open, onClose, title, children }: {
  open: boolean; onClose: () => void; title: string; children: ReactNode;
}) {
  const t = useT();
  const titleId = useId();
  const modalRef = useRef<HTMLDivElement>(null);
  const prevFocus = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const { mounted, closing } = usePresence(open);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open || !modalRef.current) return;
    prevFocus.current = document.activeElement as HTMLElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const first = modalRef.current.querySelector<HTMLElement>(
      'input:not([disabled]), button:not([disabled]), a[href], select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    );
    window.requestAnimationFrame(() => first?.focus());

    const trap = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab" || !modalRef.current) return;
      const els = Array.from(modalRef.current.querySelectorAll<HTMLElement>(
        'input:not([disabled]), button:not([disabled]), a[href], select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )).filter((element) => !element.hasAttribute("hidden"));
      if (!els.length) return;
      if (e.shiftKey && document.activeElement === els[0]) { e.preventDefault(); els[els.length-1].focus(); }
      else if (!e.shiftKey && document.activeElement === els[els.length-1]) { e.preventDefault(); els[0].focus(); }
    };
    document.addEventListener("keydown", trap);
    return () => {
      document.removeEventListener("keydown", trap);
      document.body.style.overflow = previousOverflow;
      prevFocus.current?.focus();
    };
  }, [open]);

  if (!mounted) return null;
  return (
    <div
      className={`fixed inset-0 z-50 flex items-start justify-center bg-black/50 px-4 pt-16 ${closing ? "overlay-backdrop-exit" : "overlay-backdrop"}`}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={`max-h-[82vh] w-full max-w-lg overflow-y-auto rounded-md border border-border bg-white shadow-xl dark:border-border dark:bg-surface ${closing ? "overlay-panel-exit" : "overlay-panel"}`}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3 dark:border-border">
          <h2 id={titleId} className="text-base font-semibold text-fg">{title}</h2>
          <button type="button" onClick={onClose} aria-label={t("common.close_dialog")}
            className="btn-icon border-0">
            <X aria-hidden="true" className="h-5 w-5" />
          </button>
        </div>
        <div className="px-4 py-4">{children}</div>
      </div>
    </div>
  );
}
