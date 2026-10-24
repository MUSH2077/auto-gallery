"use client";

import { useEffect, useRef, type RefObject } from "react";

const FOCUSABLE = 'input:not([disabled]), button:not([disabled]), a[href], select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

// Save the original value only for the first owner. Nested dialogs may unmount
// in either order during navigation, so per-dialog snapshots cannot restore it.
const scrollLocks = new WeakMap<HTMLElement, { owners: number; overflow: string }>();
const inertLocks = new WeakMap<HTMLElement, { owners: number; inert: boolean }>();

function isolateDialog(dialog: HTMLElement) {
  const body = dialog.ownerDocument.body;
  const scroll = scrollLocks.get(body) || { owners: 0, overflow: body.style.overflow };
  scroll.owners++;
  scrollLocks.set(body, scroll);
  body.style.overflow = "hidden";
  const background: Array<{ element: HTMLElement; lock: { owners: number; inert: boolean } }> = [];
  for (let branch: HTMLElement | null = dialog.parentElement; branch?.parentElement; branch = branch.parentElement) {
    for (const sibling of Array.from(branch.parentElement.children)) {
      if (sibling !== branch && sibling instanceof HTMLElement) {
        const lock = inertLocks.get(sibling) || { owners: 0, inert: sibling.inert };
        lock.owners++;
        inertLocks.set(sibling, lock);
        sibling.inert = true;
        background.push({ element: sibling, lock });
      }
    }
    if (branch.parentElement === body) break;
  }
  return () => {
    for (const { element, lock } of background) {
      if (--lock.owners === 0) {
        element.inert = lock.inert;
        inertLocks.delete(element);
      }
    }
    if (--scroll.owners === 0) {
      body.style.overflow = scroll.overflow;
      scrollLocks.delete(body);
    }
  };
}

export function useDialogFocus(
  active: boolean,
  dialogRef: RefObject<HTMLElement | null>,
  onClose: () => void,
) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!active || !dialog) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    const releaseIsolation = isolateDialog(dialog);
    const focusables = () => Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE))
      .filter(element => element.tabIndex >= 0 && element.getClientRects().length > 0);
    const focusFirst = () => (focusables()[0] || dialog).focus();
    const frame = window.requestAnimationFrame(focusFirst);
    const trap = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.keyCode === 229 || dialog.closest("[inert]")) return;
      if (dialog.querySelector('[role="dialog"][aria-modal="true"]')) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
      } else if (event.key === "Tab") {
        const elements = focusables();
        const first = elements[0];
        const last = elements[elements.length - 1];
        if (!first || !dialog.contains(document.activeElement)) {
          event.preventDefault();
          focusFirst();
        } else if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", trap);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", trap);
      releaseIsolation();
      if (previousFocus?.isConnected && !previousFocus.closest("[inert]")) previousFocus.focus();
    };
  }, [active, dialogRef]);
}
