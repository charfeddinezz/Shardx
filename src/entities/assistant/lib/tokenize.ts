// Turning a sentence into what the matcher scores: folded tokens, quoted
// spans (a name said exactly), and the raw normalized text for phrases.
// Pure; tokenize.test lives with match.test.ts.

import { normalizeText, words } from "../../../shared/lib/smart/fuzzy.ts";

export type Tokenized = {
  /** The sentence, folded and squeezed. */
  text: string;
  /** Tokens in order, folded (see `foldToken`). */
  tokens: string[];
  /** Tokens in order, as typed (lowercased, punctuation stripped). */
  rawTokens: string[];
  /** Spans inside straight or curly quotes, as typed (trimmed). */
  quotes: string[];
};

/** One fold step for matching: plurals and verb endings map to their stem
 *  ("launching" and "launched" both become "launch"; "proxies" becomes
 *  "proxy"). Both sides of a comparison fold the same way, so a stem stays
 *  symmetric. Short words and words that would fold to noise stay as they are. */
export function foldToken(token: string): string {
  const t = token;
  if (t.endsWith("ies") && t.length > 4) return `${t.slice(0, -3)}y`;
  if (t.endsWith("ing") && t.length > 5) {
    const stem = t.slice(0, -3);
    // "running" folds to "run": a doubled final consonant drops one.
    return stem.length > 2 && stem[stem.length - 1] === stem[stem.length - 2] ? stem.slice(0, -1) : stem;
  }
  if (t.endsWith("ed") && t.length > 4) {
    const stem = t.slice(0, -2);
    return stem.length > 2 && stem[stem.length - 1] === stem[stem.length - 2] ? stem.slice(0, -1) : stem;
  }
  if (/(?:sses|shes|ches|xes|zes|oes)$/.test(t)) return t.slice(0, -2);
  if (t.endsWith("s") && !t.endsWith("ss") && t.length > 3) return t.slice(0, -1);
  return t;
}

/** Splits a raw sentence. Quotes of any shape open and close a span. */
export function tokenize(raw: string): Tokenized {
  const quotes: string[] = [];
  const unquoted = raw.replace(/["“”«»‘’']([^"“”«»‘’']+)["“”«»‘’']/g, (_all, span: string) => {
    const name = span.trim();
    if (name) quotes.push(name);
    return " ";
  });
  const text = normalizeText(unquoted);
  const rawTokens = words(unquoted);
  return { text, tokens: rawTokens.map(foldToken), rawTokens, quotes };
}
