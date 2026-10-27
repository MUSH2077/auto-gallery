import assert from "node:assert/strict";
import test from "node:test";
import { parseTaskListRoute, updatedTaskListHref } from "../src/features/gallery/taskListRoute.ts";

test("legacy tab, view, and filters preserve exact filter identifiers", () => {
  const route = parseTaskListRoute(new URLSearchParams("tab=downloads&view=attention&q=creator%3Afoo&p=3&job=download-1&subscription_source_id=source-1"));
  assert.deepEqual(route, { q: "creator:foo", search: "creator:foo", kind: "download", records: "download", status: "", source: "", sort: "created-desc", order: "", scope: "attention", page: 3, taskId: null, downloadJobId: "download-1", importJobId: null, downloadJobFilterId: null, subscriptionSourceId: "source-1" });
});

test("legacy page and import filter remain distinct from a job deep link", () => {
  const route = parseTaskListRoute(new URLSearchParams("tab=imports&page=4&download_job_id=download-1&task=task-1&job=download-2&import_job=import-1"));
  assert.equal(route.page, 3);
  assert.equal(route.q, "");
  assert.equal(route.taskId, "task-1");
  assert.equal(route.downloadJobId, "download-2");
  assert.equal(route.importJobId, "import-1");
  assert.equal(route.downloadJobFilterId, "download-1");
});

test("changing a gallery filter removes legacy tab and pagination fallbacks", () => {
  assert.equal(updatedTaskListHref("/admin/jobs", new URLSearchParams("tab=downloads&view=attention&page=4&download_job_id=old"), { kind: null }, true), "/admin/jobs?scope=attention");
  assert.equal(updatedTaskListHref("/admin/jobs", new URLSearchParams("tab=downloads&page=4"), { scope: "running" }, true), "/admin/jobs?kind=download&records=download&scope=running");
});


test("pagination preserves legacy foreign-key filters while normalizing aliases", () => {
 const next=updatedTaskListHref("/admin/jobs",new URLSearchParams("tab=imports&page=4&download_job_id=original&view=attention"),{p:"4"});
 const query=new URL(next,"http://localhost").searchParams;
 assert.equal(query.get("kind"),"import");assert.equal(query.get("scope"),"attention");assert.equal(query.get("download_job_id"),"original");assert.equal(query.get("p"),"4");
 assert.equal(query.has("tab"),false);assert.equal(query.has("page"),false);
});


test("record tabs preserve exact status/source/sort controls", () => {
 const route = parseTaskListRoute(new URLSearchParams("tab=downloads&status=failed&source=pixiv&sort=updated-desc&order=desc"));
 assert.equal(route.kind, "download"); assert.equal(route.records, "download"); assert.equal(route.status, "failed"); assert.equal(route.source, "pixiv"); assert.equal(route.sort, "updated-desc"); assert.equal(route.order, "desc");
 const href = updatedTaskListHref("/admin/jobs", new URLSearchParams("tab=downloads&status=failed&source=pixiv"), { sort: "created-asc" }, true);
 const next = new URL(href, "http://localhost").searchParams;
 assert.equal(next.get("kind"), "download"); assert.equal(next.get("status"), "failed"); assert.equal(next.get("source"), "pixiv"); assert.equal(next.get("sort"), "created-asc");
});


test("explicit task kind remains TaskRun while legacy tab opts into record mode", () => {
 const task = parseTaskListRoute(new URLSearchParams("kind=download&scope=running"));
 assert.equal(task.kind, "download"); assert.equal(task.records, ""); assert.equal(task.scope, "running");
 const record = parseTaskListRoute(new URLSearchParams("tab=downloads&scope=running"));
 assert.equal(record.kind, "download"); assert.equal(record.records, "download");
 const promoted = new URL(updatedTaskListHref("/admin/jobs", new URLSearchParams("tab=downloads"), { scope: "all" }), "http://localhost").searchParams;
 assert.equal(promoted.get("records"), "download");
});


test("foreign-key compatibility routes infer records mode without stealing plain kinds", () => {
 const imp = parseTaskListRoute(new URLSearchParams("kind=import&download_job_id=parent"));
 assert.equal(imp.records, "import");
 const dl = parseTaskListRoute(new URLSearchParams("kind=download&subscription_source_id=repo"));
 assert.equal(dl.records, "download");
 const plain = parseTaskListRoute(new URLSearchParams("kind=download"));
 assert.equal(plain.records, "");
});
