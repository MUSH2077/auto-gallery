"use client";

import {
  forwardRef,
  useDeferredValue,
  useEffect,
  useId,
  useImperativeHandle,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Search, X } from "lucide-react";

import {
  api,
  type SearchAssistResponse,
  type SearchQualifierToken,
  type SearchScope,
  type SearchSuggestion,
} from "@/lib/api";
import { useT } from "@/lib/i18n";

type Translator = ReturnType<typeof useT>;

function diagnosticMessage(t: Translator, diagnostic: SearchAssistResponse["diagnostics"][number]) {
  switch (diagnostic.code) {
    case "unclosed_quote":
    case "invalid_quote":
      return t("search.error_quotes", { token: diagnostic.token });
    case "missing_value":
      return t("search.error_missing_value", { token: diagnostic.token });
    case "invalid_date":
      return t("search.error_invalid_date", { token: diagnostic.token });
    case "invalid_identity":
      return t("search.error_identity", { token: diagnostic.token });
    case "unsupported_url":
      return t("search.error_url", { token: diagnostic.token });
    case "invalid_value":
    case "unknown_value":
    case "unknown_qualifier":
      return t("search.error_unknown_value", { token: diagnostic.token });
    case "ambiguous_value":
      return t("search.error_ambiguous", { token: diagnostic.token });
    case "invalid_negation":
      return t("search.error_negation", { token: diagnostic.token });
    case "conflicting_values":
    case "duplicate_sort":
      return t("search.error_conflict", { token: diagnostic.token });
    case "permission_denied":
      return t("search.error_permission", { token: diagnostic.token });
    case "qualifier_not_available":
    case "type_not_available":
    case "no_compatible_type":
      return t("search.error_incompatible", { token: diagnostic.token });
    default:
      return t("search.error_generic", { token: diagnostic.token });
  }
}

function suggestionDescription(t: Translator, suggestion: SearchSuggestion) {
  if (suggestion.kind === "qualifier" && suggestion.help_id) {
    return t(suggestion.help_id, suggestion.description, {
      example: suggestion.example || suggestion.label,
    });
  }
  if (suggestion.kind === "repair") return t("search.suggestion_repair");
  return t("search.suggestion_value");
}

type ComposeRequest = {
  key: string;
  value?: string | null;
  operation?: "set" | "add" | "toggle" | "remove" | "replace-group";
  negated?: boolean;
  replace_values?: string[];
};

export function useSearchComposer({
  value,
  scope,
  onChange,
}: {
  value: string;
  scope: SearchScope;
  onChange: (value: string) => void;
}) {
  return useMutation({
    mutationFn: (compose: ComposeRequest) => api.assistSearch({
      before_cursor: value,
      scope,
      compose,
    }),
    onSuccess: (result) => onChange(result.canonical_query || result.query),
  });
}

export function useSearchBatchComposer({
  value,
  scope,
  onChange,
}: {
  value: string;
  scope: SearchScope;
  onChange: (value: string) => void;
}) {
  return useMutation({
    mutationFn: (composes: ComposeRequest[]) => api.assistSearch({
      before_cursor: value,
      scope,
      composes,
    }),
    onSuccess: (result) => onChange(result.canonical_query || result.query),
  });
}

export interface SmartSearchInputProps {
  value: string;
  onChange: (value: string) => void;
  scope?: SearchScope;
  placeholder?: string;
  ariaLabel?: string;
  className?: string;
  inputClassName?: string;
  autoFocus?: boolean;
  disabled?: boolean;
  showTokens?: boolean;
  showHelp?: boolean;
  onFocus?: () => void;
  onSubmit?: (canonicalQuery: string) => void;
}

