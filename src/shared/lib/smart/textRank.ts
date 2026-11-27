// Light document ranking for the assistant's local knowledge base: TF-IDF
// vectors, cosine similarity and keyword extraction. Everything runs on this
// computer, with no model and no network (smart/textRank.test.ts).
//
// The math is the textbook TF-IDF: a word counts for a document in proportion
// to how often it appears there (TF) and in inverse proportion to how many
// documents use it (IDF), so "fingerprint" separates fingerprint answers while
// "the" separates nothing. Cosine over the vectors ranks the candidates.

import { words } from "./fuzzy.ts";

/** Term counts of one token list. */
export function termCounts(tokens: readonly string[]): Map<string, number> {
  const out = new Map<string, number>();
  for (const tok of tokens) out.set(tok, (out.get(tok) ?? 0) + 1);
  return out;
}

/** IDF of each term across `docs` (token lists), with smoothing: a term in
 *  every document lands near 0, one in a single document near 1. */
export function idfOf(docs: readonly (readonly string[])[]): Map<string, number> {
  const df = new Map<string, number>();
  for (const doc of docs) {
    for (const tok of new Set(doc)) df.set(tok, (df.get(tok) ?? 0) + 1);
  }
  const n = docs.length || 1;
  const out = new Map<string, number>();
  for (const [tok, dfi] of df) out.set(tok, Math.log((n + 1) / (dfi + 1)) + 1);
  return out;
}

/** A TF-IDF vector of one token list. */
export function tfidf(tokens: readonly string[], idf: Map<string, number>): Map<string, number> {
  const counts = termCounts(tokens);
  const out = new Map<string, number>();
  let norm = 0;
  for (const [tok, count] of counts) {
    const weight = count * (idf.get(tok) ?? Math.log(2) + 1);
    out.set(tok, weight);
    norm += weight * weight;
  }
  // Length-normalized: a short answer with one strong word can beat a long one.
  norm = Math.sqrt(norm) || 1;
  for (const [tok, weight] of out) out.set(tok, weight / norm);
  return out;
}

/** Cosine of two sparse vectors: 0 for disjoint, 1 for proportional. */
export function cosine(a: Map<string, number>, b: Map<string, number>): number {
  const [small, large] = a.size <= b.size ? [a, b] : [b, a];
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (const [tok, weight] of small) {
    dot += weight * (large.get(tok) ?? 0);
    na += weight * weight;
  }
  for (const weight of large.values()) nb += weight * weight;
  if (!na || !nb) return 0;
  return dot / (Math.sqrt(na) * Math.sqrt(nb));
}

export type DocRank = { index: number; score: number };

/** `docs` (token lists) ordered by their likeness to `query`'s tokens, best
 *  first, with scores. Entries under `minScore` drop out (default 0.1). */
export function rankDocs(query: readonly string[], docs: readonly (readonly string[])[], minScore = 0.1): DocRank[] {
  const idf = idfOf([...docs, query]);
  const qv = tfidf(query, idf);
  return docs
    .map((doc, index) => ({ index, score: cosine(qv, tfidf(doc, idf)) }))
    .filter((r) => r.score >= minScore)
    .sort((a, b) => b.score - a.score || a.index - b.index);
}

/** The content words of `text`, stop words removed, in order of first use. */
export function contentWords(text: string, stop: ReadonlySet<string>): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const tok of words(text)) {
    if (tok.length < 2 || stop.has(tok) || seen.has(tok)) continue;
    seen.add(tok);
    out.push(tok);
  }
  return out;
}
