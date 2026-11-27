// node --test: the assistant's classifier and slot extraction
// (`npm run test:unit`).
import { test } from "node:test";
import assert from "node:assert/strict";
import { foldToken, tokenize } from "./tokenize.ts";
import { extractSlots, pageOfWord, topicOfWord } from "./slots.ts";
import { classify } from "./match.ts";
import { MIN_CONFIDENCE } from "../model/intents.ts";

test("foldToken maps plurals and verb endings to one stem", () => {
  assert.equal(foldToken("proxies"), "proxy");
  assert.equal(foldToken("profiles"), "profile");
  assert.equal(foldToken("launching"), "launch");
  assert.equal(foldToken("launched"), "launch");
  assert.equal(foldToken("started"), "start");
  assert.equal(foldToken("running"), "run");
  assert.equal(foldToken("glass"), "glass");
  assert.equal(foldToken("is"), "is");
});

test("tokenize keeps quoted spans as names and folds the rest", () => {
  const tok = tokenize("start “FB Ads” and sandbox-02");
  assert.deepEqual(tok.quotes, ["FB Ads"]);
  assert.ok(tok.tokens.includes("start"));
  assert.ok(tok.tokens.includes("sandbox"));
});

test("extractSlots reads names, counts, pages, topics and the all word", () => {
  const s = extractSlots('start the selected 3 in “Sandbox” and go to settings, explain the fingerprint');
  assert.deepEqual(s.names, ["Sandbox"]);
  assert.equal(s.count, 3);
  assert.equal(s.page, "settings");
  assert.equal(s.topic, "fingerprint");
  assert.ok(s.selected);
  assert.ok(!s.all);
  const all = extractSlots("stop everything");
  assert.ok(all.all);
  assert.equal(pageOfWord("proxies"), "proxies");
  assert.equal(pageOfWord("shop"), "proxyshard");
  assert.equal(topicOfWord("cookies"), "profile");
  assert.equal(topicOfWord("zzz"), null);
});

test("plain asks land on their intent over the floor", () => {
  const bank: [string, string][] = [
    ["hello there", "greet"],
    ["hi!", "greet"],
    ["what can you do?", "help"],
    ["help me please", "help"],
    ["how many profiles do I have?", "countProfiles"],
    ["give me stats", "countProfiles"],
    ["what is running now?", "runningNow"],
    ["which sessions are open?", "runningNow"],
    ["start sandbox", "startProfile"],
    ["run FB Ads and etsy", "startProfile"],
    ["launch the selected", "startProfile"],
    ["stop sandbox", "stopProfile"],
    ["shut them down", "stopProfile"],
    ["close everything", "stopProfile"],
    ["find sandbox", "findProfile"],
    ["where is etsy?", "findProfile"],
    ["show me the ads ones", "findProfile"],
    ["how is proxy health?", "proxyHealth"],
    ["which proxy is fastest?", "proxyHealth"],
    ["assign a proxy to sandbox", "assignProxy"],
    ["create a new profile", "createProfile"],
    ["add another profile", "createProfile"],
    ["go to settings", "openPage"],
    ["open the proxies page", "openPage"],
    ["what is a fingerprint?", "explain"],
    ["explain how sync works", "explain"],
  ];
  for (const [text, want] of bank) {
    const m = classify(text);
    assert.equal(m.intent, want, `${text} -> ${m.intent} (${m.confidence.toFixed(2)})`);
    assert.ok(m.confidence >= MIN_CONFIDENCE, `${text} only ${m.confidence.toFixed(2)}`);
  }
});

test("a follow-up keeps the verb of the turn before", () => {
  const first = classify("start sandbox and etsy");
  assert.equal(first.intent, "startProfile");
  const follow = classify("stop them too", first.intent);
  assert.equal(follow.intent, "stopProfile");
});

test("noise does not fake an intent", () => {
  const m = classify("purple monkey dishwasher");
  assert.ok(m.confidence < MIN_CONFIDENCE, `${m.confidence}`);
  assert.equal(m.intent, "unknown");
});

test("runners-up name the second reading", () => {
  const m = classify("start the proxies page");
  assert.ok(m.runnersUp.length > 0);
  assert.ok(m.runnersUp[0].confidence <= m.confidence);
});
