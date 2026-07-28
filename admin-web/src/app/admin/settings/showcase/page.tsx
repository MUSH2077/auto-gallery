"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, queryKeys, type ProviderInfo } from "@/lib/api";
import { PageHeader, PageShell, PermissionGuard } from "@/components";
import { useShowcaseConfig, type ShowcaseConfig } from "@/lib/showcase/config";
import { useT } from "@/lib/i18n";

function OptionButton<T extends string>({
  value,
  activeValue,
  children,
  onSelect,
}: {
  value: T;
  activeValue: T;
  children: ReactNode;
  onSelect: (value: T) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(value)}
      aria-pressed={value === activeValue}
      className={`segment ${value === activeValue ? "segment-active" : ""}`}
    >
      {children}
    </button>
  );
}

function SettingRow({
  title,
  description,
  children,
}: {
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 border-b border-border py-4 last:border-0 md:flex-row md:items-center md:justify-between">
      <div className="min-w-0">
        <div className="text-sm font-medium text-fg">{title}</div>
        {description ? <div className="mt-1 text-xs text-muted">{description}</div> : null}
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

function ToggleButton({
  checked,
  onToggle,
  label,
  disabled,
}: {
  checked: boolean;
  onToggle: () => void;
  label: string;
  disabled?: boolean;
}) {
  const t = useT();
  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={disabled}
      aria-pressed={checked}
      aria-label={label}
      className={`rounded-md border px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
        checked ? "border-accent bg-accent-subtle text-accent" : "border-border text-muted hover:bg-subtle"
      }`}
    >
      {checked ? t("common.on") : t("common.off")}
    </button>
  );
}

function RangeRow({
  title,
  value,
  min,
  max,
  step,
  unit,
  onChange,
  disabled,
}: {
  title: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit?: string;
  onChange: (value: number) => void;
  disabled?: boolean;
}) {
  const decimals = step < 1 ? String(step).split(".")[1]?.length ?? 0 : 0;
  return (
    <SettingRow
      title={
        <>
          {title} <span className="tabular text-muted">({value.toFixed(decimals)}{unit ?? ""})</span>
        </>
      }
    >
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        aria-label={title}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-40 sm:w-56"
      />
    </SettingRow>
  );
}

export default function ShowcaseSettingsPage() {
  const t = useT();
  const { config, update } = useShowcaseConfig();
  const me = useQuery({ queryKey: queryKeys.me, queryFn: api.getMe });
  const sources = useQuery({ queryKey: queryKeys.sources, queryFn: api.sources });

  const nsfwLocked = me.data?.nsfw_visible === false;

  const scopeOptions: { value: ShowcaseConfig["scope"]; label: string }[] = [
    { value: "all", label: t("showcase_settings.scope_all", "全部") },
    { value: "favorites", label: t("showcase_settings.scope_favorites", "仅收藏") },
  ];
  const transitionOptions: { value: ShowcaseConfig["slideTransition"]; label: string }[] = [
    { value: "kenburns", label: t("showcase_settings.transition_kenburns", "Ken Burns 缓慢缩放") },
    { value: "crossfade", label: t("showcase_settings.transition_crossfade", "交叉淡入淡出") },
  ];
  const landingOptions: { value: ShowcaseConfig["landing"]; label: string }[] = [
    { value: "showcase", label: t("showcase_settings.landing_showcase", "展示墙") },
    { value: "dashboard", label: t("showcase_settings.landing_dashboard", "仪表盘") },
  ];

  return (
    <PermissionGuard module="system">
      <PageShell size="normal" className="page-transition">
        <div className="mb-4">
          <Link href="/admin/settings" className="text-sm text-accent hover:underline">
            {t("common.back", "返回")}
          </Link>
        </div>
        <PageHeader title={t("showcase_settings.title", "展示墙")} description={t("showcase_settings.desc", "首页动态展示墙的内容筛选、动效与幻灯片行为。")} />

        {/* ① 内容源 */}
        <section className="card p-5">
          <h2 className="section-title mb-2">{t("showcase_settings.group_source", "内容源")}</h2>
          <SettingRow title={t("showcase_settings.scope", "范围")}>
            <div className="segmented-control">
              {scopeOptions.map((option) => (
                <OptionButton key={option.value} value={option.value} activeValue={config.scope} onSelect={(value) => update({ scope: value })}>
                  {option.label}
                </OptionButton>
              ))}
            </div>
          </SettingRow>
          <SettingRow title={t("showcase_settings.source", "来源")}>
            <select
              value={config.source ?? ""}
              onChange={(e) => update({ source: e.target.value === "" ? null : e.target.value })}
              className="select w-48"
              aria-label={t("showcase_settings.source", "来源")}
            >
              <option value="">{t("showcase_settings.source_any", "任意")}</option>
              {(sources.data?.sources || []).filter((s: ProviderInfo) => s.capabilities.can_download || s.capabilities.can_import_local).map((s: ProviderInfo) => (
                <option key={s.source_name} value={s.source_name}>
                  {s.display_name}
                </option>
              ))}
            </select>
          </SettingRow>
          <SettingRow title={t("showcase_settings.tag", "标签")}>
            <input
              type="text"
              value={config.tag ?? ""}
              onChange={(e) => update({ tag: e.target.value === "" ? null : e.target.value })}
              placeholder={t("showcase_settings.tag_placeholder", "不限标签")}
              aria-label={t("showcase_settings.tag", "标签")}
              className="input w-48"
            />
          </SettingRow>
          <SettingRow
            title={t("showcase_settings.include_nsfw", "包含 NSFW 内容")}
            description={nsfwLocked ? t("showcase_settings.include_nsfw_locked", "当前账号未开放 NSFW 内容可见性，此项由账号设置强制关闭。") : undefined}
          >
            <ToggleButton
              checked={config.includeNsfw}
              onToggle={() => update({ includeNsfw: !config.includeNsfw })}
              disabled={nsfwLocked}
              label={t("showcase_settings.include_nsfw", "包含 NSFW 内容")}
            />
          </SettingRow>
        </section>

        {/* ② 动效 */}
        <section className="card mt-5 p-5">
          <h2 className="section-title mb-2">{t("showcase_settings.group_motion", "动效")}</h2>
          <RangeRow
            title={t("showcase_settings.plane_height")}
            value={config.planeHeightVh}
            min={30}
            max={70}
            step={5}
            disabled={config.minimal}
            onChange={(value) => update({ planeHeightVh: value })}
          />
          <RangeRow
            title={t("showcase_settings.auto_scroll_speed")}
            value={config.autoScrollSpeed}
            min={0.2}
            max={3}
            step={0.1}
            disabled={config.minimal}
            onChange={(value) => update({ autoScrollSpeed: value })}
          />
          <RangeRow
            title={t("showcase_settings.curve_strength")}
            value={config.curveStrength}
            min={0}
            max={1}
            step={0.05}
            disabled={config.minimal}
            onChange={(value) => update({ curveStrength: value })}
          />
          <SettingRow title={t("showcase_settings.minimal", "极简模式")} description={t("showcase_settings.minimal_hint", "开启后，以上动效参数将不再生效。")}>
            <ToggleButton
              checked={config.minimal}
              onToggle={() => update({ minimal: !config.minimal })}
              label={t("showcase_settings.minimal", "极简模式")}
            />
          </SettingRow>
        </section>

        {/* ③ 幻灯片 */}
        <section className="card mt-5 p-5">
          <h2 className="section-title mb-2">{t("showcase_settings.group_slideshow", "幻灯片")}</h2>
          <RangeRow
            title={t("showcase_settings.slide_dwell", "停留时长")}
            value={config.slideDwellMs}
            min={2000}
            max={15000}
            step={500}
            unit="ms"
            onChange={(value) => update({ slideDwellMs: value })}
          />
          <SettingRow title={t("showcase_settings.slide_transition", "切换效果")}>
            <div className="segmented-control">
              {transitionOptions.map((option) => (
                <OptionButton key={option.value} value={option.value} activeValue={config.slideTransition} onSelect={(value) => update({ slideTransition: value })}>
                  {option.label}
                </OptionButton>
              ))}
            </div>
          </SettingRow>
          <SettingRow title={t("showcase_settings.slide_loop", "循环播放")}>
            <ToggleButton checked={config.slideLoop} onToggle={() => update({ slideLoop: !config.slideLoop })} label={t("showcase_settings.slide_loop", "循环播放")} />
          </SettingRow>
          <SettingRow title={t("showcase_settings.slide_meta", "显示作品信息")}>
            <ToggleButton checked={config.slideShowMeta} onToggle={() => update({ slideShowMeta: !config.slideShowMeta })} label={t("showcase_settings.slide_meta", "显示作品信息")} />
          </SettingRow>
        </section>

        {/* ④ 首页行为 */}
        <section className="card mt-5 p-5">
          <h2 className="section-title mb-2">{t("showcase_settings.group_home", "首页行为")}</h2>
          <SettingRow title={t("showcase_settings.landing", "默认首页")}>
            <div className="segmented-control">
              {landingOptions.map((option) => (
                <OptionButton key={option.value} value={option.value} activeValue={config.landing} onSelect={(value) => update({ landing: value })}>
                  {option.label}
                </OptionButton>
              ))}
            </div>
          </SettingRow>
          <SettingRow title={t("showcase_settings.headline", "标题文案")}>
            <input
              type="text"
              value={config.headline}
              onChange={(e) => update({ headline: e.target.value })}
              placeholder={t("showcase.headline_default", "你的画廊，静静展开")}
              aria-label={t("showcase_settings.headline", "标题文案")}
              className="input w-56"
            />
          </SettingRow>
          <SettingRow title={t("showcase_settings.show_stats", "显示统计数据")}>
            <ToggleButton checked={config.showStats} onToggle={() => update({ showStats: !config.showStats })} label={t("showcase_settings.show_stats", "显示统计数据")} />
          </SettingRow>
        </section>
      </PageShell>
    </PermissionGuard>
  );
}
