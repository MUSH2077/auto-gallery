"use client";
import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from "react";

type Lang = "zh" | "en";

const STORAGE_KEY = "auto-gallery-lang";

const zh: Record<string, string> = {
  "nav.dashboard": "仪表盘",
  "nav.sources": "数据源",
  "nav.creators": "创作者",
  "nav.subscriptions": "订阅",
  "nav.downloads": "下载",
  "nav.works": "作品",
  "nav.tags": "标签",
  "nav.scheduler": "调度",
  "nav.import": "导入",
  "nav.danbooru": "Danbooru",
  "nav.settings": "设置",
  "settings.title": "设置",
  "settings.gallerydl": "gallery-dl 配置",
  "settings.gallerydl.desc": "Pixiv、Twitter/X、Iwara 提取器设置、认证令牌、文件组织、速率限制。",
  "settings.dedup": "去重",
  "settings.dedup.desc": "源级、跨源和感知哈希去重控制，带开关。",
  "settings.auth": "认证状态",
  "settings.auth.desc": "监控所有订阅源的认证健康状态。检测过期 Cookie 和令牌。",
  "settings.sub_defaults": "订阅默认值",
  "settings.sub_defaults.desc": "默认同步间隔、调度器扫描频率和新订阅行为。",
  "settings.dl_defaults": "下载默认值",
  "settings.dl_defaults.desc": "gallery-dl 下载任务的超时、重试次数和退避策略。",
  "settings.search_index": "搜索索引",
  "settings.search_index.desc": "Meilisearch 全量重建索引。",
  "settings.current_config": "当前配置",
  "settings.system_info": "系统信息",
  "settings.language": "界面语言",
  "settings.language.desc": "选择管理后台的显示语言。",
  "settings.reindex": "重建索引",
  "settings.reindexing": "重建中...",
  "common.save": "保存",
  "common.saving": "保存中...",
  "common.saved": "已保存",
  "common.cancel": "取消",
  "common.confirm": "确认",
  "common.delete": "删除",
  "common.edit": "编辑",
  "common.create": "创建",
  "common.retry": "重试",
  "common.loading": "加载中...",
  "common.error": "错误",
  "common.no_data": "暂无数据",
  "common.back": "返回",
  "theme.light": "浅色",
  "theme.dark": "深色",
  "theme.system": "跟随系统",
};

const en: Record<string, string> = {};

function buildEn(zh: Record<string, string>): Record<string, string> {
  // English keys are the key itself (the key is the English fallback)
  const result: Record<string, string> = {};
  for (const k of Object.keys(zh)) {
    // Use the key's last segment as the default English
    const parts = k.split(".");
    const last = parts[parts.length - 1];
    result[k] = last.charAt(0).toUpperCase() + last.slice(1).replace(/_/g, " ");
  }
  // Override with proper English for nav items
  Object.assign(result, {
    "nav.dashboard": "Dashboard",
    "nav.sources": "Sources",
    "nav.creators": "Creators",
    "nav.subscriptions": "Subscriptions",
    "nav.downloads": "Downloads",
    "nav.works": "Works",
    "nav.tags": "Tags",
    "nav.scheduler": "Scheduler",
    "nav.import": "Import",
    "nav.danbooru": "Danbooru",
    "nav.settings": "Settings",
    "settings.title": "Settings",
    "settings.gallerydl": "gallery-dl Config",
    "settings.gallerydl.desc": "Pixiv, Twitter/X, Iwara extractor settings, auth tokens, file organization, rate limiting.",
    "settings.dedup": "Deduplication",
    "settings.dedup.desc": "Source-level, cross-source, and perceptual hash dedup controls with toggles.",
    "settings.auth": "Auth & Cookie Status",
    "settings.auth.desc": "Monitor authentication health for all subscription sources. Detect expired cookies and tokens.",
    "settings.sub_defaults": "Subscription Defaults",
    "settings.sub_defaults.desc": "Default sync interval, scheduler scan frequency, and new subscription behavior.",
    "settings.dl_defaults": "Download Job Defaults",
    "settings.dl_defaults.desc": "Timeout, max retries, and exponential backoff for gallery-dl download jobs.",
    "settings.search_index": "Search Index",
    "settings.search_index.desc": "Admin-triggered full re-indexing of Meilisearch.",
    "settings.current_config": "Current Configuration",
    "settings.system_info": "System Information",
    "settings.language": "Language",
    "settings.language.desc": "Select the display language for the admin panel.",
    "settings.reindex": "Reindex Now",
    "settings.reindexing": "Reindexing...",
    "common.save": "Save",
    "common.saving": "Saving...",
    "common.saved": "Saved!",
    "common.cancel": "Cancel",
    "common.confirm": "Confirm",
    "common.delete": "Delete",
    "common.edit": "Edit",
    "common.create": "Create",
    "common.retry": "Retry",
    "common.loading": "Loading...",
    "common.error": "Error",
    "common.no_data": "No data",
    "common.back": "Back",
    "theme.light": "Light",
    "theme.dark": "Dark",
    "theme.system": "System",
  });
  return result;
}

const enBuilt = buildEn(zh);
const dictionaries: Record<Lang, Record<string, string>> = { zh, en: enBuilt };

interface I18nContextType {
  lang: Lang;
  t: (key: string, fallback?: string) => string;
  setLang: (l: Lang) => void;
}

const I18nContext = createContext<I18nContextType>({
  lang: "zh",
  t: (k, fb) => fb || k,
  setLang: () => {},
});

export function useI18n() {
  return useContext(I18nContext);
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>("zh");

  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored === "en" || stored === "zh") setLangState(stored);
    } catch {}
  }, []);

  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    try { localStorage.setItem(STORAGE_KEY, l); } catch {}
  }, []);

  const t = useCallback(
    (key: string, fallback?: string) => {
      return dictionaries[lang]?.[key] || dictionaries.en[key] || fallback || key;
    },
    [lang]
  );

  return <I18nContext.Provider value={{ lang, t, setLang }}>{children}</I18nContext.Provider>;
}

export function useT() {
  const { t } = useI18n();
  return t;
}
