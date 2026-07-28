import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const TOKEN_COOKIE = "ag_token";
const LOGIN = "/admin/login";

const PUBLIC_PREFIXES = [
  LOGIN,
  "/_next",
  "/favicon.ico",
  "/api/v1/auth/login",
  "/api/v1/auth/me",
  "/api/v1/auth/change-password",
];

function isPublicPath(pathname: string): boolean {
  if (
    pathname.startsWith("/_next/") ||
    pathname.endsWith(".ico") ||
    pathname.endsWith(".png") ||
    pathname.endsWith(".svg") ||
    pathname.endsWith(".css") ||
    pathname.endsWith(".js")
  ) {
    return true;
  }
  for (const prefix of PUBLIC_PREFIXES) {
    if (pathname === prefix || pathname.startsWith(prefix + "/") || pathname.startsWith(prefix + "?")) {
      return true;
    }
  }
  return false;
}

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const token = request.cookies.get(TOKEN_COOKIE)?.value;

  if (isPublicPath(pathname)) {
    if (pathname === LOGIN && token) {
      return NextResponse.redirect(new URL("/", request.url));
    }
    return NextResponse.next();
  }

  if (!token) {
    const loginUrl = new URL(LOGIN, request.url);
    loginUrl.searchParams.set("redirect", pathname);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  // "/" is included explicitly: the homepage now renders real content
  // (Task 4) instead of an unconditional server redirect into "/admin", so
  // it needs the same ag_token gate every /admin/* route gets — without
  // this the root path bypasses the middleware function entirely.
  matcher: ["/", "/admin/:path*"],
};
