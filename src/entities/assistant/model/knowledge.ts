// The local knowledge base: what the assistant answers when a question is
// about the app itself. Plain data with locale keys, retrieved offline by
// TF-IDF over the question words (lib/knowledge.ts). Nothing here reaches the
// network: the answers are the launcher's own documentation, in the UI's
// language.

export type KnowledgeEntry = {
  /** Stable id, also the doc's face in tests. */
  id: string;
  /** The answer's locale key (`assistant.kb.*`). */
  answer: string;
  /** The question words this entry answers, in English (retrieval normalizes). */
  terms: readonly string[];
};

/** One line of help per topic, the way CONTEXT.md keeps the words. */
export const KNOWLEDGE: readonly KnowledgeEntry[] = [
  {
    id: "profile",
    answer: "assistant.kb.profile",
    terms: ["profile", "profiles", "identity", "account", "cookie", "cookies", "storage", "what", "is"],
  },
  {
    id: "fingerprint",
    answer: "assistant.kb.fingerprint",
    terms: ["fingerprint", "fingerprints", "canvas", "webgl", "webgpu", "noise", "spoof", "signals", "authenticity", "what", "is"],
  },
  {
    id: "proxy",
    answer: "assistant.kb.proxy",
    terms: ["proxy", "proxies", "socks5", "http", "ip", "exit", "rotate", "assign", "what", "is"],
  },
  {
    id: "engine",
    answer: "assistant.kb.engine",
    terms: ["engine", "browser", "chromium", "patched", "update", "download", "version", "what", "is"],
  },
  {
    id: "folder",
    answer: "assistant.kb.folder",
    terms: ["folder", "folders", "group", "organize", "color", "client", "project", "what", "is"],
  },
  {
    id: "view",
    answer: "assistant.kb.view",
    terms: ["view", "views", "filter", "filters", "sort", "columns", "saved", "smart", "what", "is"],
  },
  {
    id: "sync",
    answer: "assistant.kb.sync",
    terms: ["sync", "team", "teams", "server", "computers", "devices", "open", "conflict", "share", "what", "is"],
  },
  {
    id: "api",
    answer: "assistant.kb.api",
    terms: ["api", "local", "http", "token", "jwt", "automation", "mcp", "sdk", "script", "cdp", "what", "is"],
  },
  {
    id: "trash",
    answer: "assistant.kb.trash",
    terms: ["trash", "restore", "deleted", "delete", "recover", "seven", "days", "what", "is"],
  },
];

/** One lookup by id. */
export function knowledgeOf(id: string): KnowledgeEntry | undefined {
  return KNOWLEDGE.find((e) => e.id === id);
}
