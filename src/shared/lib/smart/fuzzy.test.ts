// node --test: the fuzzy matcher the assistant resolves profile names with
// (`npm run test:unit`).
import { test } from "node:test";
import assert from "node:assert/strict";
import { bestScore, editDistance, fuzzyRank, fuzzyScore, isSubsequence, normalizeText, words } from "./fuzzy.ts";

test("normalizeText folds case, diacritics and punctuation", () => {
  assert.equal(normalizeText("  Café  Déjà—Vu!! "), "cafe deja vu");
  assert.equal(normalizeText("FB Ads acc-02"), "fb ads acc 02");
  assert.deepEqual(words("Sandbox, Main"), ["sandbox", "main"]);
  assert.deepEqual(words("  …  "), []);
});

test("an exact name scores 1, an empty side scores 0", () => {
  assert.equal(fuzzyScore("Sandbox", "Sandbox"), 1);
  assert.equal(fuzzyScore("sandbox", "SANDBOX"), 1);
  assert.equal(fuzzyScore("", "Sandbox"), 0);
  assert.equal(fuzzyScore("Sandbox", ""), 0);
});

test("a prefix outranks a mid-string hit, which outranks a loose one", () => {
  const prefix = fuzzyScore("sand", "sandbox main");
  const inside = fuzzyScore("box", "sandbox main");
  const loose = fuzzyScore("sam", "sandbox main");
  assert.ok(prefix > 0.9, `prefix ${prefix}`);
  assert.ok(inside > 0.75 && inside < prefix, `inside ${inside}`);
  assert.ok(loose > 0 && loose < inside, `loose ${loose}`);
});

test("a word-starts match beats a scattered one", () => {
  const initials = fuzzyScore("fa", "FB Ads acc-02");
  const scattered = fuzzyScore("fa", "safari test");
  assert.ok(initials > scattered, `${initials} vs ${scattered}`);
});

test("one typo in a short name still matches", () => {
  assert.ok(fuzzyScore("sandbx", "sandbox") > 0.4);
  assert.ok(fuzzyScore("tokyo", "tokio") > 0.4);
  assert.equal(fuzzyScore("zzzzqq", "sandbox"), 0);
});

test("editDistance counts an adjacent swap once and respects the cutoff", () => {
  assert.equal(editDistance("sandbox", "sandbox"), 0);
  assert.equal(editDistance("snadbox", "sandbox"), 1);
  assert.equal(editDistance("sabdnox", "sandbox"), 2);
  assert.equal(editDistance("abc", ""), 3);
  assert.ok(editDistance("abcdefgh", "zzzzzzzz", 2) > 2);
});

test("isSubsequence keeps order and allows gaps", () => {
  assert.ok(isSubsequence("sbx", "sandbox"));
  assert.ok(!isSubsequence("xbd", "sandbox"));
});

test("fuzzyRank orders by score, keeps ties stable and drops weak rows", () => {
  const items = [{ name: "Sandbox" }, { name: "sand-line" }, { name: "Totally other" }];
  const out = fuzzyRank("sand", items, (i) => i.name);
  assert.deepEqual(out.map((i) => i.name), ["Sandbox", "sand-line"]);
  assert.deepEqual(fuzzyRank("qqq", items, (i) => i.name), []);
  assert.deepEqual(fuzzyRank("s", items, (i) => i.name, { limit: 1 }).map((i) => i.name), ["Sandbox"]);
});

test("bestScore takes the closest of several spellings", () => {
  assert.equal(bestScore("sandbox", ["main", "sandbox"]), 1);
  assert.equal(bestScore("nothing", ["main", "sandbox"]), 0);
});
