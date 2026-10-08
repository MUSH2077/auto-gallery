import assert from "node:assert/strict";
import test from "node:test";
import { createServer, request as httpRequest } from "node:http";
import { once } from "node:events";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";

const directory = await mkdtemp(path.join(tmpdir(), "ag-upload-proxy-test-"));
for (const name of ["uploadLimits", "server/uploadProxy"]) {
  const source = await readFile(new URL("../src/lib/" + name + ".ts", import.meta.url), "utf8");
  const js = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText.replace("../uploadLimits.ts", "./uploadLimits.mjs");
  await writeFile(path.join(directory, name.split("/").at(-1) + ".mjs"), js);
}
const { forwardUpload } = await import(pathToFileURL(path.join(directory, "uploadProxy.mjs")));
const { MAX_UPLOAD_REQUEST_BYTES } = await import(pathToFileURL(path.join(directory, "uploadLimits.mjs")));
test.after(() => rm(directory, { recursive: true, force: true }));

async function server(t, handler) {
  const instance = createServer(handler);
  await new Promise(resolve => instance.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    instance.closeAllConnections();
    await new Promise(resolve => instance.close(resolve));
  });
  return "http://127.0.0.1:" + instance.address().port;
}
async function setup(t, handler) {
  const previous = process.env.BACKEND_INTERNAL_URL;
  process.env.BACKEND_INTERNAL_URL = await server(t, handler);
  t.after(() => {
    if (previous === undefined) delete process.env.BACKEND_INTERNAL_URL;
    else process.env.BACKEND_INTERNAL_URL = previous;
  });
  return server(t, forwardUpload);
}

test("forwards multipart chunks before the source finishes, past 10MiB", async t => {
  let first;
  const received = new Promise(resolve => { first = resolve; });
  let bytes = 0;
  const base = await setup(t, (req, res) => {
    assert.equal(req.url, "/api/v1/upload");
    assert.equal(req.headers.cookie, "ag_session=fixture");
    assert.equal(req.headers.authorization, "Bearer fixture");
    assert.equal(req.headers.origin, "http://frontend");
    assert.equal(req.headers["x-csrf-token"], "fixture");
    assert.equal(req.headers["content-type"], "multipart/form-data; boundary=fixture");
    req.on("data", chunk => { bytes += chunk.length; first(); });
    req.on("end", () => res.end(JSON.stringify({ bytes })));
  });
  const client = httpRequest(base, { method: "POST", headers: {
    cookie: "ag_session=fixture", authorization: "Bearer fixture", origin: "http://frontend",
    "x-csrf-token": "fixture", "content-type": "multipart/form-data; boundary=fixture",
  } });
  const response = once(client, "response");
  const chunk = Buffer.alloc(64 * 1024);
  client.write(chunk);
  await received;
  for (let i = 1; i < 176; i++) if (!client.write(chunk)) await once(client, "drain");
  client.end();
  const [reply] = await response;
  let text = "";
  for await (const part of reply) text += part;
  assert.equal(reply.statusCode, 200);
  assert.equal(JSON.parse(text).bytes, 176 * chunk.length);
});

for (const status of [401, 403, 413, 503]) {
  test("preserves upstream status " + status + " and error headers", async t => {
    const base = await setup(t, (req, res) => {
      req.resume();
      req.on("end", () => {
        res.writeHead(status, { "content-type": "application/json", "retry-after": "10",
          "set-cookie": ["first=one", "second=two"] });
        res.end(JSON.stringify({ detail: { code: "fixture", message: "reason" } }));
      });
    });
    const response = await fetch(base, { method: "POST", body: "fixture" });
    assert.equal(response.status, status);
    assert.equal(response.headers.get("retry-after"), "10");
    assert.match(response.headers.get("set-cookie"), /first=one/);
    assert.equal((await response.json()).detail.code, "fixture");
  });
}

test("rejects an oversized declared request before contacting the backend", async t => {
  let sent = 0;
  const base = await setup(t, (req, res) => { sent++; req.resume(); res.end(); });
  const client = httpRequest(base, { method: "POST", headers: {
    "content-length": MAX_UPLOAD_REQUEST_BYTES + 1,
  } });
  const response = once(client, "response");
  client.flushHeaders();
  const [reply] = await response;
  let body = "";
  for await (const chunk of reply) body += chunk;
  assert.equal(reply.statusCode, 413);
  assert.equal(JSON.parse(body).detail.code, "upload_request_too_large");
  assert.equal(sent, 0);
  client.destroy();
});

test("client cancellation closes the upstream request", async t => {
  let started, closed;
  const start = new Promise(resolve => { started = resolve; });
  const close = new Promise(resolve => { closed = resolve; });
  const base = await setup(t, req => {
    req.on("data", started);
    req.on("aborted", closed);
  });
  const client = httpRequest(base, { method: "POST" });
  client.on("error", () => {});
  client.write("incomplete");
  await start;
  client.destroy();
  await close;
});

test("a slow backend applies backpressure to the upload", async t => {
  let resume;
  const base = await setup(t, (req, res) => {
    req.pause();
    resume = () => { req.resume(); req.on("end", () => res.end("ok")); };
  });
  const client = httpRequest(base, { method: "POST" });
  const response = once(client, "response");
  let sent = 0;
  const block = Buffer.alloc(64 * 1024);
  const writing = (async () => {
    for (let i = 0; i < 512; i++) {
      sent++;
      if (!client.write(block)) await once(client, "drain");
    }
    client.end();
  })();
  await new Promise(resolve => setTimeout(resolve, 200));
  assert(resume);
  assert(sent < 512, "The proxy consumed the whole 32MiB body while the backend was paused");
  resume();
  await writing;
  const [reply] = await response;
  reply.resume();
  assert.equal(reply.statusCode, 200);
});

test("unreachable backend produces a structured 502", async t => {
  const base = await setup(t, (req, res) => { req.resume(); res.end(); });
  process.env.BACKEND_INTERNAL_URL = "http://127.0.0.1:1";
  const response = await fetch(base, { method: "POST", body: "fixture" });
  assert.equal(response.status, 502);
  assert.equal((await response.json()).detail.code, "upload_proxy_unavailable");
});

test("accepts the exact declared request limit without buffering its body", async t => {
  const base = await setup(t, (req, res) => {
    assert.equal(Number(req.headers["content-length"]), MAX_UPLOAD_REQUEST_BYTES);
    res.end("accepted");
  });
  const client = httpRequest(base, { method: "POST", headers: { "content-length": MAX_UPLOAD_REQUEST_BYTES } });
  const response = once(client, "response");
  client.flushHeaders();
  const [reply] = await response;
  let body = "";
  for await (const chunk of reply) body += chunk;
  assert.equal(reply.statusCode, 200);
  assert.equal(body, "accepted");
  client.destroy();
});

test("an early rejection releases the unfinished upstream upload", async t => {
  let aborted;
  const closed = new Promise(resolve => { aborted = resolve; });
  const base = await setup(t, (req, res) => {
    req.socket.once("close", aborted);
    req.resume();
    res.writeHead(503, { "retry-after": "10" });
    res.end("maintenance");
  });
  const client = httpRequest(base, { method: "POST", headers: { "content-length": 1000 } });
  client.on("error", () => {});
  const response = once(client, "response");
  client.write("partial");
  const [reply] = await response;
  reply.resume();
  await once(reply, "end");
  assert.equal(reply.statusCode, 503);
  await closed;
  client.destroy();
});
