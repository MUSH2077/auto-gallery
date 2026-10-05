"use client";
import { X } from "lucide-react";
import { ReactNode, useId, useRef } from "react";
import { useT } from "@/lib/i18n";
import { usePresence } from "@/lib/motion";
import { useDialogFocus } from "@/lib/useDialogFocus";

export default function Modal({ open, onClose, title, children }: {
  open: boolean; onClose: () => void; title: string; children: ReactNode;
}) {
  const t = useT();
  const titleId = useId();
  const modalRef = useRef<HTMLDivElement>(null);
  const { mounted, closing } = usePresence(open);

  useDialogFocus(open && mounted, modalRef, onClose);

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
        tabIndex={-1}
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
