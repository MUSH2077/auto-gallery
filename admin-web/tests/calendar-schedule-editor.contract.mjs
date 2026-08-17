import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const component = readFileSync(new URL("../src/components/CalendarScheduleEditor.tsx", import.meta.url), "utf8");
const defaults = readFileSync(new URL("../src/app/admin/settings/scheduler-defaults/page.tsx", import.meta.url), "utf8");
const subscription = readFileSync(new URL("../src/app/admin/subscriptions/[id]/page.tsx", import.meta.url), "utf8");

for (const token of ["frequency", "weekdays", "month_days", "ScheduleTimePicker"]) {
  assert.ok(component.includes(token), `calendar editor must expose ${token}`);
}
assert.ok(defaults.includes("<CalendarScheduleEditor"));
assert.ok(subscription.includes("<CalendarScheduleEditor"));
assert.ok(!defaults.includes('option value="fixed_time"'));
assert.ok(!subscription.includes('option value="fixed_time"'));
