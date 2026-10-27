"use client";
import { useContext } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useT } from "./i18n";
import { OperationMonitorContext } from "./operationMonitor";
import { executeTaskBatch, executeTaskCommand, invalidateTaskCommandQueries, type CommandTask, type CommandSurface } from "./taskCommands";
import type { TaskAction } from "./task-actions";
import { api } from "./api";

export function useTaskCommands(userId?: string | number | null) {
  const client = useQueryClient();
  const monitor = useContext(OperationMonitorContext);
  const t = useT();
  const context = { userId, originalUnavailable: t("jobs.repeat_original_unavailable"), accepted: monitor?.accept };
  return {
    execute: (task: CommandTask, action: TaskAction, surface?: CommandSurface) => executeTaskCommand(task, action, context, surface),
    repeat: (task: CommandTask, surface?: CommandSurface) => executeTaskCommand(task, "repeat_sync", context, surface),
    batch: (tasks: CommandTask[], action: TaskAction, surface?: CommandSurface) => executeTaskBatch(tasks, action, context, surface),
    byFilter: async (filters: Record<string, string>, action: string, note?: string) => {
      try { return await api.batchDownloadJobsByFilter(filters, action, note); }
      finally { invalidateTaskCommandQueries(client); }
    },
    invalidate: () => invalidateTaskCommandQueries(client),
  };
}
