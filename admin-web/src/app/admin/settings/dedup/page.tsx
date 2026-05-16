"use client";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, ErrorState } from "@/components";
import Link from "next/link";

export default function DedupSettingsPage() {
  const settings = useQuery({ queryKey: queryKeys.admin.settings, queryFn: api.getAdminSettings });

  if (settings.isError) {
    return (
      <main className="max-w-4xl mx-auto p-6">
        <ErrorState message={settings.error?.message || "Failed"} onRetry={() => settings.refetch()} />
      </main>
    );
  }

  if (!settings.data) {
    return (
      <main className="max-w-4xl mx-auto p-6">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-gray-200 rounded w-1/3" />
          <div className="h-32 bg-gray-200 rounded" />
        </div>
      </main>
    );
  }

  return (
    <main className="max-w-4xl mx-auto p-6">
      <div className="flex items-center gap-4 mb-6">
        <Link href="/admin/settings" className="text-sm text-blue-600 hover:underline">&larr; Settings</Link>
      </div>
      <PageHeader title="Deduplication Settings" description="Control duplicate detection and merge behavior." />

      <div className="bg-white rounded-lg shadow p-6 text-sm space-y-4">
        {Object.entries(settings.data.dedup || {}).map(([key, value]) => (
          <div key={key} className="flex items-center justify-between py-3 border-b last:border-0">
            <div>
              <span className="font-medium">{key}</span>
              <p className="text-xs text-gray-500 mt-1">{
                key === "source_level_enabled" ? "Same source + same ID = skip download" :
                key === "cross_source_enabled" ? "SHA-256 match across sources = reuse asset record" :
                key === "auto_merge" ? "Automatically merge visually similar works. DANGEROUS." :
                key === "phash_threshold" ? "Perceptual hash threshold (0-64). Lower values = stricter matching." : ""
              }</p>
            </div>
            <span className={`px-3 py-1 rounded text-xs font-mono ${
              typeof value === "boolean"
                ? (value ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500")
                : "bg-blue-100 text-blue-700"
            }`}>{String(value)}</span>
          </div>
        ))}

        <div className="mt-6 p-4 bg-yellow-50 border border-yellow-200 rounded-lg text-sm text-yellow-800">
          <strong>All deduplication is OFF by default.</strong> Auto-merge may irreversibly modify your library.
          Enable dedup settings only after reviewing the risk documentation.
        </div>

        <div className="p-4 bg-blue-50 border border-blue-200 rounded-lg text-sm text-blue-800">
          <strong>Phase 5 limitation:</strong> Dedup settings are editable via API (<code className="text-xs bg-blue-100 px-1 rounded">PUT /api/v1/admin/settings</code>).
          A UI toggle will be added in a future phase.
        </div>
      </div>
    </main>
  );
}
