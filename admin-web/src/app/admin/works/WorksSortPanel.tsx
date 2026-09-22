"use client";

import { RefreshCw } from "lucide-react";

import { useT } from "@/lib/i18n";

export type WorksSortValue =
  | "relevance"
  | "heat-desc"
  | "random"
  | "created-desc"
  | "created-asc"
  | "posted-desc"
  | "posted-asc"
  | "updated-desc"
  | "updated-asc"
  | "title-desc"
  | "title-asc";

function SortChoice({
  value,
  current,
  label,
  onChange,
}: {
  value: WorksSortValue;
  current: WorksSortValue;
  label: string;
  onChange: (value: WorksSortValue) => void;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={current === value}
      onClick={() => onChange(value)}
      className={`flex min-h-11 w-full items-center justify-between rounded-lg border px-3 py-2 text-left text-sm ${current === value ? "border-accent bg-accent-subtle text-accent" : "border-border text-fg hover:bg-subtle"}`}
    >
      <span>{label}</span>
      {current === value ? <span aria-hidden>✓</span> : null}
    </button>
  );
}

export function WorksSortPanel({
  value,
  hasText,
  onChange,
  onReshuffle,
}: {
  value: WorksSortValue;
  hasText: boolean;
  onChange: (value: WorksSortValue) => void;
  onReshuffle: () => void;
}) {
  const t = useT();
  const choices: { value: WorksSortValue; label: string }[] = [
    ...(hasText ? [{ value: "relevance" as const, label: t("works.sort_relevance") }] : []),
    { value: "heat-desc", label: t("works.sort_heat") },
    { value: "random", label: t("works.sort_random") },
    { value: "created-desc", label: t("works.sort_imported_desc") },
    { value: "created-asc", label: t("works.sort_imported_asc") },
    { value: "posted-desc", label: t("works.sort_posted_desc") },
    { value: "posted-asc", label: t("works.sort_posted_asc") },
    { value: "updated-desc", label: t("works.sort_updated_desc") },
    { value: "updated-asc", label: t("works.sort_updated_asc") },
    { value: "title-asc", label: t("works.sort_title_asc") },
    { value: "title-desc", label: t("works.sort_title_desc") },
  ];
  return (
    <fieldset>
      <legend className="sr-only">{t("works.sort_panel_title")}</legend>
      <div className="grid gap-2">
        {choices.map((choice) => (
          <SortChoice key={choice.value} {...choice} current={value} onChange={onChange} />
        ))}
      </div>
      {value === "random" ? (
        <button type="button" className="btn-ghost mt-3 w-full" onClick={onReshuffle}>
          <RefreshCw className="h-4 w-4" aria-hidden />
          {t("works.reshuffle")}
        </button>
      ) : null}
    </fieldset>
  );
}
