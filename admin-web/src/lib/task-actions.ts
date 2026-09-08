import type { components } from "./api/types.generated";

export type TaskAction = NonNullable<components["schemas"]["TaskRead"]["available_actions"]>[number];

export interface TaskActionCapabilities {
  available_actions?: readonly string[] | null;
  disabled_reasons?: Partial<Record<string, string>> | null;
}

export function hasTaskAction(capabilities: TaskActionCapabilities | null | undefined, action: TaskAction): boolean {
  return capabilities?.available_actions?.includes(action) === true;
}

export function actionReason(capabilities: TaskActionCapabilities | null | undefined, action: TaskAction): string | null {
  if (hasTaskAction(capabilities, action)) return null;
  return capabilities?.disabled_reasons?.[action] || "action_unavailable";
}

export function partitionTaskAction<T extends TaskActionCapabilities & { id: string }>(
  rows: readonly T[],
  selectedIds: ReadonlySet<string>,
  action: TaskAction,
): { eligible: T[]; ineligible: Array<{ row: T; reason: string }> } {
  const eligible: T[] = [];
  const ineligible: Array<{ row: T; reason: string }> = [];
  for (const row of rows) {
    if (!selectedIds.has(row.id)) continue;
    if (hasTaskAction(row, action)) eligible.push(row);
    else ineligible.push({ row, reason: actionReason(row, action) || "action_unavailable" });
  }
  return { eligible, ineligible };
}
