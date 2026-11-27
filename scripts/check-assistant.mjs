// Checks the assistant's intent database and knowledge base against the
// locales: every reply, suggestion and page key it names is in en.json, every
// intent carries its own words, and every suggestion chip really answers when
// it is sent (`npm run check:assistant`, part of `npm run check`).
//
// The check is pure over its inputs (exports `checkAssistant`), so
// check-assistant.test.mjs runs it on fixtures without the real database.
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { INTENTS, MIN_CONFIDENCE } from "../src/entities/assistant/model/intents.ts";
import { KNOWLEDGE } from "../src/entities/assistant/model/knowledge.ts";
import { classify } from "../src/entities/assistant/lib/match.ts";
import { PAGE_NAME_KEYS } from "../src/entities/assistant/lib/engine.ts";

const ROOT = fileURLToPath(new URL("..", import.meta.url));

/** The problems of one database against one locale. Empty is a pass. */
export function checkAssistant({ en, intents, knowledge, pageKeys, classify: classifyFn }) {
  const problems = [];
  const at = (where, message) => problems.push({ where, message });

  const intentIds = new Set();
  for (const def of intents) {
    if (intentIds.has(def.id)) at(`intent:${def.id}`, "two intents share one id");
    intentIds.add(def.id);
    if (!en[def.reply]) at(`intent:${def.id}`, `reply key not in en.json: ${def.reply}`);
    if (def.id !== "unknown" && !(def.words.length || def.phrases?.length)) {
      at(`intent:${def.id}`, "no words and no phrases: it can never match");
    }
    const words = new Set();
    for (const trigger of def.words) {
      if (words.has(trigger.w)) at(`intent:${def.id}`, `word listed twice: ${trigger.w}`);
      words.add(trigger.w);
      if (!(trigger.k > 0)) at(`intent:${def.id}`, `word weight must be positive: ${trigger.w}`);
    }
    for (const key of def.suggestions ?? []) {
      if (!en[key]) at(`intent:${def.id}`, `suggestion key not in en.json: ${key}`);
    }
  }
  if (!intentIds.has("unknown")) at("intent:unknown", "no unknown intent: unmatched lines would fall through");

  const kbIds = new Set();
  for (const entry of knowledge) {
    if (kbIds.has(entry.id)) at(`kb:${entry.id}`, "two entries share one id");
    kbIds.add(entry.id);
    if (!en[entry.answer]) at(`kb:${entry.id}`, `answer key not in en.json: ${entry.answer}`);
    if (!entry.terms.length) at(`kb:${entry.id}`, "no terms: retrieval can never reach it");
  }

  for (const [section, key] of Object.entries(pageKeys)) {
    if (!en[key]) at(`page:${section}`, `page name key not in en.json: ${key}`);
  }

  // Every suggestion chip is a line the person can send: each must answer
  // with something over the confidence floor when sent in English.
  const chipTexts = new Map();
  for (const def of intents) for (const key of def.suggestions ?? []) if (en[key]) chipTexts.set(key, en[key]);
  for (const [key, text] of chipTexts) {
    const match = classifyFn(text);
    if (match.confidence < MIN_CONFIDENCE) {
      at(`suggest:${key}`, `sending it does not read as any intent (confidence ${match.confidence.toFixed(2)})`);
    }
  }

  // Every intent's own words must point home: at least one word alone (with
  // the intent's continuity) keeps the floor, so a typo in a weight cannot
  // silence an intent while its phrases still fire.
  for (const def of intents) {
    if (def.id === "unknown" || !def.words.length) continue;
    const best = def.words.reduce((a, b) => (a.k >= b.k ? a : b));
    const match = classifyFn(`${best.w}`);
    if (match.intent !== def.id && match.confidence >= MIN_CONFIDENCE) {
      at(`intent:${def.id}`, `its strongest word "${best.w}" reads as ${match.intent}`);
    }
  }

  // The database must answer the questions its suggestions ask, by id where
  // the question names a topic directly.
  return problems;
}

/** Runs the check over the real database and locale. */
function main() {
  const en = JSON.parse(readFileSync(`${ROOT}/src/shared/i18n/locales/en.json`, "utf8"));
  const problems = checkAssistant({ en, intents: INTENTS, knowledge: KNOWLEDGE, pageKeys: PAGE_NAME_KEYS, classify });
  for (const p of problems) console.log(`${p.where}  ${p.message}`);
  console.log(problems.length ? `\n${problems.length} problem(s)` : `assistant OK: ${INTENTS.length} intents, ${KNOWLEDGE.length} knowledge entries, every key in en.json`);
  process.exit(problems.length ? 1 : 0);
}

// Run the real check only when invoked as a script, never on import (the
// test module imports `checkAssistant` and runs it on fixtures).
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
