import type { ReadonlyURLSearchParams } from "next/navigation";

// All rendered pages use the App Router. The only Pages Router entry is the
// raw multipart API handler, which does not render navigation hooks. Next
// otherwise adds nullable Pages Router signatures as soon as pages/api exists.
declare module "next/navigation" {
  export function useSearchParams(): ReadonlyURLSearchParams;
  export function usePathname(): string;
  export function useParams<
    T extends Record<string, string | string[]> = Record<string, string | string[]>,
  >(): T;
}
