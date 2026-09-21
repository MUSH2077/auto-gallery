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

export type TaskActionFailureKind = "local_refusal" | "server_refusal" | "uncertain";
export interface TaskActionFailure { id: string; reason: string; kind: TaskActionFailureKind }
export interface TaskBulkSubmission {
  selectedIds: readonly string[];
  eligibleIds: readonly string[];
  localRefusals: readonly { id: string; reason: string }[];
}
interface TaskBulkResponseLike {
  succeeded?: number;
  failed?: number;
  errors?: readonly { id: string; error: unknown }[];
}
export interface TaskBulkReconciliation {
  confirmedSuccessIds: string[];
  retained: TaskActionFailure[];
}

export function actionErrorReason(error: unknown): string {
  if (typeof error === "string" && error.trim()) return error;
  if (!error || typeof error !== "object") return "action_failed";
  const detail = "detail" in error ? error.detail : error;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object") {
    if ("reason" in detail && typeof detail.reason === "string" && detail.reason.trim()) return detail.reason;
    if ("message" in detail && typeof detail.message === "string" && detail.message.trim()) return detail.message;
    if ("code" in detail && typeof detail.code === "string" && detail.code.trim()) return detail.code;
  }
  if ("message" in error && typeof error.message === "string" && error.message.trim()) return error.message;
  return "action_failed";
}

export function reconcileTaskBulkResult(submission: TaskBulkSubmission, response: TaskBulkResponseLike | null): TaskBulkReconciliation {
  const local = submission.localRefusals.map(({ id, reason }) => ({ id, reason, kind: "local_refusal" as const }));
  if (!response) {
    return { confirmedSuccessIds: [], retained: [
      ...submission.eligibleIds.map((id) => ({ id, reason: "outcome_unknown", kind: "uncertain" as const })),
      ...local,
    ] };
  }
  const errors = Array.isArray(response.errors) ? response.errors : [];
  const errorIds = new Set(errors.map((item) => item.id));
  const eligibleIds = new Set(submission.eligibleIds);
  const consistent = Number.isInteger(response.succeeded)
    && Number.isInteger(response.failed)
    && response.succeeded! >= 0
    && response.failed! >= 0
    && response.succeeded! + response.failed! === submission.eligibleIds.length
    && response.failed === errors.length
    && eligibleIds.size === submission.eligibleIds.length
    && errorIds.size === errors.length
    && errors.every((item) => eligibleIds.has(item.id));
  if (!consistent) {
    return { confirmedSuccessIds: [], retained: [
      ...submission.eligibleIds.map((id) => ({ id, reason: "response_unverified", kind: "uncertain" as const })),
      ...local,
    ] };
  }
  return {
    confirmedSuccessIds: submission.eligibleIds.filter((id) => !errorIds.has(id)),
    retained: [
      ...errors.map(({ id, error }) => ({ id, reason: actionErrorReason(error), kind: "server_refusal" as const })),
      ...local,
    ],
  };
}

export interface RepeatSyncIntent {
  version: 1;
  actorId: string | number;
  originalJobId: string;
  requestId: string;
  label: string;
  action: "repeat_sync";
}
export function repeatSyncStorageKey(actorId: string | number, originalJobId: string): string {
  return `auto-gallery-repeat-sync:${actorId}:${originalJobId}:repeat_sync`;
}
export function createRepeatSyncIntent(actorId: string | number, originalJobId: string, label: string, createRequestId: () => string): RepeatSyncIntent {
  return { version: 1, actorId, originalJobId, requestId: createRequestId(), label, action: "repeat_sync" };
}
interface IntentStorage {
  readonly length: number;
  key(index: number): string | null;
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}
function validRepeatIntent(value: unknown, actorId: string | number, originalJobId: string): value is RepeatSyncIntent {
  return !!value && typeof value === "object"
    && (value as RepeatSyncIntent).version === 1
    && String((value as RepeatSyncIntent).actorId) === String(actorId)
    && (value as RepeatSyncIntent).originalJobId === originalJobId
    && typeof (value as RepeatSyncIntent).requestId === "string" && Boolean((value as RepeatSyncIntent).requestId)
    && typeof (value as RepeatSyncIntent).label === "string"
    && (value as RepeatSyncIntent).action === "repeat_sync";
}
export function readRepeatSyncIntent(actorId: string | number, originalJobId: string, storage: IntentStorage = localStorage): RepeatSyncIntent | null {
  try {
    const value = JSON.parse(storage.getItem(repeatSyncStorageKey(actorId, originalJobId)) || "null") as unknown;
    return validRepeatIntent(value, actorId, originalJobId) ? value : null;
  } catch {
    return null;
  }
}
export function storeRepeatSyncIntent(intent: RepeatSyncIntent, storage: IntentStorage = localStorage): void {
  storage.setItem(repeatSyncStorageKey(intent.actorId, intent.originalJobId), JSON.stringify(intent));
}
export function clearRepeatSyncIntent(intent: RepeatSyncIntent, storage: IntentStorage = localStorage): void {
  storage.removeItem(repeatSyncStorageKey(intent.actorId, intent.originalJobId));
}
export function listRepeatSyncIntents(actorId: string | number, storage: IntentStorage = localStorage): RepeatSyncIntent[] {
  const prefix = `auto-gallery-repeat-sync:${actorId}:`;
  const results: RepeatSyncIntent[] = [];
  try {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (!key?.startsWith(prefix) || !key.endsWith(":repeat_sync")) continue;
      const originalJobId = key.slice(prefix.length, -":repeat_sync".length);
      const intent = readRepeatSyncIntent(actorId, originalJobId, storage);
      if (intent) results.push(intent);
    }
  } catch {}
  return results.sort((left, right) => left.originalJobId.localeCompare(right.originalJobId));
}

