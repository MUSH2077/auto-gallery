"use client";

import {
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from "react";

export interface EntityEntranceProps {
  className: string;
  style?: CSSProperties;
}

export function EntityList({
  children,
  label,
  className = "",
}: {
  children: ReactNode;
  label?: string;
  className?: string;
}) {
  return (
    <div role="list" aria-label={label} className={`entity-list ${className}`}>
      {children}
    </div>
  );
}

export function EntityRow({
  children,
  label,
  onOpen,
  selected = false,
  entrance,
  className = "",
}: {
  children: ReactNode;
  label: string;
  onOpen: () => void;
  selected?: boolean;
  entrance?: EntityEntranceProps;
  className?: string;
}) {
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onOpen();
    }
  };

  return (
    <div
      role="listitem"
      className={entrance?.className}
      style={entrance?.style}
    >
      <div
        role="link"
        tabIndex={0}
        aria-label={label}
        onClick={onOpen}
        onKeyDown={handleKeyDown}
        className={`entity-row ${selected ? "entity-row-selected" : ""} ${className}`}
      >
        {children}
      </div>
    </div>
  );
}

