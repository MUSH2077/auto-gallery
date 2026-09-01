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
      className="compact-selection-hitbox"
      onClick={stopPropagation ? (event) => event.stopPropagation() : undefined}
    >
      <input
        ref={inputRef}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-label={ariaLabel}
        onChange={onChange}
        className="compact-selection-checkbox"
      />
    </span>
  );
}
