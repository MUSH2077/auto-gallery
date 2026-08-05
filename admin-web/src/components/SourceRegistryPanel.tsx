"use client";

import { useId, useState } from "react";

import ErrorState from "@/components/ErrorState";
import EmptyState from "@/components/EmptyState";
import type { ProviderInfo } from "@/lib/api";
import { useT } from "@/lib/i18n";
import { useStaggeredEntrance } from "@/lib/motion";
import { getSourceColor } from "@/lib/sourceColors";

const DEFAULT_URLS: Record<string, string> = {
  pixiv: "https://www.pixiv.net/artworks/12345678",
  iwara: "https://www.iwara.tv/video/abc123",
  x: "https://x.com/artist_handle/status/1234567890123456789",
  danbooru: "https://danbooru.donmai.us/posts?tags=ask",
  pinterest: "https://www.pinterest.com/username/pins/",
  lofter: "https://blogname.lofter.com/",
  danbooru_reference: "https://danbooru.donmai.us/artists/12345",
  local: "/path/to/local/folder",
  weibo: "https://weibo.com/u/1234567890",
  bilibili: "https://space.bilibili.com/123456",
};

const SOURCE_DESCRIPTION_KEYS: Record<string, string> = {
  pixiv: "sources.description_pixiv",
  iwara: "sources.description_iwara",
  x: "sources.description_x",
  danbooru: "sources.description_danbooru",
  danbooru_reference: "sources.description_danbooru_reference",
  local: "sources.description_local",
  manual: "sources.description_manual",
  pinterest: "sources.description_pinterest",
  lofter: "sources.description_lofter",
  weibo: "sources.description_weibo",
  bilibili: "sources.description_bilibili",
};

const URL_PATTERNS: Record<string, RegExp> = {
  pixiv: /pixiv\.net\/(?:en\/)?(artworks|users)\/\d+/,
  iwara: /iwara\.tv\/(video|profile)\/[\w-]+/,
  x: /(?:twitter\.com|x\.com)\/\w+(?:\/status\/\d+)?\/?$/,
  danbooru: /danbooru\.donmai\.us\/posts\?tags=.+/,
  danbooru_reference: /danbooru\.donmai\.us\/(artists\/\d+|posts\?tags=.+)/,
  pinterest: /pinterest\.\w+\/(pin\/\d+|[\w.-]+\/(pins|[\w.-]+))/,
  lofter: /[\w-]+\.lofter\.com(\/post\/[\w_]+)?/,
  local: /.+/,
  weibo: /weibo\.(?:com|cn)\/(?:u\/\d+|[\w\u4e00-\u9fff]+)/,
  bilibili: /bilibili\.com\/video\/BV[\w]+|space\.bilibili\.com\/\d+/,
};

function CapabilityMark({ enabled }: { enabled: boolean }) {
  const t = useT();
  return (
    <span>
      <span aria-hidden>{enabled ? "✓" : "—"}</span>
      <span className="sr-only">{enabled ? t("common.on") : t("common.off")}</span>
    </span>
  );
}

function ProviderCard({ source }: { source: ProviderInfo }) {
  const t = useT();
  const inputId = useId();
  const resultId = useId();
  const [url, setUrl] = useState("");
  const [validation, setValidation] = useState<{ ok: boolean; message: string } | null>(null);
  const pattern = URL_PATTERNS[source.source_name];
  const defaultUrl = DEFAULT_URLS[source.source_name];

  const validate = () => {
    if (!url.trim()) {
      setValidation({ ok: false, message: t("sources.enter_url") });
      return;
    }
    if (!pattern) {
      setValidation({ ok: false, message: t("sources.no_pattern") });
      return;
    }
    setValidation(pattern.test(url)
      ? { ok: true, message: t("sources.match_ok", { source: source.display_name }) }
      : { ok: false, message: t("sources.match_fail", { source: source.display_name }) });
  };

  return (
    <article className="card h-full p-4">
      <div className="mb-2 flex min-w-0 items-center gap-2">
        <span
          className="h-3 w-3 shrink-0 rounded-full"
          style={{ backgroundColor: getSourceColor(source.source_name) }}
          aria-hidden
        />
        <h3 className="min-w-0 flex-1 truncate text-base font-semibold text-fg">
          {source.display_name}
        </h3>
        <span className="shrink-0 font-mono text-xs text-muted">{source.source_name}</span>
      </div>

      <p className="mb-3 text-xs leading-relaxed text-muted">
        {t(SOURCE_DESCRIPTION_KEYS[source.source_name] || "sources.no_desc")}
      </p>

      <div className="mb-3 flex flex-wrap gap-2">
        <span className={`badge ${source.capabilities.can_download ? "border-success/30 bg-success-subtle text-success" : ""}`}>
          {source.capabilities.can_download ? t("sources.download_available") : t("sources.download_placeholder")}
        </span>
        {source.capabilities.supports_gallerydl && (
          <span className="badge border-accent/30 bg-accent-subtle text-accent">{t("sources.gallerydl")}</span>
        )}
        {source.capabilities.supports_tags && <span className="badge">{t("sources.tags")}</span>}
        {source.capabilities.is_reference_only && (
          <span className="badge border-warning/30 bg-warning-subtle text-warning">{t("sources.reference_only")}</span>
        )}
        {source.capabilities.can_import_local && (
          <span className="badge border-success/30 bg-success-subtle text-success">{t("sources.local_import")}</span>
        )}
      </div>

      <div className="border-t border-border pt-3">
        {pattern ? (
          <>
            <label htmlFor={inputId} className="mb-1 block text-xs text-muted">
              {t("sources.test_validation")}
            </label>
            <div className="flex min-w-0 flex-col gap-2 sm:flex-row">
              <input
                id={inputId}
                type="text"
                value={url}
                onChange={(event) => {
                  setUrl(event.target.value);
                  setValidation(null);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") validate();
                }}
                placeholder={defaultUrl || "https://…"}
                aria-describedby={validation ? resultId : undefined}
                className="input min-w-0 flex-1 font-mono"
              />
              <button type="button" onClick={validate} className="btn-ghost min-h-11 shrink-0 px-4 text-sm">
                {t("sources.test")}
              </button>
            </div>
            {defaultUrl && (
              <button
                type="button"
                onClick={() => {
                  setUrl(defaultUrl);
                  setValidation(null);
                }}
                className="mt-2 block min-h-6 max-w-full truncate text-left text-xs text-accent hover:underline"
              >
                {t("sources.try_default")}{" "}
                <span className="font-mono text-muted">{defaultUrl}</span>
              </button>
            )}
            {validation && (
              <div
                id={resultId}
                role="status"
                className={`mt-2 rounded-md border p-2 text-xs ${
                  validation.ok
                    ? "border-success/30 bg-success-subtle text-success"
                    : "border-danger/40 bg-danger-subtle text-danger"
                }`}
              >
                <span aria-hidden>{validation.ok ? "✓ " : "✗ "}</span>
                {validation.message}
              </div>
            )}
          </>
        ) : (
          <p className="text-xs text-muted">{t("sources.validation_unavailable")}</p>
        )}
      </div>
    </article>
  );
}