export const SmartSearchInput = forwardRef<HTMLInputElement, SmartSearchInputProps>(function SmartSearchInput({
  value,
  onChange,
  scope = "global",
  placeholder,
  ariaLabel,
  className = "",
  inputClassName = "",
  autoFocus,
  disabled,
  showTokens = true,
  showHelp = false,
  onFocus,
  onSubmit,
}, forwardedRef) {
  const t = useT();
  const inputRef = useRef<HTMLInputElement>(null);
  useImperativeHandle(forwardedRef, () => inputRef.current as HTMLInputElement);
  const listId = useId();
  const statusId = useId();
  const deferredValue = useDeferredValue(value);
  const [focused, setFocused] = useState(false);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);

  const assist = useQuery({
    queryKey: ["search-assist", scope, deferredValue],
    queryFn: () => api.assistSearch({
      before_cursor: deferredValue,
      scope,
      limit: 10,
    }),
    enabled: focused,
    staleTime: 15_000,
    placeholderData: (previous) => previous,
  });

  const compose = useSearchComposer({ value, scope, onChange });
  const suggestions = assist.data?.suggestions || [];
  const qualifiers = (assist.data?.parsed?.tokens || []).filter(
    (token): token is SearchQualifierToken => token.kind === "qualifier",
  );
  const diagnostic = assist.data?.diagnostics?.[0];

  useEffect(() => {
    setActiveIndex(0);
  }, [deferredValue, suggestions.length]);

  useEffect(() => {
    if (!focused) setOpen(false);
  }, [focused]);

  const selectSuggestion = (index: number) => {
    const suggestion = suggestions[index];
    if (!suggestion) return;
    onChange(suggestion.query);
    setOpen(false);
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" && suggestions.length) {
      event.preventDefault();
      setOpen(true);
      setActiveIndex((index) => (index + 1) % suggestions.length);
    } else if (event.key === "ArrowUp" && suggestions.length) {
      event.preventDefault();
      setOpen(true);
      setActiveIndex((index) => (index - 1 + suggestions.length) % suggestions.length);
    } else if (event.key === "Home" && open && suggestions.length) {
      event.preventDefault();
      setActiveIndex(0);
    } else if (event.key === "End" && open && suggestions.length) {
      event.preventDefault();
      setActiveIndex(suggestions.length - 1);
    } else if (event.key === "Escape" && open && suggestions.length) {
      event.preventDefault();
      setOpen(false);
    } else if (event.key === "Enter") {
      if (open && suggestions[activeIndex]) {
        event.preventDefault();
        selectSuggestion(activeIndex);
      } else if (!diagnostic && onSubmit) {
        event.preventDefault();
        onSubmit(assist.data?.canonical_query || value);
      }
    }
  };

  const result: SearchAssistResponse | undefined = assist.data;
  const status = diagnostic
    ? diagnosticMessage(t, diagnostic)
    : result?.canonical_query && result.canonical_query !== value
      ? t("search.syntax_normalized", { query: result.canonical_query })
      : suggestions.length
        ? t("search.suggestion_count", { count: suggestions.length })
        : "";

  return (
    <div className={`relative min-w-0 ${className}`} data-smart-search>
      <div className={`flex min-h-11 items-center gap-2 rounded-md border bg-surface px-3 transition-colors ${
        diagnostic ? "border-danger" : focused ? "border-accent ring-2 ring-accent/15" : "border-border"
      }`}>
        <Search className="h-4 w-4 shrink-0 text-muted" strokeWidth={1.8} aria-hidden />
        <input
          ref={inputRef}
          value={value}
          onChange={(event) => {
            onChange(event.target.value);
            setOpen(true);
          }}
          onFocus={() => {
            setFocused(true);
            setOpen(true);
            onFocus?.();
          }}
          onBlur={() => setFocused(false)}
          onKeyDown={handleKeyDown}
          autoFocus={autoFocus}
          disabled={disabled}
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={open && suggestions.length > 0}
          aria-controls={listId}
          aria-activedescendant={open && suggestions[activeIndex] ? `${listId}-${activeIndex}` : undefined}
          aria-describedby={statusId}
          aria-invalid={!!diagnostic}
          aria-label={ariaLabel || placeholder || t("search.placeholder")}
          placeholder={placeholder || t("search.placeholder")}
          className={`h-10 min-w-0 flex-1 bg-transparent text-sm text-fg outline-none placeholder:text-placeholder ${inputClassName}`}
        />
        {value && !disabled && (
          <button
            type="button"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => {
              onChange("");
              requestAnimationFrame(() => inputRef.current?.focus());
            }}
            className="btn-icon min-h-9 min-w-9 text-muted"
            aria-label={t("search.clear")}
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        )}
      </div>

      {showTokens && (qualifiers.length > 0 || diagnostic || showHelp) && (
        <div className="mt-2 flex min-h-6 flex-wrap items-center gap-1.5">
          {qualifiers.map((token, index) => (
            <button
              key={`${token.key}:${token.value}:${token.negated}:${index}`}
              type="button"
              onClick={() => compose.mutate({
                key: token.key,
                value: token.value,
                negated: token.negated,
                operation: "remove",
              })}
              disabled={compose.isPending}
              className="inline-flex min-h-8 items-center gap-1 rounded-full border border-border bg-subtle px-2.5 font-mono text-[11px] text-muted transition-colors hover:border-danger/40 hover:text-danger"
              aria-label={t("search.remove_token", { token: `${token.negated ? "-" : ""}${token.key}:${token.value}` })}
            >
              {token.negated ? "-" : ""}{token.key}:{token.value}
              <X className="h-3 w-3" aria-hidden />
            </button>
          ))}
          {diagnostic && <span className="text-xs text-danger">{diagnosticMessage(t, diagnostic)}</span>}
          {!diagnostic && showHelp && qualifiers.length === 0 && (
            <span className="text-xs text-muted">{t("search.syntax_hint")}</span>
          )}
        </div>
      )}

      {open && focused && suggestions.length > 0 && (
        <div
          id={listId}
          role="listbox"
          className="absolute z-50 mt-1 max-h-72 w-full overflow-y-auto rounded-lg border border-border bg-surface p-1.5 shadow-overlay"
        >
          {suggestions.map((suggestion, index) => (
            <button
              id={`${listId}-${index}`}
              key={`${suggestion.kind}:${suggestion.query}:${index}`}
              type="button"
              role="option"
              aria-labelledby={`${listId}-${index}-label ${listId}-${index}-description`}
              aria-selected={activeIndex === index}
              onMouseDown={(event) => event.preventDefault()}
              onMouseEnter={() => setActiveIndex(index)}
              onClick={() => selectSuggestion(index)}
              className={`flex min-h-11 w-full items-center gap-3 rounded-md px-3 py-2 text-left ${
                activeIndex === index ? "bg-accent-subtle text-fg" : "text-muted hover:bg-subtle hover:text-fg"
              }`}
            >
              <span className="min-w-0 flex-1">
                <span id={`${listId}-${index}-label`} className="block truncate font-mono text-xs font-medium">
                  {suggestion.label}
                </span>
                <span
                  id={`${listId}-${index}-description`}
                  className="mt-0.5 block whitespace-normal break-words text-[11px] leading-4 text-muted"
                >
                  {suggestionDescription(t, suggestion)}
                </span>
              </span>
            </button>
          ))}
        </div>
      )}

      <span id={statusId} role="status" aria-live="polite" className="sr-only">{status}</span>
    </div>
  );
});
