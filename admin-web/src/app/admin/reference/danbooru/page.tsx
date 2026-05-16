"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, queryKeys } from "@/lib/api";
import { PageHeader, EmptyState, ErrorState, SourceBadge } from "@/components";

interface ArtistUrls { url: string; normalized_url: string; is_active: boolean }
interface ArtistResult {
  id: number; name: string; other_names: string[]; post_count?: number;
  notes?: string; is_active?: boolean; created_at?: string; urls: ArtistUrls[];
}
interface SuggestedLink { url: string; link_type: string; source: string; confidence: number; is_verified: boolean; notes?: string }

function PreviewResult({ artist, links, onImport, importPending, selectedCreator, setSelectedCreator }: {
  artist: ArtistResult; links: SuggestedLink[];
  onImport: (creatorId: string) => void; importPending: boolean;
  selectedCreator: string; setSelectedCreator: (v: string) => void;
}) {
  const creators = useQuery({ queryKey: queryKeys.creators.all, queryFn: () => api.listCreators() });

  return (
    <div className="mt-4 space-y-4">
      <div className="bg-white border rounded-lg p-4">
        <h3 className="font-medium mb-2">Artist #{artist.id}: {artist.name}</h3>
        {artist.other_names.length > 0 && (
          <p className="text-xs text-gray-500 mb-1">Also known as: {artist.other_names.join(", ")}</p>
        )}
        {artist.post_count != null && <p className="text-xs text-gray-500">Danbooru posts: {artist.post_count}</p>}
        {artist.notes && <p className="text-xs text-gray-600 mt-2 bg-gray-50 p-2 rounded">{artist.notes}</p>}

        {artist.urls.length > 0 && (
          <div className="mt-3">
            <h4 className="text-xs font-medium text-gray-500 mb-1">Associated URLs ({artist.urls.length})</h4>
            <div className="space-y-1">
              {artist.urls.map((u, i) => (
                <div key={i} className="flex items-center gap-2 text-xs">
                  {!u.is_active && <span className="text-yellow-500">[inactive]</span>}
                  <a href={u.normalized_url} target="_blank" rel="noopener noreferrer"
                    className="text-blue-600 hover:underline truncate max-w-lg">{u.normalized_url}</a>
                </div>
              ))}
            </div>
          </div>
        )}
        {artist.urls.length === 0 && (
          <p className="text-xs text-gray-400 mt-3">No associated source URLs in Danbooru. A Danbooru artist reference link will still be created.</p>
        )}
      </div>

      {links.length > 0 && (
        <div className="bg-white border rounded-lg p-4">
          <h3 className="font-medium mb-2">Suggested Links ({links.length})</h3>
          <div className="space-y-2 mb-4">
            {links.map((l, i) => (
              <div key={i} className="flex items-center gap-2 text-xs border-b pb-2">
                <SourceBadge source={l.link_type} />
                <a href={l.url} target="_blank" rel="noopener noreferrer"
                  className="text-blue-600 hover:underline truncate max-w-md">{l.url}</a>
                <span className="text-gray-400">confidence: {l.confidence.toFixed(1)}</span>
                {l.notes && <span className="text-gray-400 truncate max-w-[200px]">— {l.notes}</span>}
              </div>
            ))}
          </div>

          <div className="flex items-end gap-3">
            <div className="flex-1">
              <label className="block text-xs font-medium mb-1">Import to Creator</label>
              <select value={selectedCreator} onChange={(e) => setSelectedCreator(e.target.value)}
                className="w-full border rounded px-3 py-2 text-sm">
                <option value="">Select creator...</option>
                {creators.data?.map((c) => <option key={c.id} value={c.id}>{c.display_name || c.name}</option>)}
              </select>
            </div>
            <button onClick={() => onImport(selectedCreator)} disabled={!selectedCreator || importPending}
              className="px-4 py-2 text-sm bg-slate-900 text-white rounded hover:bg-slate-800 disabled:opacity-50 shrink-0">
              {importPending ? "Importing..." : `Import ${links.length} Links`}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function DanbooruReferencePage() {
  const [searchUrl, setSearchUrl] = useState("");
  const [searchName, setSearchName] = useState("");
  const [searchPixivId, setSearchPixivId] = useState("");
  const [selectedCreator, setSelectedCreator] = useState("");
  const [searchParams, setSearchParams] = useState<{ url?: string; pixiv_id?: string; name?: string } | null>(null);

  const preview = useQuery({
    queryKey: ["danbooru-preview", searchParams],
    queryFn: () => api.previewDanbooruArtist(searchParams!),
    enabled: !!searchParams,
  });

  const qc = useQueryClient();
  const importMutation = useMutation({
    mutationFn: (creatorId: string) => api.importDanbooruArtist({ creator_id: creatorId, ...searchParams }),
    onSuccess: (data) => {
      alert(`Imported ${data.imported} links from Danbooru artist "${data.artist_name}"`);
      qc.invalidateQueries({ queryKey: queryKeys.creators.all });
    },
  });

  const handleSearch = (type: "url" | "name" | "pixiv_id") => {
    const params: Record<string, string> = {};
    if (type === "url" && searchUrl.trim()) params.url = searchUrl.trim();
    if (type === "name" && searchName.trim()) params.name = searchName.trim();
    if (type === "pixiv_id" && searchPixivId.trim()) params.pixiv_id = searchPixivId.trim();
    if (Object.keys(params).length === 0) return;
    setSearchParams(params);
  };

  const artist = preview.data?.artist;
  const links = preview.data?.suggested_links || [];

  return (
    <main className="max-w-5xl mx-auto p-6">
      <PageHeader title="Danbooru Reference Mapping" description="Import Danbooru artist reference data for creator identity mapping" />

      <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4 text-sm mb-6">
        <p className="font-medium text-yellow-800 mb-1">Danbooru is a reference provider only.</p>
        <p className="text-yellow-700">Danbooru artist tag data is used to enrich creator identity mapping, suggest source account links, and extract related URLs.</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
        <div className="bg-white rounded-lg shadow p-4">
          <h3 className="font-medium mb-3 text-sm">Search by Pixiv URL</h3>
          <input value={searchUrl} onChange={(e) => setSearchUrl(e.target.value)}
            placeholder="https://www.pixiv.net/en/users/1980643"
            className="w-full border rounded px-3 py-2 text-sm mb-2" />
          <button onClick={() => handleSearch("url")} disabled={!searchUrl.trim()}
            className="w-full px-3 py-2 bg-slate-900 text-white rounded text-sm hover:bg-slate-800 disabled:opacity-50">
            Search
          </button>
        </div>

        <div className="bg-white rounded-lg shadow p-4">
          <h3 className="font-medium mb-3 text-sm">Search by Pixiv User ID</h3>
          <input value={searchPixivId} onChange={(e) => setSearchPixivId(e.target.value)}
            placeholder="1980643"
            className="w-full border rounded px-3 py-2 text-sm mb-2" />
          <button onClick={() => handleSearch("pixiv_id")} disabled={!searchPixivId.trim()}
            className="w-full px-3 py-2 bg-slate-900 text-white rounded text-sm hover:bg-slate-800 disabled:opacity-50">
            Search
          </button>
        </div>

        <div className="bg-white rounded-lg shadow p-4">
          <h3 className="font-medium mb-3 text-sm">Search by Artist Name</h3>
          <input value={searchName} onChange={(e) => setSearchName(e.target.value)}
            placeholder="artist_name"
            className="w-full border rounded px-3 py-2 text-sm mb-2" />
          <button onClick={() => handleSearch("name")} disabled={!searchName.trim()}
            className="w-full px-3 py-2 bg-slate-900 text-white rounded text-sm hover:bg-slate-800 disabled:opacity-50">
            Search
          </button>
        </div>
      </div>

      {preview.isLoading && <div className="bg-white rounded-lg shadow p-4 animate-pulse"><div className="h-24 bg-gray-100 rounded" /></div>}
      {preview.error && <ErrorState message={(preview.error as Error).message} />}
      {preview.data && !preview.data.found && (
        <div className="bg-white rounded-lg shadow p-4">
          <EmptyState title="No match found" description={preview.data.message || "No matching Danbooru artist found."} />
        </div>
      )}

      {artist && (
        <PreviewResult artist={artist} links={links}
          onImport={(creatorId) => importMutation.mutate(creatorId)}
          importPending={importMutation.isPending}
          selectedCreator={selectedCreator} setSelectedCreator={setSelectedCreator} />
      )}

      <div className="bg-white rounded-lg shadow p-4 mt-6">
        <h3 className="font-medium mb-3">How Danbooru Reference Works</h3>
        <ol className="text-sm text-gray-600 space-y-2 list-decimal list-inside">
          <li>Enter a Pixiv profile URL, user ID, or artist name above</li>
          <li>Backend queries the Danbooru API for matching artist records</li>
          <li>Related URLs are extracted and suggested as creator identity links</li>
          <li>Import the links to a creator — they appear as low-confidence suggestions on the mapping page</li>
          <li>Admin reviews and approves/rejects links via the creator mapping page</li>
          <li>When creating a subscription, Danbooru enrichment runs automatically</li>
        </ol>
      </div>
    </main>
  );
}
