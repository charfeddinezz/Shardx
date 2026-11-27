// node --test: the assistant database checker (`npm run test:scripts`).
import { test } from "node:test";
import assert from "node:assert/strict";
import { checkAssistant } from "./check-assistant.mjs";

const EN = {
  "assistant.reply.a": "Answer a",
  "assistant.reply.unknown": "Unknown",
  "assistant.suggest.q": "do the thing",
  "assistant.kb.x": "X is x",
  "assistant.page.profiles": "the profiles page",
};

const intents = [
  { id: "a", reply: "assistant.reply.a", words: [{ w: "thing", k: 1 }], suggestions: ["assistant.suggest.q"] },
  { id: "unknown", reply: "assistant.reply.unknown", words: [] },
];
const knowledge = [{ id: "x", answer: "assistant.kb.x", terms: ["thing", "x"] }];
const pageKeys = { profiles: "assistant.page.profiles" };

// A classifier stand-in: it reads "thing" (the chip's words) as intent "a",
// and anything else as nothing.
const classify = (text) => (/thing/.test(text)
  ? { intent: "a", confidence: 0.9, slots: null, runnersUp: [] }
  : { intent: "unknown", confidence: 0.1, slots: null, runnersUp: [] });

test("a well-formed database passes", () => {
  const problems = checkAssistant({ en: EN, intents, knowledge, pageKeys, classify });
  assert.deepEqual(problems, []);
});

test("a reply key the locale lacks is named", () => {
  const missing = checkAssistant({ en: Object.fromEntries(Object.entries(EN).filter(([k]) => k !== "assistant.reply.a")), intents, knowledge, pageKeys, classify });
  assert.ok(missing.some((p) => p.where === "intent:a" && p.message.includes("assistant.reply.a")));
});

test("a duplicated intent id is named", () => {
  const problems = checkAssistant({ en: EN, intents: [...intents, intents[0]], knowledge, pageKeys, classify });
  assert.ok(problems.some((p) => p.message.includes("share one id")));
});

test("an intent with no words and no phrases is named", () => {
  const dead = [{ id: "a", reply: "assistant.reply.a", words: [] }, intents[1]];
  const problems = checkAssistant({ en: EN, intents: dead, knowledge, pageKeys, classify });
  assert.ok(problems.some((p) => p.where === "intent:a" && p.message.includes("never match")));
});

test("a suggestion whose line answers nothing is named", () => {
  const problems = checkAssistant({
    en: { ...EN, "assistant.suggest.q": "purple monkey" },
    intents, knowledge, pageKeys, classify,
  });
  assert.ok(problems.some((p) => p.where === "suggest:assistant.suggest.q"));
});

test("a knowledge entry without terms is named", () => {
  const problems = checkAssistant({ en: EN, intents, knowledge: [{ id: "x", answer: "assistant.kb.x", terms: [] }], pageKeys, classify });
  assert.ok(problems.some((p) => p.where === "kb:x" && p.message.includes("no terms")));
});

test("a page key the locale lacks is named", () => {
  const problems = checkAssistant({ en: EN, intents, knowledge, pageKeys: { profiles: "assistant.page.nope" }, classify });
  assert.ok(problems.some((p) => p.where === "page:profiles"));
});

test("the strongest word reading as another intent is named", () => {
  const cross = [{ id: "a", reply: "assistant.reply.a", words: [{ w: "other", k: 1 }] }, intents[1]];
  const reads = (text) => (text === "other"
    ? { intent: "unknown", confidence: 0.7, slots: null, runnersUp: [] }
    : classify(text));
  const problems = checkAssistant({ en: EN, intents: cross, knowledge, pageKeys, classify: reads });
  assert.ok(problems.some((p) => p.where === "intent:a" && p.message.includes("reads as")));
});
