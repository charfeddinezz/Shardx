// Knowledge retrieval: the question in, the closest answer out. Direct
// naming first (a token is an entry id, typos included), then TF-IDF over
// the knowledge base's terms (shared/lib/smart/textRank.ts). Offline and
// deterministic (knowledge.test.ts).

import { fuzzyScore } from "../../../shared/lib/smart/fuzzy.ts";
import { rankDocs } from "../../../shared/lib/smart/textRank.ts";
import { KNOWLEDGE, type KnowledgeEntry } from "../model/knowledge.ts";
import { tokenize } from "./tokenize.ts";

const DOCS = KNOWLEDGE.map((e) => e.terms.map((term) => term.toLowerCase()));

/** How alike a token and an entry id must be to name it outright. */
const NAMED_FLOOR = 0.55;

/** The entry that answers `question`, or null when nothing is close enough. */
export function retrieve(question: string): KnowledgeEntry | null {
  const tok = tokenize(question);
  if (!tok.tokens.length) return null;
  // Direct naming: one of the question's words is (nearly) an entry id.
  // Three characters and up: a lone "a" must not name "api".
  const words = tok.tokens.filter((token) => token.length >= 3);
  let named: KnowledgeEntry | undefined;
  let namedScore = 0;
  for (const entry of KNOWLEDGE) {
    for (const token of words) {
      const s = fuzzyScore(token, entry.id);
      if (s > namedScore) {
        namedScore = s;
        named = entry;
      }
    }
  }
  if (named && namedScore >= NAMED_FLOOR) return named;
  // Else the closest document by TF-IDF over its question words.
  const ranked = rankDocs(tok.tokens, DOCS, 0.12);
  return ranked.length ? KNOWLEDGE[ranked[0].index] : null;
}
