import type { IncomingMessage, ServerResponse } from "node:http";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { MAX_UPLOAD_REQUEST_BYTES } from "../uploadLimits.ts";

const REQUEST_HEADERS = [
  "content-type", "content-length", "cookie", "authorization", "origin", "x-csrf-token",
];
const HOP_HEADERS = new Set([
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade",
]);

export function forwardUpload(request: IncomingMessage, response: ServerResponse): void {
  const error = (status: number, code: string, message: string) => {
    if (response.destroyed) return;
    if (response.headersSent) { response.destroy(); return; }
    response.writeHead(status, { "content-type": "application/json", connection: "close" });
    response.end(JSON.stringify({ detail: { code, message } }));
  };
  if (request.method !== "POST") {
    response.setHeader("allow", "POST");
    error(405, "method_not_allowed", "Use POST for uploads");
    return;
  }
  const length = request.headers["content-length"];
  if (typeof length === "string" && /^\d+$/.test(length) && Number(length) > MAX_UPLOAD_REQUEST_BYTES) {
    error(413, "upload_request_too_large", "Upload request exceeds the byte limit");
    return;
  }
  const headers: Record<string, string | string[]> = {};
  for (const name of REQUEST_HEADERS) {
    const value = request.headers[name];
    if (value !== undefined) headers[name] = value;
  }
  const backend = new URL("/api/v1/upload", process.env.BACKEND_INTERNAL_URL || "http://backend:8000");
  // Use Node streams directly: pipe pauses the client when the backend cannot
  // accept more data, without parsing, cloning, or queuing the entire body.
  const upstream = (backend.protocol === "https:" ? httpsRequest : httpRequest)(backend, {
    method: "POST", headers,
  });
  const timer = setTimeout(() => {
    error(504, "upload_proxy_timeout", "Upload timed out; retry the upload");
    upstream.destroy();
  }, 10 * 60 * 1000);
  const abort = () => upstream.destroy();
  const close = () => {
    clearTimeout(timer);
    request.off("aborted", abort);
    request.unpipe(upstream);
    upstream.destroy();
  };
  request.once("aborted", abort);
  request.once("error", abort);
  response.once("close", close);
  response.once("finish", close);
  upstream.once("error", () => {
    error(502, "upload_proxy_unavailable", "Upload service unavailable; retry the upload");
  });
  upstream.once("response", incoming => {
    const excluded = new Set(HOP_HEADERS);
    for (const name of (incoming.headers.connection || "").split(",")) excluded.add(name.trim().toLowerCase());
    for (const [name, value] of Object.entries(incoming.headers)) {
      if (value !== undefined && !excluded.has(name)) response.setHeader(name, value);
    }
    response.statusCode = incoming.statusCode || 502;
    incoming.once("error", () => response.destroy());
    incoming.pipe(response);
  });
  upstream.flushHeaders();
  request.pipe(upstream);
}
