import { NextRequest } from "next/server";

const BACKEND_INTERNAL_URL = process.env.BACKEND_INTERNAL_URL || "http://backend:8000";

export const runtime = "nodejs";

function buildTargetUrl(request: NextRequest, path: string[]) {
  const target = new URL(`/api/v1/${path.join("/")}`, BACKEND_INTERNAL_URL);
  target.search = request.nextUrl.search;
  return target;
}

async function proxy(request: NextRequest, path: string[]) {
  const target = buildTargetUrl(request, path);
  const outboundHeaders = new Headers();

  request.headers.forEach((value, key) => {
    const lower = key.toLowerCase();
    if (lower === "host" || lower === "content-length" || lower === "x-admin-key") {
      return;
    }
    outboundHeaders.set(key, value);
  });

  const adminPassword = process.env.ADMIN_PASSWORD;
  if (adminPassword) {
    outboundHeaders.set("X-Admin-Key", adminPassword);
  }

  const init: RequestInit = {
    method: request.method,
    headers: outboundHeaders,
    redirect: "manual",
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.arrayBuffer();
  }

  const response = await fetch(target, init);
  const responseHeaders = new Headers(response.headers);
  responseHeaders.delete("content-encoding");
  responseHeaders.delete("transfer-encoding");

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders,
  });
}

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function POST(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function PUT(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function PATCH(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path);
}

export async function DELETE(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  return proxy(request, path);
}