export type RepeatSyncConflict =
  | { kind: "existing"; existingJobId: string; reason: string }
  | { kind: "identity"; reason: string }
  | { kind: "other"; reason: string };
export function repeatSyncConflict(error: unknown): RepeatSyncConflict {
  const detail = error && typeof error === "object" && "detail" in error && error.detail && typeof error.detail === "object"
    ? error.detail as Record<string, unknown>
    : null;
  const reason = actionErrorReason(error);
  if (detail?.code === "repeat_sync_not_admitted" && typeof detail.existing_job_id === "string" && detail.existing_job_id) {
    return { kind: "existing", existingJobId: detail.existing_job_id, reason };
  }
  if (detail?.code === "request_identity_conflict") return { kind: "identity", reason };
  return { kind: "other", reason };
}
export function validateRepeatSyncAcceptance<T extends Record<string, unknown>>(intent: RepeatSyncIntent, accepted: T): T {
  if (accepted.previous_job_id !== intent.originalJobId
    || accepted.request_id !== intent.requestId
    || accepted.action !== "repeat_sync"
    || accepted.status !== "enqueued"
    || typeof accepted.task_id !== "string" || !accepted.task_id
    || typeof accepted.job_id !== "string" || !accepted.job_id) throw new Error("repeat_identity_invalid");
  return accepted;
}

export function parseOptionalFiniteNumber(value: string, fallback: number | undefined): number | undefined {
  if (value.trim() === "") return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export interface MetadataCleanupPresentation {
  kind: "success" | "partial";
  removed: number;
  scanned: number;
  skipped: number;
  failed: number;
  reasons: Record<string, number>;
  errors: string[];
}

export function metadataCleanupPresentation(task: {
  operation_type?: unknown;
  status?: unknown;
  reason_code?: unknown;
  result_data?: unknown;
}): MetadataCleanupPresentation | null {
  if (task.operation_type !== "admin-cleanup-metadata-jsons" || !["complete", "failed"].includes(String(task.status))) return null;
  if (!task.result_data || typeof task.result_data !== "object") return null;
  const result = task.result_data as Record<string, unknown>;
  const count = (key: string) => typeof result[key] === "number" && Number.isFinite(result[key]) && result[key] >= 0 ? result[key] as number : 0;
  const rawReasons = result.reasons && typeof result.reasons === "object" && !Array.isArray(result.reasons)
    ? result.reasons as Record<string, unknown>
    : {};
  const reasons = Object.fromEntries(Object.entries(rawReasons).filter((entry): entry is [string, number] => typeof entry[1] === "number" && Number.isFinite(entry[1])));
  const errors = Array.isArray(result.errors) ? result.errors.map(actionErrorReason) : [];
  const partial = task.status === "failed" || task.reason_code === "metadata_cleanup_partial_failure" || result.status === "partial" || count("failed") > 0;
  if (!partial && task.status !== "complete") return null;
  return { kind: partial ? "partial" : "success", removed: count("removed"), scanned: count("scanned"), skipped: count("skipped"), failed: count("failed"), reasons, errors };
}
