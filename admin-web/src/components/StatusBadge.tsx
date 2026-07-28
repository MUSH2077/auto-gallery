type Status = "up" | "down" | "running" | "pending" | "failed" | "completed" | "stale" | "unknown";

const colors: Record<Status, string> = {
  up: "bg-[#dafbe1] text-[#1a7f37] border-[#1a7f37]/30 dark:bg-[#2ea04326] dark:text-[#3fb950] dark:border-[#3fb950]/30",
  down: "bg-[#ffebe9] text-[#cf222e] border-[#cf222e]/30 dark:bg-[#f8514926] dark:text-[#f85149] dark:border-[#f85149]/30",
  running: "bg-[#ddf4ff] text-[#0969da] border-[#0969da]/30 dark:bg-[#1f6feb26] dark:text-[#58a6ff] dark:border-[#58a6ff]/30",
  pending: "bg-[#fff8c5] text-[#9a6700] border-[#bf8700]/30 dark:bg-[#bb800926] dark:text-[#d29922] dark:border-[#d29922]/30",
  failed: "bg-[#ffebe9] text-[#cf222e] border-[#cf222e]/30 dark:bg-[#f8514926] dark:text-[#f85149] dark:border-[#f85149]/30",
  completed: "bg-[#dafbe1] text-[#1a7f37] border-[#1a7f37]/30 dark:bg-[#2ea04326] dark:text-[#3fb950] dark:border-[#3fb950]/30",
  stale: "bg-[#eaeef2] text-[#57606a] border-[#8c959f]/30 dark:bg-[#30363d] dark:text-[#8b949e] dark:border-[#8b949e]/30",
  unknown: "bg-[#eaeef2] text-[#57606a] border-[#8c959f]/30 dark:bg-[#30363d] dark:text-[#8b949e] dark:border-[#8b949e]/30",
};

export default function StatusBadge({ status }: { status: string }) {
  const s = (status || "unknown").toLowerCase() as Status;
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border ${colors[s] || colors.unknown}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${s === "running" ? "animate-pulse bg-current" : "bg-current"}`} />
      {status}
    </span>
  );
}
