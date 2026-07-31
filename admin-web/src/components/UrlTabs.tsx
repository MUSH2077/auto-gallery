"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type KeyboardEvent, useId, useRef } from "react";

export interface UrlTabItem {
  id: string;
  label: string;
  href: string;
}

export default function UrlTabs({
  tabs,
  activeId,
  ariaLabel,
  className = "",
}: {
  tabs: readonly UrlTabItem[];
  activeId: string;
  ariaLabel: string;
  className?: string;
}) {
  const router = useRouter();
  const selectId = useId();
  const tabRefs = useRef<Array<HTMLAnchorElement | null>>([]);

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>, index: number) => {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
    if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = tabs.length - 1;
    if (nextIndex === null) return;
    event.preventDefault();
    tabRefs.current[nextIndex]?.focus();
  };

  if (tabs.length <= 1) return null;

  return (
    <div className={`mb-6 ${className}`}>
      <label htmlFor={selectId} className="sr-only">
        {ariaLabel}
      </label>
      <select
        id={selectId}
        value={activeId}
        onChange={(event) => {
          const target = tabs.find((tab) => tab.id === event.target.value);
          if (target) router.push(target.href, { scroll: false });
        }}
        className="select min-h-11 w-full md:hidden"
        aria-label={ariaLabel}
      >
        {tabs.map((tab) => (
          <option key={tab.id} value={tab.id}>
            {tab.label}
          </option>
        ))}
      </select>

      <nav
        className="segmented-control hidden w-fit max-w-full flex-wrap md:flex"
        role="tablist"
        aria-label={ariaLabel}
      >
        {tabs.map((tab, index) => {
          const selected = tab.id === activeId;
          return (
            <Link
              key={tab.id}
              ref={(node) => { tabRefs.current[index] = node; }}
              href={tab.href}
              scroll={false}
              role="tab"
              aria-selected={selected}
              aria-current={selected ? "page" : undefined}
              tabIndex={selected ? 0 : -1}
              onKeyDown={(event) => handleKeyDown(event, index)}
              className={`segment min-h-11 ${selected ? "segment-active" : ""}`}
            >
              {tab.label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
