"use client";

interface PipelineStage {
  name: string;
  status: "pending" | "active" | "complete" | "failed";
}

const STAGE_LABELS: Record<string, string> = {
  enqueued: "Queued",
  configuring: "Config",
  downloading: "Download",
  post_download: "Verify",
  enqueuing_import: "Import Q",
  importing: "Import",
  import_indexing: "Index",
  complete: "Done",
};

export function PipelineVisualizer({ stages }: { stages: PipelineStage[] }) {
  if (!stages || stages.length === 0) return null;

  return (
    <div className="flex items-center gap-0.5">
      {stages.map((stage, i) => {
        const color =
          stage.status === "active"
            ? "bg-blue-500"
            : stage.status === "complete"
              ? "bg-green-500"
              : stage.status === "failed"
                ? "bg-red-500"
                : "bg-gray-300 dark:bg-slate-600";

        const size =
          stage.status === "active" ? "w-2.5 h-2.5" : "w-2 h-2";

        return (
          <div key={stage.name} className="flex items-center gap-0.5">
            <div
              className={`${color} ${size} rounded-full shrink-0`}
              title={`${STAGE_LABELS[stage.name] ?? stage.name}: ${stage.status}`}
            />
            {i < stages.length - 1 && (
              <div
                className={`w-2 h-0.5 ${
                  stage.status === "complete"
                    ? "bg-green-500"
                    : "bg-gray-300 dark:bg-slate-600"
                }`}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
