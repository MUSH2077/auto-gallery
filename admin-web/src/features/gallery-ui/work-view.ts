import type { WorksViewMode } from '../works/view';

export const PREVIEW_DEFAULT_WORK_VIEW: WorksViewMode = 'masonry';
export const WORK_VIEW_ORDER = ['masonry', 'grid', 'list'] as const;
export const WORK_VIEW_OPTIONS = {
  masonry: { label: '瀑布流', icon: 'masonry' },
  grid: { label: '网格', icon: 'grid' },
  list: { label: '列表', icon: 'list' },
} as const satisfies Record<WorksViewMode, { label: string; icon: WorksViewMode }>;

export function resolveWorkView(value: string | undefined, fallback: WorksViewMode): WorksViewMode {
  return WORK_VIEW_ORDER.find(mode => mode === value) ?? fallback;
}
