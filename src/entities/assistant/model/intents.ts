// The intent database: what words lead to which answer. Plain data plus one
// scorer's constants, so scripts/check-assistant.mjs can validate it against
// the locales and match.test.ts can score it offline. Reply and suggestion
// keys are plain strings here (like the tour's step keys); check-i18n counts
// them as called, and the checker script proves each one is in en.json.
//
// Matching is weighted keyword evidence, not a model: each trigger word adds
// its weight, phrases add a flat bonus, and the previous intent adds its
// continuity weight so follow-ups ("stop them too") land right. The engine
// takes the best intent over `minConfidence`, else the knowledge base answers.

import type { IntentId, SlotKind } from "./types";

export type TriggerWord = { w: string; k: number };

export type IntentDef = {
  id: IntentId;
  /** The reply key (`assistant.reply.*`). */
  reply: string;
  /** Trigger words with the evidence each adds. */
  words: readonly TriggerWord[];
  /** Whole phrases that alone make a strong case (normalized, lowercase). */
  phrases?: readonly string[];
  /** Evidence added when this intent answered the turn before. */
  continuity?: number;
  /** Slots this intent reads. */
  slots?: readonly SlotKind[];
  /** Follow-up chips (`assistant.suggest.*` keys). */
  suggestions?: readonly string[];
};

/** Evidence below this is not an answer: the knowledge base takes over. */
export const MIN_CONFIDENCE = 0.42;

/** Evidence above this is a sure reading (an exact phrase). */
export const STRONG = 1;

/** Counts as an answer after scaling; `scale` shrinks long-sentence noise. */
export const scaled = (evidence: number, tokens: number): number =>
  Math.max(0, Math.min(1, evidence / (0.9 + tokens * 0.045)));

const W = (w: string, k = 1): TriggerWord => ({ w, k });

