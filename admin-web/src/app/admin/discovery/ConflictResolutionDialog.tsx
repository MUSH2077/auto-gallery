"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, Search } from "lucide-react";

import { Modal } from "@/components";
import { api, queryKeys, type DiscoveryCandidate } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useDebounce } from "@/lib/useDebounce";

export type ConflictResolutionValue = {
  creatorId?: string;
  creatorName?: string;
  syncNow: boolean;
};

export default function ConflictResolutionDialog({
  candidate,
  open,
  pending,
  error,
  onClose,
  onResolve,
}: {
  candidate: DiscoveryCandidate | null;
  open: boolean;
  pending: boolean;
  error?: string | null;
  onClose: () => void;
  onResolve: (value: ConflictResolutionValue) => void;
}) {
  const t = useT();
  const [mode, setMode] = useState<"existing" | "new">("existing");
  const [query, setQuery] = useState("");
  const [creatorId, setCreatorId] = useState<string | null>(null);
  const [creatorName, setCreatorName] = useState("");
  const [syncNow, setSyncNow] = useState(false);
  const debouncedQuery = useDebounce(query, 250);
  const creators = useQuery({
    queryKey: [...queryKeys.creators.all, "discovery-lookup", debouncedQuery],
    queryFn: () => api.listCreators(0, 20, debouncedQuery),
    enabled: open && mode === "existing",
    placeholderData: (previous) => previous,
  });

  useEffect(() => {
    if (!open) return;
    setMode("existing");
    setQuery("");
    setCreatorId(null);
    setCreatorName(candidate?.display_name || "");
    setSyncNow(false);
  }, [candidate?.id, candidate?.display_name, open]);

  if (!candidate) return null;
  const displayName = candidate.display_name || candidate.source_creator_id;
  const canSubmit = mode === "existing" ? !!creatorId : !!creatorName.trim();

  return (
    <Modal open={open} onClose={onClose} title={t("discovery.resolve_title")}>
      <div className="space-y-4">
        <p className="text-sm leading-5 text-muted">{t("discovery.resolve_desc", { name: displayName })}</p>
        <fieldset className="grid gap-2 sm:grid-cols-2">
          <label className={`flex min-h-11 cursor-pointer items-center gap-2 rounded-md border px-3 text-sm font-medium ${mode === "existing" ? "border-accent bg-accent-subtle text-accent" : "border-border text-fg"}`}>
            <input type="radio" name="resolution-mode" checked={mode === "existing"} onChange={() => setMode("existing")} />
            {t("discovery.attach_existing")}
          </label>
          <label className={`flex min-h-11 cursor-pointer items-center gap-2 rounded-md border px-3 text-sm font-medium ${mode === "new" ? "border-accent bg-accent-subtle text-accent" : "border-border text-fg"}`}>
            <input type="radio" name="resolution-mode" checked={mode === "new"} onChange={() => setMode("new")} />
            {t("discovery.create_new")}
          </label>
        </fieldset>

        {mode === "existing" ? (
          <div>
            <label htmlFor="discovery-creator-lookup" className="mb-1.5 block text-sm font-medium text-fg">{t("discovery.creator_lookup")}</label>
            <div className="relative">
              <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <input
                id="discovery-creator-lookup"
                className="input w-full pl-9"
                value={query}
                placeholder={t("discovery.creator_lookup_placeholder")}
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
            <div className="mt-2 max-h-52 space-y-1 overflow-y-auto rounded-md border border-border p-1">
              {(creators.data?.items || []).map((creator) => {
                const name = creator.display_name || creator.name;
                const selected = creatorId === creator.id;
                return (
                  <button
                    key={creator.id}
                    type="button"
                    aria-label={t("discovery.choose_creator", { name })}
                    aria-pressed={selected}
                    onClick={() => setCreatorId(creator.id)}
                    className={`flex min-h-11 w-full items-center justify-between rounded-md px-3 text-left text-sm ${selected ? "bg-accent-subtle font-medium text-accent" : "text-fg hover:bg-subtle"}`}
                  >
                    <span className="truncate">{name}</span>
                    {selected ? <Check aria-hidden="true" className="h-4 w-4 shrink-0" /> : null}
                  </button>
                );
              })}
              {!creators.isLoading && (creators.data?.items.length || 0) === 0 ? (
                <p className="px-3 py-4 text-center text-sm text-muted">{t("discovery.no_creators")}</p>
              ) : null}
            </div>
          </div>
        ) : (
          <label className="block text-sm font-medium text-fg">
            <span className="mb-1.5 block">{t("discovery.new_creator_name")}</span>
            <input className="input w-full" value={creatorName} onChange={(event) => setCreatorName(event.target.value)} />
          </label>
        )}

        <label className="flex min-h-11 cursor-pointer items-center gap-2 rounded-md border border-border px-3 text-sm text-fg">
          <input type="checkbox" className="rounded" checked={syncNow} onChange={(event) => setSyncNow(event.target.checked)} />
          {t("discovery.sync_immediately")}
        </label>
        {error ? <p role="alert" className="rounded-md border border-danger/30 bg-danger-subtle p-3 text-sm text-danger">{error}</p> : null}
        <div className="flex flex-wrap justify-end gap-2">
          <button type="button" className="btn-ghost" onClick={onClose}>{t("common.cancel")}</button>
          <button
            type="button"
            className="btn-primary"
            disabled={!canSubmit || pending}
            onClick={() => onResolve(mode === "existing"
              ? { creatorId: creatorId || undefined, syncNow }
              : { creatorName: creatorName.trim(), syncNow })}
          >
            {t("discovery.resolve_conflict")}
          </button>
        </div>
      </div>
    </Modal>
  );
}
