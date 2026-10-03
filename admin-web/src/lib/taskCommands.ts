import { api, queryKeys, type TaskRun, type RepeatSyncAccepted } from "./api";
import type { QueryClient } from "@tanstack/react-query";
import { clearRepeatSyncIntent, createRepeatSyncIntent, readRepeatSyncIntent, storeRepeatSyncIntent, validateRepeatSyncAcceptance, type TaskAction } from "./task-actions";
import { secureRandomUuid } from "./random";

export type CommandTask = Pick<TaskRun, "id"> & Partial<Pick<TaskRun, "kind" | "operation_type" | "subject_type" | "subject_id" | "title" | "meta">>;
export type CommandSurface = "task" | "download" | "import";
export interface CommandContext {
  userId?: string | number | null;
  originalUnavailable: string;
  accepted?: (task: CommandTask, accepted: { task_id: string; status: string }) => void;
}

export function executeTaskCommand(task: CommandTask, action: "repeat_sync", context: CommandContext, surface?: CommandSurface): Promise<RepeatSyncAccepted>;
export function executeTaskCommand(task: CommandTask, action: TaskAction, context: CommandContext, surface?: CommandSurface): Promise<unknown>;
export async function executeTaskCommand(task: CommandTask, action: TaskAction, context: CommandContext, surface: CommandSurface = "task") {
  if (action === "repeat_sync") {
    const original = surface === "download" ? task.id : task.subject_type === "download_job" ? task.subject_id : null;
    if (!context.userId || !original) throw new Error(context.originalUnavailable);
    const intent = readRepeatSyncIntent(context.userId, original) || createRepeatSyncIntent(context.userId, original, task.title || original, secureRandomUuid);
    storeRepeatSyncIntent(intent);
    const result = validateRepeatSyncAcceptance(intent, await (surface === "download"
      ? api.repeatDownloadJob(task.id, intent.requestId) : api.repeatTask(task.id, intent.requestId)));
    clearRepeatSyncIntent(intent);
    return result;
  }
  if (surface === "task") {
    if (action === "retry") {
      const accepted = await api.retryTask(task.id);
      context.accepted?.(task, accepted);
      return accepted;
    }
    const commands = { pause: api.pauseTask, resume: api.resumeTask, cancel: api.cancelTask, delete: api.deleteTask, acknowledge: api.acknowledgeTask };
    return commands[action](task.id);
  }
  const commands: Partial<Record<TaskAction, (id: string) => Promise<unknown>>> = surface === "download"
    ? { retry: api.retryDownloadJob, pause: api.pauseDownloadJob, resume: api.resumeDownloadJob, cancel: api.cancelDownloadJob, delete: api.deleteDownloadJob }
    : { retry: api.retryImportJob, cancel: api.cancelImportJob, delete: api.deleteImportJob };
  const command = commands[action];
  if (!command) throw new Error("Action requires a TaskRun");
  return command(task.id);
}

export async function executeTaskBatch(tasks: CommandTask[], action: TaskAction, context: CommandContext, surface: CommandSurface = "task") {
  const ids = tasks.map(task => task.id);
  if (surface === "download") return api.batchDownloadJobs(ids, action);
  if (surface === "import") return api.batchImportJobsByFilter({ ids }, action);
  const settled = await Promise.allSettled(tasks.map(task => executeTaskCommand(task, action, context)));
  return {
    succeeded: settled.filter(item => item.status === "fulfilled").length,
    failed: settled.filter(item => item.status === "rejected").length,
    errors: settled.flatMap((item, index) => item.status === "rejected" ? [{ id: ids[index], error: item.reason }] : []),
  };
}

export function invalidateTaskCommandQueries(client: QueryClient) {
  for (const key of [queryKeys.tasks.all, queryKeys.downloadJobs.all, queryKeys.importJobs.all, queryKeys.workbench]) {
    void client.invalidateQueries({ queryKey: key });
  }
}