export const INTENTS: readonly IntentDef[] = [
  {
    id: "greet",
    reply: "assistant.reply.greet",
    words: [W("hello", 0.8), W("hi", 0.7), W("hey", 0.7), W("salam", 0.8), W("bonjour", 0.8), W("merhaba", 0.8), W("ciao", 0.7)],
    phrases: ["good morning", "good evening", "how are you"],
    continuity: 0.15,
    suggestions: ["assistant.suggest.help", "assistant.suggest.running", "assistant.suggest.count"],
  },
  {
    id: "help",
    reply: "assistant.reply.help",
    words: [W("help", 0.9), W("can", 0.25), W("do", 0.2), W("what", 0.2), W("abilities", 0.8), W("commands", 0.5), W("features", 0.5)],
    phrases: ["what can you do", "help me", "how do you work", "what do you know"],
    continuity: 0.2,
    suggestions: ["assistant.suggest.start", "assistant.suggest.health", "assistant.suggest.explainFingerprint"],
  },
  {
    id: "countProfiles",
    reply: "assistant.reply.countProfiles",
    words: [W("how", 0.3), W("many", 0.9), W("count", 0.8), W("total", 0.6), W("number", 0.6), W("stats", 0.7), W("statistics", 0.7)],
    phrases: ["how many", "how much", "count them", "give me stats", "workspace summary"],
    continuity: 0.25,
    slots: ["names"],
    suggestions: ["assistant.suggest.running", "assistant.suggest.health", "assistant.suggest.count"],
  },
  {
    id: "runningNow",
    reply: "assistant.reply.runningNow",
    words: [W("running", 1), W("started", 0.7), W("open", 0.4), W("active", 0.6), W("sessions", 0.8), W("which", 0.3), W("now", 0.3)],
    phrases: ["what is running", "which are running", "running now", "open sessions"],
    continuity: 0.3,
    suggestions: ["assistant.suggest.stop", "assistant.suggest.count", "assistant.suggest.start"],
  },
  {
    id: "startProfile",
    reply: "assistant.reply.startNamed",
    words: [W("start", 1), W("run", 0.9), W("open", 0.5), W("begin", 0.7), W("fire", 0.5), W("launch", 0.8), W("up", 0.2)],
    phrases: ["start them", "start all", "run them", "start the selected", "fire up"],
    continuity: 0.35,
    slots: ["names", "count", "all", "selected"],
    suggestions: ["assistant.suggest.stop", "assistant.suggest.running", "assistant.suggest.health"],
  },
  {
    id: "stopProfile",
    reply: "assistant.reply.stopNamed",
    words: [W("stop", 1), W("close", 0.7), W("kill", 0.8), W("end", 0.6), W("halt", 0.7), W("shut", 0.6), W("down", 0.2)],
    phrases: ["stop them", "stop all", "shut them down", "stop the selected", "close them"],
    continuity: 0.35,
    slots: ["names", "count", "all", "selected"],
    suggestions: ["assistant.suggest.start", "assistant.suggest.running", "assistant.suggest.count"],
  },
  {
    id: "findProfile",
    reply: "assistant.reply.findNamed",
    words: [W("find", 1), W("where", 0.8), W("show", 0.7), W("search", 0.9), W("locate", 0.9), W("look", 0.5), W("filter", 0.7), W("list", 0.5)],
    phrases: ["where is", "show me", "find the", "look for", "which profiles"],
    continuity: 0.3,
    slots: ["names"],
    suggestions: ["assistant.suggest.start", "assistant.suggest.count", "assistant.suggest.running"],
  },
  {
    id: "proxyHealth",
    reply: "assistant.reply.proxyHealth",
    // Bare "proxy" words carry little: "assign a proxy" is the assign
    // intent's sentence; health needs "health", "fastest" or the phrase.
    words: [W("proxy", 0.35), W("proxies", 0.4), W("health", 1), W("latency", 0.9), W("fastest", 0.9), W("best", 0.5), W("slow", 0.6), W("check", 0.4), W("speed", 0.7)],
    phrases: ["proxy health", "which proxy", "fastest proxy", "test the proxies", "proxy status"],
    continuity: 0.3,
    suggestions: ["assistant.suggest.explainProxy", "assistant.suggest.start", "assistant.suggest.count"],
  },
  {
    id: "assignProxy",
    reply: "assistant.reply.assignProxy",
    words: [W("assign", 1), W("bind", 0.9), W("attach", 0.8), W("change", 0.4), W("replace", 0.5), W("proxy", 0.5), W("proxies", 0.5)],
    phrases: ["assign a proxy", "change the proxy", "give them a proxy", "set the proxy"],
    continuity: 0.25,
    slots: ["names"],
    suggestions: ["assistant.suggest.health", "assistant.suggest.explainProxy", "assistant.suggest.start"],
  },
  {
    id: "createProfile",
    reply: "assistant.reply.createProfile",
    words: [W("create", 1), W("new", 0.8), W("add", 0.8), W("make", 0.7), W("profile", 0.5), W("profiles", 0.4)],
    phrases: ["new profile", "create a profile", "add a profile", "make a profile"],
    continuity: 0.2,
    suggestions: ["assistant.suggest.explainFingerprint", "assistant.suggest.start", "assistant.suggest.help"],
  },
  {
    id: "openPage",
    reply: "assistant.reply.openPage",
    words: [W("go", 0.8), W("open", 0.6), W("navigate", 1), W("page", 0.6), W("show", 0.3), W("settings", 0.5), W("proxies", 0.4), W("fingerprints", 0.4)],
    phrases: ["go to", "open the", "take me to", "navigate to"],
    continuity: 0.25,
    slots: ["page"],
    suggestions: ["assistant.suggest.count", "assistant.suggest.running", "assistant.suggest.help"],
  },
  {
    id: "explain",
    // The knowledge base answers these; this is the fallback when no entry
    // is close (lib/engine.ts knows which is which).
    reply: "assistant.reply.explainNone",
    words: [W("what", 0.5), W("explain", 1), W("meaning", 0.8), W("difference", 0.7), W("is", 0.2), W("are", 0.2), W("how", 0.3), W("why", 0.4), W("works", 0.4)],
    phrases: ["what is", "what are", "explain to me", "how does", "tell me about"],
    continuity: 0.3,
    slots: ["topic"],
    suggestions: ["assistant.suggest.explainFingerprint", "assistant.suggest.explainProxy", "assistant.suggest.explainSync"],
  },
  {
    id: "unknown",
    reply: "assistant.reply.unknown",
    words: [],
    continuity: 0,
    suggestions: ["assistant.suggest.help", "assistant.suggest.count", "assistant.suggest.running"],
  },
];

/** Intent ids as the database lists them, for checks against the type. */
export const INTENT_IDS: readonly IntentId[] = INTENTS.map((i) => i.id);

/** One lookup by id. */
export function intentOf(id: IntentId): IntentDef {
  return INTENTS.find((i) => i.id === id) ?? INTENTS[INTENTS.length - 1];
}
