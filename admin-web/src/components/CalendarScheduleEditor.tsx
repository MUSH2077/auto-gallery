"use client";

import { Plus, X } from "lucide-react";
import type { CalendarScheduleRule } from "@/lib/api";
import { useT } from "@/lib/i18n";

export function defaultCalendarRule(): CalendarScheduleRule {
  return { frequency: "daily", times: ["03:00:00"] };
}

function normalizedTimes(rule: CalendarScheduleRule): string[] {
  return rule.times.length ? rule.times : ["03:00:00"];
}

export function ScheduleTimePicker({
  value,
  onChange,
}: {
  value: string[];
  onChange: (times: string[]) => void;
}) {
  const t = useT();
  const times = value.length ? value : ["03:00:00"];
  const setTime = (index: number, next: string) => {
    const updated = [...times];
    updated[index] = next.length === 5 ? `${next}:00` : next;
    onChange([...new Set(updated)].sort());
  };
  return (
    <div className="space-y-2">
      {times.map((time, index) => (
        <div key={`${time}-${index}`} className="flex items-center gap-2">
          <input
            type="time"
            step="1"
            value={time}
            onChange={(event) => setTime(index, event.target.value)}
            aria-label={t("subdefaults.scheduled_times")}
            className="input w-36 px-2 py-1 font-mono"
          />
          <button
            type="button"
            className="btn-icon text-danger"
            disabled={times.length === 1}
            onClick={() => onChange(times.filter((_, itemIndex) => itemIndex !== index))}
            aria-label={t("subdefaults.remove_time")}
          >
            <X aria-hidden="true" className="h-4 w-4" />
          </button>
          {index === times.length - 1 && (
            <button
              type="button"
              className="btn-icon text-accent"
              onClick={() => onChange([...times, "12:00:00"])}
              aria-label={t("subdefaults.add_time")}
            >
              <Plus aria-hidden="true" className="h-4 w-4" />
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

function toggleNumber(values: number[], value: number): number[] {
  const next = values.includes(value)
    ? values.filter((item) => item !== value)
    : [...values, value];
  return next.length ? next.sort((a, b) => a - b) : values;
}

export default function CalendarScheduleEditor({
  value,
  onChange,
}: {
  value?: CalendarScheduleRule | null;
  onChange: (rule: CalendarScheduleRule) => void;
}) {
  const t = useT();
  const rule = value || defaultCalendarRule();
  const setFrequency = (frequency: CalendarScheduleRule["frequency"]) => {
    const times = normalizedTimes(rule);
    if (frequency === "daily") onChange({ frequency, times });
    if (frequency === "weekly") onChange({ frequency, weekdays: [1, 2, 3, 4, 5], times });
    if (frequency === "monthly") onChange({ frequency, month_days: [1], overflow: "last_day", times });
  };
  const setTimes = (times: string[]) => onChange({ ...rule, times } as CalendarScheduleRule);

  return (
    <fieldset className="space-y-4">
      <legend className="sr-only">{t("subdefaults.calendar_rule")}</legend>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <span className="font-medium">{t("subdefaults.frequency")}</span>
          <p className="mt-1 text-xs text-muted">{t("subdefaults.frequency.desc")}</p>
        </div>
        <select
          value={rule.frequency}
          onChange={(event) => setFrequency(event.target.value as CalendarScheduleRule["frequency"])}
          className="select px-2 py-1"
          aria-label={t("subdefaults.frequency")}
        >
          <option value="daily">{t("subdefaults.daily")}</option>
          <option value="weekly">{t("subdefaults.weekly")}</option>
          <option value="monthly">{t("subdefaults.monthly")}</option>
        </select>
      </div>

      {rule.frequency === "weekly" && (
        <div>
          <span className="text-sm font-medium">{t("subdefaults.weekdays")}</span>
          <div className="mt-2 flex flex-wrap gap-2">
            {[1, 2, 3, 4, 5, 6, 7].map((weekday) => (
              <button
                key={weekday}
                type="button"
                aria-pressed={rule.weekdays.includes(weekday)}
                onClick={() => onChange({ ...rule, weekdays: toggleNumber(rule.weekdays, weekday) })}
                className={`min-h-11 min-w-11 rounded-md border px-3 text-xs ${rule.weekdays.includes(weekday) ? "border-accent bg-accent-subtle text-accent" : "border-border bg-surface text-muted"}`}
              >
                {t(`subdefaults.weekday.${weekday}`)}
              </button>
            ))}
          </div>
        </div>
      )}

      {rule.frequency === "monthly" && (
        <div>
          <span className="text-sm font-medium">{t("subdefaults.month_days")}</span>
          <p className="mt-1 text-xs text-muted">{t("subdefaults.month_days.desc")}</p>
          <div className="mt-2 grid grid-cols-7 gap-1 sm:grid-cols-10">
            {Array.from({ length: 31 }, (_, index) => index + 1).map((monthDay) => (
              <button
                key={monthDay}
                type="button"
                aria-pressed={rule.month_days.includes(monthDay)}
                onClick={() => onChange({ ...rule, month_days: toggleNumber(rule.month_days, monthDay), overflow: "last_day" })}
                className={`min-h-10 rounded-md border text-xs ${rule.month_days.includes(monthDay) ? "border-accent bg-accent-subtle text-accent" : "border-border bg-surface text-muted"}`}
              >
                {monthDay}
              </button>
            ))}
          </div>
        </div>
      )}

      <div>
        <span className="text-sm font-medium">{t("subdefaults.scheduled_times")}</span>
        <p className="mb-2 mt-1 text-xs text-muted">{t("subdefaults.scheduled_times.desc")}</p>
        <ScheduleTimePicker value={rule.times} onChange={setTimes} />
      </div>
    </fieldset>
  );
}
