// The intent classifier: weighted evidence over folded tokens, phrase
// bonuses, and a continuity nudge from the turn before ("stop them too"
// after a start). Pure and offline; match.test.ts scores it against a
// sentence bank. The formula and the weights live in model/intents.ts.

import { INTENTS, MIN_CONFIDENCE, STRONG, scaled, type IntentDef } from "../model/intents.ts";
import type { MatchResult, Slots, IntentId } from "../model/types";
import { extractSlots } from "./slots.ts";
import { foldToken, tokenize } from "./tokenize.ts";

/** Evidence one token adds to one intent. A word said as a trigger is written
 *  counts in full ("running" for "running"); a word that only folds to the
 *  trigger's stem counts half ("run" is not quite "running"), so one stem
 *  cannot speak for two verbs at once. */
function wordEvidence(def: IntentDef, raw: string, folded: string): number {
  let sum = 0;
  for (const trigger of def.words) {
    if (raw === trigger.w || folded === trigger.w) sum += trigger.k;
    else if (folded === foldToken(trigger.w)) sum += trigger.k * 0.5;
  }
  return sum;
}

/** Evidence the sentence's phrases add (normalized text contains them). */
function phraseEvidence(def: IntentDef, text: string): number {
  let sum = 0;
  for (const phrase of def.phrases ?? []) {
    if (text.includes(phrase)) sum += STRONG;
  }
  return sum;
}

/** Scores every intent for one sentence. `prev` is the intent that answered
 *  the turn before, for follow-ups. `slots` may come pre-extracted when the
 *  caller has them already. */
export function classify(raw: string, prev?: IntentId | null, slots?: Slots): MatchResult {
  const tok = tokenize(raw);
  const used = slots ?? extractSlots(raw);
  const runnersUp: { intent: IntentId; confidence: number }[] = [];
  let best: { intent: IntentId; confidence: number } = { intent: "unknown", confidence: 0 };
  for (const def of INTENTS) {
    if (def.id === "unknown") continue;
    let evidence = phraseEvidence(def, tok.text);
    for (let i = 0; i < tok.tokens.length; i++) evidence += wordEvidence(def, tok.rawTokens[i], tok.tokens[i]);
    if (def.continuity && prev === def.id) evidence += def.continuity;
    const confidence = scaled(evidence, tok.tokens.length);
    if (confidence > best.confidence) {
      if (best.confidence > 0) runnersUp.push(best);
      best = { intent: def.id, confidence };
    } else if (confidence > 0) {
      runnersUp.push({ intent: def.id, confidence });
    }
  }
  runnersUp.sort((a, b) => b.confidence - a.confidence);
  // A sentence about a page but with no other reading is a navigation ask,
  // even when the verbs were soft ("settings please").
  if (best.confidence < MIN_CONFIDENCE && used.page && tok.tokens.length <= 3) {
    best = { intent: "openPage", confidence: MIN_CONFIDENCE };
  }
  return { intent: best.intent, confidence: best.confidence, slots: used, runnersUp: runnersUp.slice(0, 3) };
}
