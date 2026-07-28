'use client';
import { useI18n } from '@/lib/i18n';

export function useFormatDate() {
  const { lang } = useI18n();
  const locale = lang === 'zh' ? 'zh-CN' : 'en-US';
  return (date: string | Date, options?: Intl.DateTimeFormatOptions) => {
    return new Date(date).toLocaleDateString(locale, options);
  };
}
