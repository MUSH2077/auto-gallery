'use client';
import { createContext, useContext, type ReactNode } from 'react';

export type GalleryLanguage = 'zh' | 'en';
export type GalleryCopyKey =
  | 'skipToContent' | 'jobsHeading' | 'mainNavigation' | 'works' | 'jobs' | 'account' | 'search' | 'more' | 'quickActions' | 'quickJump'
  | 'viewWork' | 'selectWork' | 'selectTask' | 'selectWorks' | 'choose' | 'assets' | 'adult' | 'ai'
  | 'favorite' | 'unfavorite' | 'saved' | 'display' | 'displaySettings' | 'viewMode' | 'blurAdult'
  | 'searchLabel' | 'clearSearch' | 'workScope' | 'taskScope' | 'workTools' | 'taskTools'
  | 'taskCaption' | 'taskAndSource' | 'status' | 'progress' | 'actions' | 'taskProgress'
  | 'masonry' | 'grid' | 'list' | 'situation' | 'completed' | 'selectionDone' | 'selectionHint' | 'applyBatch' | 'confirmApply'
  | 'close' | 'selectTasks' | 'workSearch' | 'taskSearch';
type Vars = Record<string, string | number>;
type CopyTable = Record<GalleryCopyKey, string>;
const zh: CopyTable = {
  selectTasks: '选择任务', workSearch: '搜索标题或作者…', taskSearch: '搜索任务、作者或编号…', skipToContent: '跳到主要内容', jobsHeading: '采集与导入任务', mainNavigation: '主要导航', works: '作品', jobs: '任务', account: '账户', search: '搜索', more: '更多', quickActions: '快捷操作', quickJump: '快速跳转',
  viewWork: '查看作品：{title}', selectWorks: '选择作品', selectWork: '选择作品：{title}', selectTask: '选择任务：{id}', choose: '选择', assets: '{count} 张', adult: '成人内容', ai: 'AI 辅助',
  favorite: '收藏：{title}', unfavorite: '取消收藏：{title}', saved: '已收藏', display: '显示', displaySettings: '显示设置', viewMode: '展示方式', blurAdult: '模糊成人内容',
  searchLabel: '搜索', clearSearch: '清除搜索', workScope: '作品范围', taskScope: '任务状态快捷筛选', workTools: '作品工具', taskTools: '任务工具',
  taskCaption: '采集与导入任务，包含来源、状态、资源进度和可用操作', taskAndSource: '任务与来源', status: '状态', progress: '进度', actions: '操作', taskProgress: '{title}进度',
  masonry: '瀑布流', grid: '网格', list: '列表', situation: '当前情况', completed: '完成', selectionDone: '完成选择', selectionHint: '选择任务后可批量处理', applyBatch: '执行批量操作', confirmApply: '确认执行', close: '关闭{title}',
};
const en: CopyTable = {
  selectTasks: 'Select tasks', workSearch: 'Search title or creator…', taskSearch: 'Search tasks, creators, or IDs…', skipToContent: 'Skip to main content', jobsHeading: 'Collection and import tasks', mainNavigation: 'Main navigation', works: 'Works', jobs: 'Jobs', account: 'Account', search: 'Search', more: 'More', quickActions: 'Quick actions', quickJump: 'Quick jump',
  viewWork: 'View work: {title}', selectWorks: 'Select works', selectWork: 'Select work: {title}', selectTask: 'Select task: {id}', choose: 'Select', assets: '{count} assets', adult: 'Adult content', ai: 'AI assisted',
  favorite: 'Favorite: {title}', unfavorite: 'Unfavorite: {title}', saved: 'Saved', display: 'Display', displaySettings: 'Display settings', viewMode: 'View', blurAdult: 'Blur adult content',
  searchLabel: 'Search', clearSearch: 'Clear search', workScope: 'Work scope', taskScope: 'Task status shortcuts', workTools: 'Work tools', taskTools: 'Task tools',
  taskCaption: 'Collection and import tasks, including source, status, resource progress, and available actions', taskAndSource: 'Task and source', status: 'Status', progress: 'Progress', actions: 'Actions', taskProgress: '{title} progress',
  masonry: 'Masonry', grid: 'Grid', list: 'List', situation: 'Current situation', completed: 'Done', selectionDone: 'Finish selection', selectionHint: 'Select tasks to apply a batch action', applyBatch: 'Apply batch action', confirmApply: 'Confirm action', close: 'Close {title}',
};
const tables: Record<GalleryLanguage, CopyTable> = { zh, en };
function format(template: string, vars?: Vars) { return vars ? template.replace(/\{([^{}]+)\}/g, (match, name: string) => vars[name] === undefined ? match : String(vars[name])) : template; }
export function galleryCopy(lang: GalleryLanguage, key: GalleryCopyKey, vars?: Vars) { return format(tables[lang][key] || zh[key], vars); }
const GalleryCopyContext = createContext<{ lang: GalleryLanguage; copy: (key: GalleryCopyKey, vars?: Vars) => string }>({ lang: 'zh', copy: (key, vars) => galleryCopy('zh', key, vars) });
export function GalleryCopyProvider({ lang = 'zh', children }: { lang?: GalleryLanguage; children: ReactNode }) { return <GalleryCopyContext.Provider value={{ lang, copy: (key, vars) => galleryCopy(lang, key, vars) }}>{children}</GalleryCopyContext.Provider>; }
export function useGalleryCopy() { return useContext(GalleryCopyContext); }
