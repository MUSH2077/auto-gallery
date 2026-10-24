export type IconName = 'image' | 'jobs' | 'search' | 'grid' | 'masonry' | 'list' | 'heart' | 'close' | 'check' | 'filter';
export function Icon({ name }: { name: IconName }) {
  const paths: Record<IconName, string> = {
    image: 'M3 4h18v16H3z M3 16l5-5 5 5 4-4 4 4 M15 8h.01',
    jobs: 'M5 4h14v17H5z M9 4V2h6v2 M9 9h6 M9 13h6 M9 17h3',
    search: 'M16 16l5 5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
    grid: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
    masonry: 'M3 3h7v8H3z M14 3h7v4h-7z M3 15h7v6H3z M14 11h7v10h-7z',
    list: 'M8 5h13 M8 12h13 M8 19h13 M3 5h1 M3 12h1 M3 19h1',
    heart: 'M12 20S2 14 2 8a5 5 0 0 1 10-1 5 5 0 0 1 10 1c0 6-10 12-10 12z',
    close: 'M5 5l14 14 M19 5L5 19',
    check: 'M4 12l5 5L20 6',
    filter: 'M3 5h18 M6 12h12 M9 19h6',
  };
  return <svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d={paths[name]} /></svg>;
}
