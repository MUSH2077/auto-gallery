"use client";

import {
  nextReferenceSortValue,
  type ReferenceSort,
  type ReferenceSortField,
} from "@/lib/reference-list-state";

const FIELDS: ReferenceSortField[] = ["name", "created", "updated"];

export default function ReferenceSortControl({
  value,
  labels,
  onChange,
}: {
  value: ReferenceSort;
  labels: Record<ReferenceSortField, string> & {
    group: string;
    ascending: string;
    descending: string;
  };
  onChange: (value: string) => void;
}) {
  return (
    <div
      className="segmented-control max-w-full flex-wrap"
      role="group"
      aria-label={labels.group}
    >
      {FIELDS.map((field) => {
        const active = value.field === field;
        const directionLabel = value.direction === "asc"
          ? labels.ascending
          : labels.descending;
        return (
          <button
            key={field}
            type="button"
            aria-pressed={active}
            aria-label={active ? `${labels[field]} · ${directionLabel}` : labels[field]}
            className={`segment ${active ? "segment-active" : ""}`}
            onClick={() => onChange(nextReferenceSortValue(value, field))}
          >
            <span>{labels[field]}</span>
            {active ? (
              <span aria-hidden="true" className="ml-1 text-[10px]">
                {value.direction === "asc" ? "↑" : "↓"}
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
