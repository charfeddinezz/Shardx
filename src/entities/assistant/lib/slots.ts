// Slot extraction: what a sentence names. Pure functions over the sentence
// and small lookup tables (slots.test.ts). Names come out as typed; the
// engine resolves them against the real profiles with the fuzzy ranker.

import type { Section } from "../../../shared/types";
import type { Slots } from "../model/types";
import { tokenize, type Tokenized } from "./tokenize.ts";

/** Page words to sections. "shop" is the ProxyShard store page. */
const PAGES: readonly { words: readonly string[]; page: Section }[] = [
  { words: ["profile", "profiles", "browsers", "home", "main"], page: "profiles" },
  { words: ["proxy", "proxies"], page: "proxies" },
  { words: ["shop", "shardx", "store", "buy"], page: "proxyshard" },
  { words: ["fingerprint", "fingerprints", "prints"], page: "fingerprints" },
  { words: ["extension", "extensions"], page: "extensions" },
  { words: ["bookmark", "bookmarks"], page: "bookmarks" },
  { words: ["automation", "automations", "project", "projects", "flows"], page: "automation" },
  { words: ["trash", "bin", "recycle"], page: "trash" },
  { words: ["setting", "settings", "preferences", "options"], page: "settings" },
  { words: ["team", "teams", "space"], page: "team" },
];

/** Topic words to knowledge ids (model/knowledge.ts). */
const TOPICS: readonly { words: readonly string[]; topic: string }[] = [
  { words: ["profile", "profiles", "identity", "identities", "cookie", "cookies"], topic: "profile" },
  { words: ["fingerprint", "fingerprints", "canvas", "webgl", "webgpu", "noise", "spoofing"], topic: "fingerprint" },
  { words: ["proxy", "proxies", "socks", "socks5", "ip"], topic: "proxy" },
  { words: ["engine", "chromium", "browser", "patched"], topic: "engine" },
  { words: ["folder", "folders", "group", "groups"], topic: "folder" },
  { words: ["view", "views", "filters", "sorting"], topic: "view" },
  { words: ["sync", "team", "teams", "server", "conflict"], topic: "sync" },
  { words: ["api", "mcp", "sdk", "cdp", "token", "jwt"], topic: "api" },
  { words: ["trash", "restore", "restoring"], topic: "trash" },
];

const ALL = new Set(["all", "everything", "every", "entire"]);
const SELECTED = new Set(["selected", "selection", "them", "those", "these", "chosen"]);
/** Verbs whose object names a page ("go to settings"): the word after one
 *  wins over a page word said earlier ("go to settings, explain the
 *  fingerprint" goes to Settings). */
const MOTION = new Set(["go", "open", "navigate", "show", "take", "head", "visit"]);
const NUMBER_WORDS: Record<string, number> = {
  one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10,
  eleven: 11, twelve: 12, twenty: 20, fifty: 50, hundred: 100,
};

export const emptySlots = (): Slots => ({ names: [], count: null, page: null, topic: null, all: false, selected: false });

/** Everything the sentence names: quoted names, a count, a page, a topic,
 *  and whether it said "all" or "the selected". */
export function extractSlots(raw: string): Slots & { tokenized: Tokenized } {
  const tok = tokenize(raw);
  const slots = emptySlots();
  slots.names = [...tok.quotes];
  // Slot markers and numbers read from the words as typed: "selected" must
  // not fold to "select" away from its table, and "ones" must not read as
  // the count one.
  for (const token of tok.rawTokens) {
    if (ALL.has(token)) slots.all = true;
    if (SELECTED.has(token)) slots.selected = true;
    const n = NUMBER_WORDS[token] ?? (/^\d{1,4}$/.test(token) ? Number(token) : null);
    if (n !== null && slots.count === null) slots.count = n;
  }
  // A motion verb's object names the page; else the last page word wins
  // ("go to the team page from settings" means team).
  for (let i = 0; i < tok.rawTokens.length; i++) {
    if (!MOTION.has(tok.rawTokens[i])) continue;
    for (let j = i + 1; j < Math.min(i + 4, tok.rawTokens.length); j++) {
      const page = pageOfWord(tok.rawTokens[j]);
      if (page) slots.page = page;
    }
  }
  if (!slots.page) {
    for (const token of tok.tokens) {
      for (const row of PAGES) if (row.words.includes(token)) slots.page = row.page;
    }
  }
  for (const token of tok.tokens) {
    for (const row of TOPICS) if (row.words.includes(token)) slots.topic = row.topic;
  }
  return { ...slots, tokenized: tok };
}

/** The page a word names, for tests and the engine's page lookups. */
export function pageOfWord(word: string): Section | null {
  for (const row of PAGES) if (row.words.includes(word)) return row.page;
  return null;
}

/** The knowledge id a word names. */
export function topicOfWord(word: string): string | null {
  for (const row of TOPICS) if (row.words.includes(word)) return row.topic;
  return null;
}
