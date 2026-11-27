// node --test: knowledge retrieval (`npm run test:unit`).
import { test } from "node:test";
import assert from "node:assert/strict";
import { retrieve } from "./knowledge.ts";

test("a question names its entry outright", () => {
  assert.equal(retrieve("what is a fingerprint?")?.id, "fingerprint");
  assert.equal(retrieve("explain proxies")?.id, "proxy");
  assert.equal(retrieve("how does sync work?")?.id, "sync");
  assert.equal(retrieve("what is the trash?")?.id, "trash");
});

test("a typo in the topic still lands", () => {
  assert.equal(retrieve("what is a fingreprint?")?.id, "fingerprint");
});

test("a longer question falls to the closest terms", () => {
  assert.equal(retrieve("can I use cookies and storage in a profile?")?.id, "profile");
  assert.equal(retrieve("does the patched chromium engine update itself?")?.id, "engine");
});

test("a lone short word names nothing", () => {
  assert.equal(retrieve("a"), null);
  assert.equal(retrieve(""), null);
});

test("noise without terms returns null", () => {
  assert.equal(retrieve("purple monkey dishwasher"), null);
});
