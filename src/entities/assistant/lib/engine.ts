// The turn engine: one message in, one bot turn out. Pure over the context
// snapshot (engine.test.ts), so the whole dialog logic runs in unit tests and
// in scripts. The reply carries a locale key and its variables; the store
// resolves the words, the widget draws the cards, and nothing runs until a
// card is pressed.
//
// The turn order:
// 1. classify the sentence (lib/match.ts), keeping the slots;
// 2. a question about the app goes to the knowledge base (lib/knowledge.ts),
//    whatever the classifier liked, since "what is a proxy" carries
//    "assign a proxy" words too;
// 3. an intent over the floor builds its answer from the workspace. Rows are
//    named by quotes ("FB Ads") or by the sentence's leftover words ("sandbx"
//    finds "Sandbox" through the fuzzy ranker);
// 4. below the floor, with no knowledge hit, the unknown answer offers ways on.

import { INTENTS, MIN_CONFIDENCE, intentOf } from "../model/intents.ts";
import type { AssistantAction, AssistantContext, AssistantProfile, IntentId, MatchResult, Slots } from "../model/types";
import type { Section } from "../../../shared/types";
import { rankProxyRows, resolveProfiles, runningProfiles, summarize } from "./context.ts";
import { retrieve } from "./knowledge.ts";
import { classify } from "./match.ts";
import { extractSlots } from "./slots.ts";
import { foldToken, type Tokenized } from "./tokenize.ts";

export type ReplySpec = { key: string; vars?: Record<string, string | number> };

export type BotTurn = {
  intent: IntentId;
  confidence: number;
  reply: ReplySpec;
  /** Set when the knowledge base answered instead of an intent. */
  knowledgeId?: string;
  actions: AssistantAction[];
  suggestions: string[];
  /** The rows this turn talked about, for the next turn's "stop them". */
  targetIds: string[];
};

export type TurnInput = {
  text: string;
  /** The intent that answered the turn before (follow-ups). */
  prevIntent?: IntentId | null;
  /** The rows the turn before talked about, when the selection is empty. */
  prevTargetIds?: readonly string[];
};

/** Words that carry no name: grammar around the sentence's subject. */
const STOP = new Set([
  "the", "a", "an", "to", "me", "my", "i", "you", "please", "now", "too", "also", "and", "or", "of", "in", "on", "for",
  "is", "are", "was", "do", "does", "did", "can", "could", "would", "should", "it", "this", "that", "this", "with",
  "at", "by", "from", "get", "give", "take", "put", "one", "ones", "some", "any", "up", "down", "them", "they",
]);

/** Each page's own name key (`assistant.page.*`): a reply says "Opening
 *  {page}" with the name, not the section id. The store resolves the key. */
export const PAGE_NAME_KEYS: Record<Section, string> = {
  profiles: "assistant.page.profiles",
  proxies: "assistant.page.proxies",
  proxyshard: "assistant.page.proxyshard",
  fingerprints: "assistant.page.fingerprints",
  extensions: "assistant.page.extensions",
  bookmarks: "assistant.page.bookmarks",
  automation: "assistant.page.automation",
  trash: "assistant.page.trash",
  settings: "assistant.page.settings",
  team: "assistant.page.team",
};

/** Every folded trigger word of every intent: what a sentence's subject is
 *  not. What is left names the rows ("start sandbx" leaves "sandbx"). */
const TRIGGERS = new Set(INTENTS.flatMap((i) => i.words.map((w) => foldToken(w.w))));

/** The sentence's leftover words: no trigger, no stop word, no slot marker,
 *  no number. Each becomes one name query. */
function residualWords(tok: Tokenized): string[] {
  return tok.tokens.filter(
    (token) => !TRIGGERS.has(token) && !STOP.has(token) && token.length > 1 && !/^\d+$/.test(token),
  );
}

/** Names in a row's order, "A, B, C", cut after `max` with a count left over. */
function nameList(profiles: readonly AssistantProfile[], max = 5): string {
  const names = profiles.slice(0, max).map((p) => p.name);
  const more = profiles.length - names.length;
  return more > 0 ? `${names.join(", ")} +${more}` : names.join(", ");
}

