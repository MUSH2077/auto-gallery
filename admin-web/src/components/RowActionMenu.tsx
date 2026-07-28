"use client";

import Link from "next/link";
import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { createPortal } from "react-dom";

import { usePresence } from "@/lib/motion";

export interface RowActionItem {
  label: string;
  href?: string;
  onSelect?: () => void;
  tone?: "default" | "danger";
  disabled?: boolean;
}

function MoreIcon() {
  return (
    <svg viewBox="0 0 16 16" className="h-4 w-4" fill="currentColor" aria-hidden>
      <circle cx="3" cy="8" r="1.25" />
      <circle cx="8" cy="8" r="1.25" />
      <circle cx="13" cy="8" r="1.25" />
    </svg>
  );
}

export default function RowActionMenu({
  label,
  items,
}: {
  label: string;
  items: RowActionItem[];
}) {
  const [open, setOpen] = useState(false);
  const { mounted, closing } = usePresence(open);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const pendingFocus = useRef<"first" | "last" | null>(null);
  const menuId = useId();
  const [position, setPosition] = useState({ left: 0, top: 0 });

  const menuItems = () => Array.from(
    menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])') || [],
  );

  const measureTrigger = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const menuWidth = 176;
    setPosition({
      left: Math.max(8, Math.min(window.innerWidth - menuWidth - 8, rect.right - menuWidth)),
      top: rect.bottom + 4,
    });
  };

  const openAndFocus = (where: "first" | "last" = "first") => {
    pendingFocus.current = where;
    measureTrigger();
    setOpen(true);
  };

  useLayoutEffect(() => {
    if (!mounted || !open || !menuRef.current || !triggerRef.current) return;
    const menuRect = menuRef.current.getBoundingClientRect();
    const triggerRect = triggerRef.current.getBoundingClientRect();
    if (menuRect.bottom > window.innerHeight - 8) {
      setPosition((current) => ({
        ...current,
        top: Math.max(8, triggerRect.top - menuRect.height - 4),
      }));
    }
  }, [mounted, open]);

  useEffect(() => {
    if (mounted && open && pendingFocus.current) {
      const entries = menuItems();
      entries[pendingFocus.current === "first" ? 0 : entries.length - 1]?.focus();
      pendingFocus.current = null;
    }
  }, [mounted, open]);

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        rootRef.current
        && !rootRef.current.contains(target)
        && !menuRef.current?.contains(target)
      ) {
        setOpen(false);
      }
    };
    const handleViewportChange = () => setOpen(false);
    document.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("resize", handleViewportChange);
    window.addEventListener("scroll", handleViewportChange, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("resize", handleViewportChange);
      window.removeEventListener("scroll", handleViewportChange, true);
    };
  }, [open]);

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const entries = menuItems();
    const currentIndex = entries.indexOf(document.activeElement as HTMLElement);
    let nextIndex: number | null = null;
    if (event.key === "ArrowDown") nextIndex = (currentIndex + 1) % entries.length;
    if (event.key === "ArrowUp") nextIndex = (currentIndex - 1 + entries.length) % entries.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = entries.length - 1;
    if (nextIndex !== null) {
      event.preventDefault();
      entries[nextIndex]?.focus();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    }
  };

  const finishAction = (item: RowActionItem) => {
    setOpen(false);
    item.onSelect?.();
  };

  const menu = mounted && typeof document !== "undefined"
    ? createPortal((
      <div
        ref={menuRef}
        id={menuId}
        role="menu"
        aria-label={label}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={handleMenuKeyDown}
        className={`popover ${closing ? "popover-exit" : ""} fixed z-[70] w-44 overflow-hidden rounded-md border border-border bg-surface py-1 text-sm text-fg shadow-overlay dark:shadow-overlay-dark`}
        style={{ left: position.left, top: position.top }}
      >
        {items.map((item) => {
          const className = `row-menu-item ${item.tone === "danger" ? "row-menu-item-danger" : ""}`;
          if (item.href && !item.disabled) {
            return (
              <Link
                key={item.label}
                href={item.href}
                role="menuitem"
                tabIndex={-1}
                className={className}
                onClick={() => setOpen(false)}
              >
                {item.label}
              </Link>
            );
          }
          return (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              tabIndex={-1}
              aria-disabled={item.disabled || undefined}
              disabled={item.disabled}
              className={className}
              onClick={() => finishAction(item)}
            >
              {item.label}
            </button>
          );
        })}
      </div>
    ), document.body)
    : null;

  return (
    <>
    <div ref={rootRef} className="relative shrink-0" onClick={(event) => event.stopPropagation()}>
      <button
        ref={triggerRef}
        type="button"
        className="btn-icon row-menu-trigger"
        aria-label={label}
        title={label}
        aria-haspopup="menu"
        aria-controls={menuId}
        aria-expanded={open}
        onClick={() => {
          if (open) setOpen(false);
          else {
            measureTrigger();
            setOpen(true);
          }
        }}
        onKeyDown={(event) => {
          if ((event.key === "Enter" || event.key === " ") && !open) {
            event.preventDefault();
            openAndFocus("first");
            return;
          }
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            openAndFocus(event.key === "ArrowUp" ? "last" : "first");
            return;
          }
          if (event.key === "Escape" && open) {
            event.preventDefault();
            setOpen(false);
          }
        }}
      >
        <MoreIcon />
      </button>
    </div>
    {menu}
    </>
  );
}