export default function SourceRegistryPanel({
  sources,
  loading,
  error,
  onRetry,
}: {
  sources?: ProviderInfo[];
  loading: boolean;
  error?: Error | null;
  onRetry: () => void;
}) {
  const t = useT();
  const items = sources || [];
  const downloadable = items.filter((source) => source.capabilities.can_download).length;
  const reference = items.filter((source) => source.capabilities.is_reference_only).length;
  const entrances = useStaggeredEntrance(items.map((source) => source.source_name));

  if (loading) {
    return (
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {Array.from({ length: 6 }).map((_, index) => (
          <div key={index} className="card animate-pulse p-4">
            <div className="mb-2 h-4 w-1/2 rounded bg-subtle" />
            <div className="mb-4 h-3 w-3/4 rounded bg-subtle" />
            <div className="h-16 rounded bg-subtle" />
          </div>
        ))}
      </div>
    );
  }

  if (error) return <ErrorState message={error.message} onRetry={onRetry} />;

  if (!items.length) {
    return <EmptyState title={t("sources.no_providers")} description={t("sources.no_providers_desc")} />;
  }

  return (
    <div>
      <p className="mb-4 text-sm text-muted">
        {t("sources.desc", { total: items.length, downloadable, reference })}
      </p>

      <div className="mb-8 grid grid-cols-1 gap-4 md:grid-cols-2">
        {items.map((source, index) => {
          const entrance = entrances(source.source_name, index);
          return (
            <div key={source.source_name} className={entrance.className} style={entrance.style}>
              <ProviderCard source={source} />
            </div>
          );
        })}
      </div>

      <details className="card p-4 text-sm">
        <summary className="min-h-11 cursor-pointer font-medium">{t("sources.matrix")}</summary>
        <div className="table-shell mt-3">
          <table className="min-w-[48rem] w-full text-sm">
            <thead>
              <tr className="table-head">
                <th className="px-2 py-2 text-left">{t("sources.col_provider")}</th>
                <th className="px-2 py-2 text-center">{t("sources.col_download")}</th>
                <th className="px-2 py-2 text-center">{t("sources.col_gallerydl")}</th>
                <th className="px-2 py-2 text-center">{t("sources.col_tags")}</th>
                <th className="px-2 py-2 text-center">{t("sources.col_reference")}</th>
                <th className="px-2 py-2 text-center">{t("sources.col_local")}</th>
                <th className="px-2 py-2 text-left">{t("sources.col_auth")}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((source) => (
                <tr key={source.source_name} className="table-row">
                  <td className="px-2 py-2 font-medium">{source.display_name}</td>
                  <td className="px-2 py-2 text-center"><CapabilityMark enabled={source.capabilities.can_download} /></td>
                  <td className="px-2 py-2 text-center"><CapabilityMark enabled={source.capabilities.supports_gallerydl} /></td>
                  <td className="px-2 py-2 text-center"><CapabilityMark enabled={source.capabilities.supports_tags} /></td>
                  <td className="px-2 py-2 text-center"><CapabilityMark enabled={source.capabilities.is_reference_only} /></td>
                  <td className="px-2 py-2 text-center"><CapabilityMark enabled={source.capabilities.can_import_local} /></td>
                  <td className="px-2 py-2 text-xs text-muted">
                    {source.source_name === "pixiv"
                      ? t("sources.auth_oauth")
                      : source.source_name === "x"
                        ? t("sources.auth_oauth_future")
                        : source.source_name === "danbooru"
                          ? t("sources.auth_basic")
                          : t("sources.auth_na")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