/** The suggestions of an intent's definition. */
function suggestionsOf(id: IntentId): string[] {
  return [...(intentOf(id).suggestions ?? [])];
}

const ASK_WHICH: ReplySpec = { key: "assistant.reply.askWhich" };

/** Builds one bot turn. `ctx` is the workspace as this turn sees it. */
export function interpret(input: TurnInput, ctx: AssistantContext): BotTurn {
  const slots = extractSlots(input.text);
  const match: MatchResult = classify(input.text, input.prevIntent, slots);
  const queries = [...slots.names, ...residualWords(slots.tokenized)];

  // The knowledge base first for a real question, and for anything the
  // classifier could not read.
  const wantsExplain = match.intent === "explain" && match.confidence >= MIN_CONFIDENCE;
  const unsure = match.confidence < MIN_CONFIDENCE;
  if (wantsExplain || unsure) {
    const hit = retrieve(input.text);
    if (hit) {
      return {
        intent: "explain",
        confidence: match.confidence,
        reply: { key: hit.answer },
        knowledgeId: hit.id,
        actions: [],
        suggestions: suggestionsOf("explain"),
        targetIds: [],
      };
    }
    if (wantsExplain) {
      return turn(
        match,
        { key: "assistant.reply.explainNone", vars: { topic: queries.join(" ") || input.text.trim() } },
        suggestionsOf("explain"),
        [],
        [],
      );
    }
    return unknownTurn();
  }

  const def = intentOf(match.intent);
  const summary = summarize(ctx);
  switch (match.intent) {
    case "greet":
      return turn(match, { key: def.reply }, suggestionsOf("greet"));
    case "help":
      return turn(match, { key: def.reply }, suggestionsOf("help"));
    case "countProfiles":
      return turn(match, { key: def.reply, vars: { ...summary } }, suggestionsOf("countProfiles"));
    case "runningNow": {
      const running = runningProfiles(ctx);
      const actions: AssistantAction[] = running.length
        ? [
            { kind: "filterProfiles", ids: running.map((p) => p.id), label: nameList(running, 3) },
            { kind: "stopProfiles", ids: running.map((p) => p.id), names: running.map((p) => p.name) },
          ]
        : [];
      return turn(
        match,
        { key: running.length ? def.reply : "assistant.reply.runningNone", vars: { names: nameList(running), n: running.length } },
        suggestionsOf("runningNow"),
        actions,
        running.map((p) => p.id),
      );
    }
    case "startProfile":
      return startStopTurn(match, ctx, queries, input, "start");
    case "stopProfile":
      return startStopTurn(match, ctx, queries, input, "stop");
    case "findProfile": {
      const targets = pickTargets(slots, ctx, queries, input.prevTargetIds);
      if (targets === "ask") return turn(match, ASK_WHICH, suggestionsOf("findProfile"));
      if (targets.length === 0) {
        return turn(match, { key: "assistant.reply.findNone", vars: { query: queries.join(", ") } }, suggestionsOf("findProfile"));
      }
      return turn(
        match,
        { key: "assistant.reply.findNamed", vars: { names: nameList(targets), n: targets.length } },
        suggestionsOf("findProfile"),
        [{ kind: "filterProfiles", ids: targets.map((p) => p.id), label: nameList(targets, 3) }],
        targets.map((p) => p.id),
      );
    }
    case "proxyHealth": {
      if (ctx.proxies.length === 0) {
        return turn(match, { key: "assistant.reply.proxyHealthEmpty" }, suggestionsOf("proxyHealth"));
      }
      const ranked = rankProxyRows(ctx.proxies);
      return turn(
        match,
        {
          key: summary.failedProxies > 0 ? def.reply : "assistant.reply.proxyHealthAllOk",
          vars: { n: ctx.proxies.length, names: ranked.slice(0, 3).map((r) => r.proxy.name).join(", "), failed: summary.failedProxies },
        },
        suggestionsOf("proxyHealth"),
        [{ kind: "runCommand", commandId: "proxy.test" }],
      );
    }
    case "assignProxy": {
      const targets = pickTargets(slots, ctx, queries, input.prevTargetIds);
      const rows = targets === "ask" ? [] : targets;
      return turn(
        match,
        { key: def.reply },
        suggestionsOf("assignProxy"),
        rows.length ? [{ kind: "openAssign", ids: rows.map((p) => p.id) }] : [],
        rows.map((p) => p.id),
      );
    }
    case "createProfile":
      return turn(match, { key: def.reply }, suggestionsOf("createProfile"), [{ kind: "runCommand", commandId: "profile.new" }]);
    case "openPage": {
      if (!slots.page) return turn(match, ASK_WHICH, suggestionsOf("openPage"));
      return turn(match, { key: def.reply, vars: { page: PAGE_NAME_KEYS[slots.page] } }, suggestionsOf("openPage"), [{ kind: "navigate", section: slots.page }]);
    }
    default:
      return unknownTurn();
  }
}

