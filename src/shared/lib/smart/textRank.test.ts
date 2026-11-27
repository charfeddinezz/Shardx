// node --test: TF-IDF ranking for the assistant's local knowledge base
// (`npm run test:unit`).
import { test } from "node:test";
import assert from "node:assert/strict";
import { contentWords, cosine, idfOf, rankDocs, termCounts, tfidf } from "./textRank.ts";
import { words } from "./fuzzy.ts";

const STOP = new Set(["the", "a", "is", "to", "of", "and", "in", "it", "for", "on", "what", "how", "do", "i", "you"]);

test("termCounts counts repeats and nothing else", () => {
  const c = termCounts(["a", "b", "a"]);
  assert.equal(c.get("a"), 2);
  assert.equal(c.get("b"), 1);
  assert.equal(c.size, 2);
});

test("a word in every document weighs less than a rare one", () => {
  const docs = [["the", "profile"], ["the", "proxy"], ["the", "fingerprint"]];
  const idf = idfOf(docs);
  assert.ok((idf.get("profile") ?? 0) > (idf.get("the") ?? 0));
});

test("a document with the rare words ranks first", () => {
  const docs = [
    words("how to assign a proxy to profiles"),
    words("what is a fingerprint and its signals"),
    words("how to start and stop a profile"),
  ];
  const ranks = rankDocs(words("proxy assign"), docs);
  assert.equal(ranks[0]?.index, 0);
  assert.ok((ranks[0]?.score ?? 0) > 0);
  // The fingerprint answer has none of the query's words: dropped.
  assert.ok(!ranks.some((r) => r.index === 1));
});

test("cosine is 1 for proportional vectors and 0 for disjoint ones", () => {
  const idf = new Map([["a", 1], ["b", 1], ["c", 1]]);
  const v = tfidf(["a", "b"], idf);
  const w = tfidf(["a", "b"], idf);
  assert.ok(Math.abs(cosine(v, w) - 1) < 1e-9);
  assert.equal(cosine(v, tfidf(["c"], idf)), 0);
  assert.equal(cosine(new Map(), v), 0);
});

test("contentWords drops stop words and repeats, keeps order", () => {
  assert.deepEqual(contentWords("the profile is a profile of proxy", STOP), ["profile", "proxy"]);
  assert.deepEqual(contentWords("", STOP), []);
});
