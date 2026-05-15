"use client";
import { useState } from "react";
import { PageHeader, EmptyState } from "@/components";

export default function DanbooruReferencePage() {
  const [artistTag, setArtistTag] = useState("");
  const [result, setResult] = useState<string | null>(null);

  const handlePreview = () => {
    if (!artistTag.trim()) return;
    setResult(`Danbooru reference lookup for "${artistTag}" — backend API not yet implemented. This will query Danbooru artist tags to extract related URLs and suggest creator identity mappings.`);
  };

  return (
    <main className="max-w-4xl mx-auto p-6">
      <PageHeader title="Danbooru Reference Mapping" description="Import Danbooru artist reference data for creator identity mapping" />

      <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4 text-sm mb-6">
        <p className="font-medium text-yellow-800 mb-1">Danbooru is a reference provider only.</p>
        <p className="text-yellow-700">Danbooru artist tag data is used to enrich creator identity mapping, suggest source account links, and extract related URLs. Danbooru is not treated as a complete union of all works and is not a default media download source.</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="bg-white rounded-lg shadow p-4">
          <h2 className="font-medium mb-3">Look up Artist Tag</h2>
          <div className="space-y-3">
            <div>
              <label className="block text-sm font-medium mb-1">Danbooru Artist Tag</label>
              <input value={artistTag} onChange={(e) => setArtistTag(e.target.value)}
                placeholder="e.g. artist_name"
                className="w-full border rounded px-3 py-2 text-sm" />
              <p className="text-xs text-gray-400 mt-1">Enter a Danbooru artist tag name to preview reference data</p>
            </div>
            <button onClick={handlePreview} disabled={!artistTag.trim()}
              className="px-4 py-2 bg-slate-900 text-white rounded text-sm hover:bg-slate-800 disabled:opacity-50 w-full">
              Preview Artist Data
            </button>
          </div>
          {result && (
            <div className="mt-4 p-3 bg-gray-50 rounded-lg text-sm text-gray-700">{result}</div>
          )}
        </div>

        <div className="bg-white rounded-lg shadow p-4">
          <h2 className="font-medium mb-3">Reference Data</h2>
          <EmptyState
            icon=" "
            title="No reference data loaded"
            description="Look up a Danbooru artist tag to preview reference data. The backend API will extract related URLs and suggest creator links for identity mapping."
          />
        </div>
      </div>

      <div className="bg-white rounded-lg shadow p-4 mt-6">
        <h2 className="font-medium mb-3">How Danbooru Reference Works</h2>
        <ol className="text-sm text-gray-600 space-y-2 list-decimal list-inside">
          <li>Admin enters a Danbooru artist tag (e.g., &quot;artist_name&quot;)</li>
          <li>Backend queries Danbooru API for artist reference data</li>
          <li>Related URLs (Pixiv, Twitter, Iwara, etc.) are extracted</li>
          <li>Suggested creator links are created with confidence scores</li>
          <li>Admin reviews suggestions on the Creator Mapping page</li>
          <li>Approved links become verified identity mappings</li>
        </ol>
        <p className="text-xs text-gray-400 mt-3">Note: Backend Danbooru reference API is not yet implemented. This page is a ready placeholder for the Phase 5 backend feature.</p>
      </div>
    </main>
  );
}
