"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, EmptyState } from "@/components";

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const results = useQuery({
    queryKey: ["search", submitted],
    queryFn: () => api.search(submitted),
    enabled: !!submitted,
  });

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title="Search" description="Full-text search across works, creators, and tags" />
      <form onSubmit={(e) => { e.preventDefault(); setSubmitted(query); }} className="flex gap-2 mb-6">
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search works, creators, tags..." className="flex-1 border rounded px-4 py-2 text-sm" />
        <button type="submit" className="px-6 py-2 bg-slate-900 dark:bg-slate-700 text-white rounded text-sm hover:bg-slate-800 dark:hover:bg-slate-600">Search</button>
      </form>

      {results.isLoading && <div className="space-y-2">{Array.from({ length: 3 }).map((_, i) => <div key={i} className="h-16 bg-gray-100 dark:bg-slate-700 rounded animate-pulse" />)}</div>}
      {results.data && !results.data.total && <EmptyState title="No results" description={`No results found for "${submitted}"`} />}
      {results.data && results.data.total > 0 && (
        <div>
          <p className="text-sm text-gray-500 dark:text-gray-400 mb-3">{results.data.total} result(s) for &quot;{submitted}&quot;</p>
          <div className="space-y-2">
            {results.data.results.map((r: any, i: number) => (
              <div key={i} className="bg-white dark:bg-slate-800 rounded-lg shadow p-4 text-sm">
                <pre className="text-xs whitespace-pre-wrap">{JSON.stringify(r, null, 2)}</pre>
              </div>
            ))}
          </div>
        </div>
      )}
    </main>
  );
}
