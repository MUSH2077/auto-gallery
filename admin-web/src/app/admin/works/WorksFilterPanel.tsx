"use client";

import { useEffect, useState } from "react";

import { useT } from "@/lib/i18n";

export type WorksVisibilityFilter = "visible" | "trashed";
export type WorksSafetyFilter = "all" | "sfw" | "nsfw";
export type WorksAiFilter = "all" | "human" | "ai";
export type WorksMediaFilter = "image" | "animation" | "video" | "multiple-assets";

export interface WorksFilterValue {
  visibility: WorksVisibilityFilter;
  sources: string[];
  safety: WorksSafetyFilter;
  ai: WorksAiFilter;
  favorite: boolean;
  media: WorksMediaFilter[];
}

const SOURCE_OPTIONS = ["pixiv", "x", "iwara", "danbooru", "pinterest", "lofter", "weibo"];

function ChoiceGroup<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <fieldset>
      <legend className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">{label}</legend>
      <div className="segmented-control flex w-full">
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={value === option.value}
            onClick={() => onChange(option.value)}
            className={`segment min-h-9 flex-1 ${value === option.value ? "segment-active" : ""}`}
          >
            {option.label}
          </button>
        ))}
      </div>
    </fieldset>
  );
}

function toggleValue<T extends string>(values: T[], value: T) {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

export function WorksFilterPanel({
  value,
  onApply,
  onCancel,
  applying = false,
}: {
  value: WorksFilterValue;
  onApply: (value: WorksFilterValue) => void;
  onCancel: () => void;
  applying?: boolean;
}) {
  const t = useT();
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  const cancel = () => {
    setDraft(value);
    onCancel();
  };
  const mediaOptions: { value: WorksMediaFilter; label: string }[] = [
    { value: "image", label: t("works.media_image") },
    { value: "animation", label: t("works.media_animation") },
    { value: "video", label: t("works.media_video") },
    { value: "multiple-assets", label: t("works.media_multiple") },
  ];

  return (
    <div className="space-y-5">
      <ChoiceGroup
        label={t("works.collection")}
        value={draft.visibility}
        options={[
          { value: "visible", label: t("works.gallery") },
          { value: "trashed", label: t("works.trash") },
        ]}
        onChange={(visibility) => setDraft((current) => ({ ...current, visibility }))}
      />
      <fieldset>
        <legend className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">{t("works.sources")}</legend>
        <div className="grid grid-cols-2 gap-2">
          {SOURCE_OPTIONS.map((source) => (
            <label key={source} className="flex min-h-10 cursor-pointer items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm text-fg hover:bg-subtle">
              <input
                type="checkbox"
                checked={draft.sources.includes(source)}
                onChange={() => setDraft((current) => ({ ...current, sources: toggleValue(current.sources, source) }))}
              />
              {source === "x" ? "X" : source[0].toUpperCase() + source.slice(1)}
            </label>
          ))}
        </div>
      </fieldset>
      <ChoiceGroup
        label={t("works.safety")}
        value={draft.safety}
        options={[
          { value: "all", label: t("works.filter_all") },
          { value: "sfw", label: t("works.filter_sfw") },
          { value: "nsfw", label: t("works.filter_nsfw") },
        ]}
        onChange={(safety) => setDraft((current) => ({ ...current, safety }))}
      />
      <ChoiceGroup
        label={t("works.ai_section")}
        value={draft.ai}
        options={[
          { value: "all", label: t("works.ai_filter_all") },
          { value: "human", label: t("works.ai_filter_human") },
          { value: "ai", label: t("works.ai_filter_ai") },
        ]}
        onChange={(ai) => setDraft((current) => ({ ...current, ai }))}
      />
      <label className="flex min-h-11 cursor-pointer items-center justify-between rounded-lg border border-border px-3 py-2 text-sm text-fg">
        {t("works.only_favorites")}
        <input type="checkbox" checked={draft.favorite} onChange={(event) => setDraft((current) => ({ ...current, favorite: event.target.checked }))} />
      </label>
      <fieldset>
        <legend className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">{t("works.media_type")}</legend>
        <div className="grid grid-cols-2 gap-2">
          {mediaOptions.map((option) => (
            <label key={option.value} className="flex min-h-10 cursor-pointer items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm text-fg hover:bg-subtle">
              <input
                type="checkbox"
                checked={draft.media.includes(option.value)}
                onChange={() => setDraft((current) => ({ ...current, media: toggleValue(current.media, option.value) }))}
              />
              {option.label}
            </label>
          ))}
        </div>
      </fieldset>
      <footer className="sticky bottom-0 -mx-4 -mb-4 flex justify-end gap-2 border-t border-border bg-surface px-4 py-3">
        <button type="button" className="btn-ghost" onClick={cancel}>{t("common.cancel")}</button>
        <button type="button" className="btn-primary" disabled={applying} onClick={() => onApply(draft)}>
          {applying ? t("common.applying") : t("common.apply")}
        </button>
      </footer>
    </div>
  );
}
