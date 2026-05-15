"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys, Tag } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState } from "@/components";

export default function TagsPage() {
  const [search, setSearch] = useState("");
  const tags = useQuery({ queryKey: queryKeys.tags.all, queryFn: () => api.listTags() });

  const filtered = tags.data?.filter((t: Tag) => !search || t.normalized_name.toLowerCase().includes(search.toLowerCase()));

  return (
    <main className="max-w-6xl mx-auto p-6">
      <PageHeader title="Tags" description={`${tags.data?.length || 0} normalized tags`} />
      <div className="mb-4"><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Filter tags..." className="w-full max-w-md border rounded px-3 py-2 text-sm" /></div>

      {tags.isLoading && <div className="space-y-1">{Array.from({ length: 10 }).map((_, i) => <div key={i} className="h-8 bg-gray-100 rounded animate-pulse" />)}</div>}
      {tags.error && <ErrorState message={(tags.error as Error).message} />}
      {tags.data && !tags.data.length && <EmptyState title="No tags" description="Tags will be populated after works are imported and indexed." />}

      {filtered && filtered.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {filtered.map((t: Tag) => (
            <span key={t.id} className="inline-flex items-center gap-2 px-3 py-1.5 bg-white rounded-full shadow text-sm border">
              <span>{t.normalized_name}</span>
              {t.category && <span className="text-xs bg-gray-100 px-1.5 py-0.5 rounded">{t.category}</span>}
            </span>
          ))}
        </div>
      )}
    </main>
  );
}
