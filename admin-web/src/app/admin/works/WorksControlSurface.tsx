"use client";

import Link from "next/link";
import { ArrowDownUp, LayoutGrid, ListFilter, X } from "lucide-react";
import {
  type ReactNode,
  type RefObject,
  createRef,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import {
  type AppearanceSettings,
  type WorkCardSize,
  type WorksViewMode,
} from "@/lib/appearance";
import { useT } from "@/lib/i18n";

export type WorksPanelKey = "filter" | "sort" | "display";

type PanelRender = (close: () => void) => ReactNode;

function useDesktopPanel() {
  const [desktop, setDesktop] = useState(() => (
    typeof window === "undefined" ? true : window.matchMedia("(min-width: 768px)").matches
  ));
  useEffect(() => {
    const query = window.matchMedia("(min-width: 768px)");
    const sync = () => setDesktop(query.matches);
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);
  return desktop;
}

function focusableElements(root: HTMLElement) {
  return Array.from(root.querySelectorAll<HTMLElement>(
    'input:not([disabled]), button:not([disabled]), a[href], select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )).filter((element) => !element.hasAttribute("hidden") && element.getAttribute("aria-hidden") !== "true");
}

function ResponsiveWorksPanel({
  open,
  title,
  triggerRef,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  triggerRef: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
  children: ReactNode;
}) {
  const t = useT();
  const desktop = useDesktopPanel();
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  const [position, setPosition] = useState({ left: 16, top: 80 });

  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);

  useEffect(() => {
    if (!open || !desktop) return;
    const measure = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const width = Math.min(420, window.innerWidth - 24);
      setPosition({
        left: Math.max(12, Math.min(rect.right - width, window.innerWidth - width - 12)),
        top: Math.min(rect.bottom + 8, window.innerHeight - 120),
      });
    };
    measure();
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, true);
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure, true);
    };
  }, [desktop, open, triggerRef]);

  useEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    const previousOverflow = document.body.style.overflow;
    if (!desktop) document.body.style.overflow = "hidden";
    const focusFrame = window.requestAnimationFrame(() => {
      focusableElements(panelRef.current || document.body)[0]?.focus();
    });
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (desktop || event.key !== "Tab" || !panelRef.current) return;
      const elements = focusableElements(panelRef.current);
      if (!elements.length) return;
      const first = elements[0];
      const last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    const onPointerDown = (event: PointerEvent) => {
      if (!desktop || panelRef.current?.contains(event.target as Node) || trigger?.contains(event.target as Node)) return;
      onCloseRef.current();
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("pointerdown", onPointerDown);
      document.body.style.overflow = previousOverflow;
      window.requestAnimationFrame(() => trigger?.focus());
    };
  }, [desktop, open, triggerRef]);

  if (!open || typeof document === "undefined") return null;
  const panel = (
    <div
      className={desktop ? "contents" : "fixed inset-0 z-[70] bg-black/40"}
      onMouseDown={(event) => {
        if (!desktop && event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal={desktop ? undefined : true}
        aria-labelledby={titleId}
        data-panel-mode={desktop ? "popover" : "sheet"}
        className={desktop
          ? "popover fixed z-[71] max-h-[min(72vh,680px)] w-[min(420px,calc(100vw-24px))] overflow-y-auto rounded-xl border border-border bg-surface shadow-overlay dark:shadow-overlay-dark"
          : "overlay-panel fixed inset-x-0 bottom-0 z-[71] max-h-[min(82vh,720px)] overflow-y-auto rounded-t-2xl border border-border bg-surface pb-[env(safe-area-inset-bottom)] shadow-2xl"}
        style={desktop ? position : undefined}
      >
        <header className="sticky top-0 z-10 flex items-center justify-between border-b border-border bg-surface/95 px-4 py-3 backdrop-blur">
          <h2 id={titleId} className="text-sm font-semibold text-fg">{title}</h2>
          <button type="button" className="btn-icon border-0" onClick={onClose} aria-label={t("common.close_dialog")}>
            <X className="h-4 w-4" aria-hidden />
          </button>
        </header>
        <div className="p-4">{children}</div>
      </div>
    </div>
  );
  return createPortal(panel, document.body);
}

function ControlTrigger({
  panel,
  activePanel,
  setActivePanel,
  triggerRef,
  label,
  detail,
  count,
  icon,
}: {
  panel: WorksPanelKey;
  activePanel: WorksPanelKey | null;
  setActivePanel: (panel: WorksPanelKey | null) => void;
  triggerRef: RefObject<HTMLButtonElement | null>;
  label: string;
  detail?: string;
  count?: number;
  icon: ReactNode;
}) {
  const open = activePanel === panel;
  return (
    <button
      ref={triggerRef}
      type="button"
      aria-label={label}
      aria-haspopup="dialog"
      aria-expanded={open}
      onClick={() => setActivePanel(open ? null : panel)}
      className={`inline-flex min-h-10 items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors ${open ? "border-accent bg-accent-subtle text-accent" : "border-border bg-surface text-fg hover:bg-subtle"}`}
    >
      {icon}
      <span className="font-medium">{label}</span>
      {detail ? <span className="hidden max-w-28 truncate text-xs text-muted lg:inline">{detail}</span> : null}
      {count ? <span className="rounded-full bg-accent px-1.5 py-0.5 text-[10px] font-semibold leading-none text-white">{count}</span> : null}
    </button>
  );
}

export function WorksControlSurface({
  search,
  filterCount = 0,
  sortSummary,
  displaySummary,
  renderFilter,
  renderSort,
  renderDisplay,
}: {
  search: ReactNode;
  filterCount?: number;
  sortSummary?: string;
  displaySummary?: string;
  renderFilter: PanelRender;
  renderSort: PanelRender;
  renderDisplay: PanelRender;
}) {
  const t = useT();
  const [activePanel, setActivePanel] = useState<WorksPanelKey | null>(null);
  const refs = useMemo(() => ({
    filter: createRef<HTMLButtonElement>(),
    sort: createRef<HTMLButtonElement>(),
    display: createRef<HTMLButtonElement>(),
  }), []);
  const close = () => setActivePanel(null);
  const triggers = [
    { key: "filter" as const, label: t("works.filter_button"), detail: undefined, count: filterCount, icon: <ListFilter className="h-4 w-4" aria-hidden /> },
    { key: "sort" as const, label: t("works.sort_button"), detail: sortSummary, icon: <ArrowDownUp className="h-4 w-4" aria-hidden /> },
    { key: "display" as const, label: t("works.display_button"), detail: displaySummary, icon: <LayoutGrid className="h-4 w-4" aria-hidden /> },
  ];
  const titles: Record<WorksPanelKey, string> = {
    filter: t("works.filter_panel_title"),
    sort: t("works.sort_panel_title"),
    display: t("works.display_panel_title"),
  };
  const renderers: Record<WorksPanelKey, PanelRender> = {
    filter: renderFilter,
    sort: renderSort,
    display: renderDisplay,
  };

  return (
    <div data-page-primary-content className="toolbar mb-4 flex flex-col gap-2 md:flex-row md:items-center">
      <div className="min-w-0 flex-1">{search}</div>
      <div className="grid grid-cols-3 gap-2 md:flex md:shrink-0">
        {triggers.map((trigger) => (
          <ControlTrigger
            key={trigger.key}
            panel={trigger.key}
            activePanel={activePanel}
            setActivePanel={setActivePanel}
            triggerRef={refs[trigger.key]}
            label={trigger.label}
            detail={trigger.detail}
            count={trigger.count}
            icon={trigger.icon}
          />
        ))}
      </div>
      {(["filter", "sort", "display"] as WorksPanelKey[]).map((panel) => (
        <ResponsiveWorksPanel
          key={panel}
          open={activePanel === panel}
          title={titles[panel]}
          triggerRef={refs[panel]}
          onClose={close}
        >
          {renderers[panel](close)}
        </ResponsiveWorksPanel>
      ))}
    </div>
  );
}

function OptionButton<T extends string>({
  value,
  current,
  label,
  onSelect,
}: {
  value: T;
  current: T;
  label: string;
  onSelect: (value: T) => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={value === current}
      onClick={() => onSelect(value)}
      className={`segment min-h-9 flex-1 ${value === current ? "segment-active" : ""}`}
    >
      {label}
    </button>
  );
}

function ToggleRow({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label className="flex min-h-11 cursor-pointer items-center justify-between gap-4 border-b border-border py-2.5 last:border-0">
      <span className="text-sm text-fg">{label}</span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="h-4 w-4 rounded border-border text-accent"
      />
    </label>
  );
}

export function WorksDisplayPanel({
  appearance,
  updateAppearance,
}: {
  appearance: AppearanceSettings;
  updateAppearance: (patch: Partial<AppearanceSettings>) => void;
}) {
  const t = useT();
  const viewModes: { value: WorksViewMode; label: string }[] = [
    { value: "grid", label: t("works.view_grid_plain") },
    { value: "list", label: t("works.view_list_plain") },
    { value: "masonry", label: t("works.view_masonry") },
  ];
  const sizes: { value: WorkCardSize; label: string }[] = [
    { value: "small", label: t("works.card_size_small") },
    { value: "medium", label: t("works.card_size_medium") },
    { value: "large", label: t("works.card_size_large") },
  ];
  return (
    <div className="space-y-5">
      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">{t("works.layout")}</h3>
        <div className="segmented-control flex w-full">
          {viewModes.map((option) => (
            <OptionButton key={option.value} {...option} current={appearance.worksViewMode} onSelect={(worksViewMode) => updateAppearance({ worksViewMode })} />
          ))}
        </div>
      </section>
      <section>
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">{t("works.card_size")}</h3>
        <div className="segmented-control flex w-full">
          {sizes.map((option) => (
            <OptionButton key={option.value} {...option} current={appearance.workCardSize} onSelect={(workCardSize) => updateAppearance({ workCardSize })} />
          ))}
        </div>
      </section>
      <section>
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">{t("works.card_elements")}</h3>
        <ToggleRow label={t("works.show_checkboxes")} checked={appearance.workCardShowCheckbox} onChange={(workCardShowCheckbox) => updateAppearance({ workCardShowCheckbox })} />
        <ToggleRow label={t("works.show_ai_badge")} checked={appearance.workCardShowAi} onChange={(workCardShowAi) => updateAppearance({ workCardShowAi })} />
        <ToggleRow label={t("works.show_nsfw_badge")} checked={appearance.workCardShowNsfw} onChange={(workCardShowNsfw) => updateAppearance({ workCardShowNsfw })} />
        <ToggleRow label={t("works.show_favorite")} checked={appearance.workCardShowFavorite} onChange={(workCardShowFavorite) => updateAppearance({ workCardShowFavorite })} />
      </section>
      <section>
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">{t("works.preview_section")}</h3>
        <ToggleRow label={t("works.hover_preview")} checked={appearance.workPreviewEnabled} onChange={(workPreviewEnabled) => updateAppearance({ workPreviewEnabled })} />
        <ToggleRow label={t("works.blur_nsfw")} checked={appearance.blurNsfw} onChange={(blurNsfw) => updateAppearance({ blurNsfw })} />
        <Link href="/admin/settings/appearance" className="mt-3 inline-flex text-sm font-medium text-accent hover:underline">
          {t("works.more_preview_settings")}
        </Link>
      </section>
    </div>
  );
}
