// Fuzzy string matching for the assistant's name slots and any lookup that
// must survive typos ("Sandbx" finds "Sandbox"). Pure functions over strings,
// no state: scripts and unit tests share them as they are (smart/fuzzy.test.ts).
//
// The score is 0..1 and blends three signals:
// 1. an exact (or exact-prefix) hit is best;
// 2. a subsequence run is scored by how much of the query lands on word
//    starts and how contiguous the runs are ("fb" finds "FB Ads acc-02" high);
// 3. a near-miss edit distance covers single typos in short words.

/** Lowercase, strip diacritics, and fold punctuation into single spaces. */
export function normalizeText(input: string): string {
  return input
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

/** Tokens of `normalizeText`, in order. */
export function words(input: string): string[] {
  const n = normalizeText(input);
  return n ? n.split(" ") : [];
}

/** Damerau-Levenshtein distance (insert, delete, substitute, swap) with an
 *  early exit once every cell of a band above `max` can only grow. */
export function editDistance(a: string, b: string, max = Number.POSITIVE_INFINITY): number {
  if (a === b) return 0;
  if (Math.abs(a.length - b.length) > max) return max + 1;
  const n = a.length;
  const m = b.length;
  if (n === 0) return m;
  if (m === 0) return n;
  let prev2: number[] = [];
  let prev: number[] = Array.from({ length: m + 1 }, (_, j) => j);
  let cur: number[] = new Array<number>(m + 1);
  for (let i = 1; i <= n; i++) {
    cur[0] = i;
    let rowMin = cur[0];
    for (let j = 1; j <= m; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      let v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
      if (i > 1 && j > 1 && a[i - 1] === b[j - 2] && a[i - 2] === b[j - 1]) {
        v = Math.min(v, prev2[j - 2] + 1);
      }
      cur[j] = v;
      if (v < rowMin) rowMin = v;
    }
    if (rowMin > max) return max + 1;
    prev2 = prev;
    prev = cur;
    cur = new Array<number>(m + 1);
  }
  return prev[m];
}

/** Whether `query` appears in `text` in order, not necessarily contiguously. */
export function isSubsequence(query: string, text: string): boolean {
  let i = 0;
  for (const ch of text) {
    if (ch === query[i]) i++;
    if (i === query.length) return true;
  }
  return query.length === 0;
}

/** How many of `query`'s characters land on a word start in `text`. */
function boundaryHits(query: string, text: string): number {
  let hits = 0;
  let at = 0;
  for (let i = 0; i < text.length && at < query.length; i++) {
    if (text[i] !== query[at]) continue;
    const startsWord = i === 0 || text[i - 1] === " ";
    if (startsWord) hits++;
    at++;
  }
  return hits;
}

/** The longest contiguous run of `query` inside `text`. */
function longestRun(query: string, text: string): number {
  let best = 0;
  for (let i = 0; i < query.length; i++) {
    for (let j = i + 1; j <= query.length; j++) {
      const part = query.slice(i, j);
      if (part.length > best && text.includes(part)) best = part.length;
    }
  }
  return best;
}

/** A 0..1 likeness of `query` to `text`. 1 is an exact match. */
export function fuzzyScore(query: string, text: string): number {
  const q = normalizeText(query);
  const t = normalizeText(text);
  if (!q || !t) return 0;
  if (q === t) return 1;
  const tight = 1 - (t.length - q.length) / (t.length + 8);
  if (t.startsWith(q)) return 0.92 + 0.06 * (q.length / t.length);
  let sub = 0;
  if (isSubsequence(q, t)) {
    const run = longestRun(q, t) / q.length;
    const hits = boundaryHits(q, t);
    // Every character on a word start: an initials match ("fa" finds
    // "FB Ads acc-02"), the way people type team codes.
    if (hits === q.length) sub = 0.84 + 0.06 * tight;
    else sub = Math.min(0.77, 0.28 + 0.26 * run + 0.2 * (hits / q.length) + 0.12 * tight);
  }
  const inner = t.includes(q) ? 0.78 + 0.1 * (q.length / t.length) : 0;
  if (sub || inner) return Math.max(sub, inner);
  // A typo in one word: whole-string distance, bounded by the length gap.
  const allowed = q.length <= 4 ? 1 : q.length <= 8 ? 2 : 3;
  const d = editDistance(q, t, allowed);
  return d <= allowed ? Math.max(0.25, 0.72 - 0.12 * d) : 0;
}

/** The best likeness of `query` against several `texts` (one name typed many
 *  ways), 0 when none matches. */
export function bestScore(query: string, texts: readonly string[]): number {
  let best = 0;
  for (const text of texts) {
    const s = fuzzyScore(query, text);
    if (s > best) best = s;
  }
  return best;
}

export type RankOptions = {
  /** Scores below this drop out. Default 0.35. */
  minScore?: number;
  /** Most rows kept, best first. Default unlimited. */
  limit?: number;
};

/** `items` ordered by their likeness to `query` (through `key`), best first.
 *  Ties keep the input order; items under `minScore` are left out. */
export function fuzzyRank<T>(query: string, items: readonly T[], key: (item: T) => string, opts?: RankOptions): T[] {
  const min = opts?.minScore ?? 0.35;
  const scored = items
    .map((item, index) => ({ item, index, score: fuzzyScore(query, key(item)) }))
    .filter((r) => r.score >= min)
    .sort((a, b) => b.score - a.score || a.index - b.index);
  const out = scored.map((r) => r.item);
  return opts?.limit !== undefined ? out.slice(0, opts.limit) : out;
}
