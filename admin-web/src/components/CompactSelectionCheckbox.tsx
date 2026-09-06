"use client";

import { useEffect, useRef, type ChangeEvent } from "react";

export default function CompactSelectionCheckbox({
  checked,
  indeterminate = false,
  ariaLabel,
  onChange,
  stopPropagation = true,
  disabled = false,
}: {
  checked: boolean;
  indeterminate?: boolean;
  ariaLabel: string;
  onChange: (event: ChangeEvent<HTMLInputElement>) => void;
  stopPropagation?: boolean;
  disabled?: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (inputRef.current) inputRef.current.indeterminate = indeterminate;
  }, [indeterminate]);

  return (
    <span
      className="compact-selection-hitbox relative"
      onClick={stopPropagation ? (event) => event.stopPropagation() : undefined}
    >
      <input
        ref={inputRef}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-label={ariaLabel}
        onChange={onChange}
        className="compact-selection-checkbox peer"
      />
      <span
        aria-hidden="true"
        className={`compact-selection-visual ${checked || indeterminate ? "compact-selection-visual-active" : ""}`}
      >
        {indeterminate ? (
          <span className="h-0.5 w-2 rounded-full bg-white" />
        ) : checked ? (
          <svg viewBox="0 0 16 16" className="h-3 w-3" fill="none">
            <path d="m3.25 8.25 2.75 2.5 6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        ) : null}
      </span>
    </span>
  );
}
