// The chat store: the dock's conversation, who said what and what the last
// turn understood (follow-ups). One turn is: the person's line, then either
// the local model (when the person turned it on) or the built-in brain
// (lib/engine.ts). The store resolves the reply keys with `t` at answer time
// and keeps the offered cards as data, so the widget stays a view of it.

import { create } from "zustand";
import { t } from "../../../shared/i18n";
import { interpret, type BotTurn } from "../lib/engine";
import { buildContext, reportInert, runAction } from "../lib/actions";
import { askLlm } from "../lib/llm";
import { factsOf } from "../lib/context";
import type { AssistantAction, ChatMessage, IntentId } from "./types";

let seq = 0;

type AssistantState = {
  open: boolean;
  busy: boolean;
  messages: ChatMessage[];
  /** What the last built-in turn understood, for "stop them too". */
  prevIntent: IntentId | null;
  /** The rows the last built-in turn named, when the selection is empty. */
  prevTargetIds: string[];
  setOpen: (open: boolean) => void;
  toggle: () => void;
  /** One turn: the person's line, then one answer. */
  send: (text: string) => Promise<void>;
  /** Runs an offered card; reports when it did nothing. */
  runCard: (action: AssistantAction) => Promise<void>;
  clear: () => void;
};

export const useAssistant = create<AssistantState>((set, get) => ({
  open: false,
  busy: false,
  messages: [],
  prevIntent: null,
  prevTargetIds: [],
  setOpen: (open) => set({ open }),
  toggle: () => set((s) => ({ open: !s.open })),
  send: async (text) => {
    const line = text.trim();
    if (!line || get().busy) return;
    const mine: ChatMessage = { id: seq++, role: "user", text: line };
    set((s) => ({ messages: [...s.messages, mine], busy: true }));
    try {
      const ctx = buildContext();
      // The local model first, when the person turned it on; any failure
      // falls through to the built-in brain in the same turn.
      const viaLlm = await askLlm(line, factsOf(ctx));
      if (viaLlm) {
        set((s) => ({ messages: [...s.messages, { id: seq++, role: "assistant", text: viaLlm, viaLlm: true }], busy: false }));
        return;
      }
      const turn: BotTurn = interpret({ text: line, prevIntent: get().prevIntent, prevTargetIds: get().prevTargetIds }, ctx);
      // A `page` var is the page's name key ("assistant.page.settings"):
      // resolve it here, where the words are chosen.
      const vars = turn.reply.vars ? { ...turn.reply.vars } : undefined;
      if (vars && typeof vars.page === "string") vars.page = t(vars.page);
      const reply: ChatMessage = {
        id: seq++,
        role: "assistant",
        text: t(turn.reply.key, vars),
        actions: turn.actions.length ? turn.actions : undefined,
        suggestions: turn.suggestions.length ? turn.suggestions : undefined,
      };
      set((s) => ({
        messages: [...s.messages, reply],
        busy: false,
        prevIntent: turn.intent,
        prevTargetIds: turn.targetIds.length ? turn.targetIds : s.prevTargetIds,
      }));
    } catch {
      // The brain is pure and should not throw; the LLM path is guarded.
      set((s) => ({
        messages: [...s.messages, { id: seq++, role: "assistant", text: t("assistant.reply.unknown"), suggestions: ["assistant.suggest.help", "assistant.suggest.count"] }],
        busy: false,
      }));
    }
  },
  runCard: async (action) => {
    const ok = await runAction(action);
    if (!ok) reportInert();
  },
  clear: () => set({ messages: [], prevIntent: null, prevTargetIds: [] }),
}));