/** The rows a start, stop or find talks about: named rows (quotes or the
 *  leftover words), "all", "the selected", a bare count, else the rows the
 *  turn before named ("them"); "ask" when the sentence named nothing. */
function pickTargets(
  slots: Slots,
  ctx: AssistantContext,
  queries: readonly string[],
  prevTargetIds?: readonly string[],
): AssistantProfile[] | "ask" {
  if (slots.all) return [...ctx.profiles];
  if (slots.selected) {
    const rows = ctx.selectedIds.length
      ? ctx.profiles.filter((p) => ctx.selectedIds.includes(p.id))
      : ctx.profiles.filter((p) => (prevTargetIds ?? []).includes(p.id));
    return rows.length ? rows : "ask";
  }
  const named = resolveProfiles(queries, ctx.profiles);
  if (named.length) return named;
  // A count with no names takes the first rows ("start 3").
  if (slots.count !== null && slots.count > 0) return ctx.profiles.slice(0, slots.count);
  if (queries.length === 0) return "ask";
  return [];
}

/** One answer for both verbs, since the differences are the keys and cards. */
function startStopTurn(
  match: MatchResult,
  ctx: AssistantContext,
  queries: readonly string[],
  input: TurnInput,
  verb: "start" | "stop",
): BotTurn {
  const slots = match.slots;
  const targets = pickTargets(slots, ctx, queries, input.prevTargetIds);
  const suggestions = suggestionsOf(verb === "start" ? "startProfile" : "stopProfile");
  if (targets === "ask") return turn(match, ASK_WHICH, suggestions);
  if (targets.length === 0) {
    return turn(match, { key: "assistant.reply.findNone", vars: { query: queries.join(", ") } }, suggestions);
  }
  const byName = !slots.all && !slots.selected && slots.count === null;
  const key =
    verb === "start"
      ? slots.all ? "assistant.reply.startAll" : byName ? "assistant.reply.startNamed" : "assistant.reply.startSelected"
      : slots.all ? "assistant.reply.stopAll" : byName ? "assistant.reply.stopNamed" : "assistant.reply.stopSelected";
  const action: AssistantAction =
    verb === "start"
      ? { kind: "startProfiles", ids: targets.map((p) => p.id), names: targets.map((p) => p.name) }
      : { kind: "stopProfiles", ids: targets.map((p) => p.id), names: targets.map((p) => p.name) };
  return turn(match, { key, vars: { names: nameList(targets), n: targets.length } }, suggestions, [action], targets.map((p) => p.id));
}

function unknownTurn(): BotTurn {
  const def = intentOf("unknown");
  return { intent: "unknown", confidence: 0, reply: { key: def.reply }, actions: [], suggestions: suggestionsOf("unknown"), targetIds: [] };
}

function turn(
  match: MatchResult,
  reply: ReplySpec,
  suggestions: string[],
  actions: AssistantAction[] = [],
  targetIds: string[] = [],
): BotTurn {
  return { intent: match.intent, confidence: match.confidence, reply, actions, suggestions, targetIds };
}
