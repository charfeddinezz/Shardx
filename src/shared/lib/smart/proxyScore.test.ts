// node --test: proxy health scoring (`npm run test:unit`).
import { test } from "node:test";
import assert from "node:assert/strict";
import { freshnessFactor, latencyFactor, placeFactor, rankProxies, scoreProxy, weakestLinks, wilsonLower, type ProxyHealth } from "./proxyScore.ts";

const base: ProxyHealth = { latencyMs: 100, successes: 10, failures: 0, ageSec: 60, wantCountry: "US", seenCountry: "US", udpOk: true };

test("wilsonLower stays cautious on a small sample", () => {
  const threeOfThree = wilsonLower(3, 0);
  const ninetyOfHundred = wilsonLower(90, 10);
  assert.ok(threeOfThree < 0.9, `${threeOfThree}`);
  assert.ok(threeOfThree > 0.3, `${threeOfThree}`);
  assert.ok(ninetyOfHundred > 0.8, `${ninetyOfHundred}`);
  assert.equal(wilsonLower(0, 0), 0);
  assert.ok(wilsonLower(0, 5) < 0.1);
});

test("the factors move the right way", () => {
  assert.ok(latencyFactor(0) > latencyFactor(250) && latencyFactor(250) > latencyFactor(2000));
  assert.equal(latencyFactor(null), 0.4);
  assert.ok(freshnessFactor(0) > freshnessFactor(1800) && freshnessFactor(1800) > freshnessFactor(24 * 3600));
  assert.equal(placeFactor("US", "US"), 1);
  assert.ok(placeFactor("US", "DE") < placeFactor("US", ""));
  assert.ok(placeFactor("", "") === 0.6);
});

test("a clean, fast, fresh proxy scores high; each flaw pulls it down", () => {
  // 10 tests are still a young record, so Wilson's 95% lower bound alone
  // holds a perfect start around 0.7; the product lands above 0.5 here.
  const good = scoreProxy(base);
  assert.ok(good > 0.5, `${good}`);
  assert.ok(scoreProxy({ ...base, successes: 1, failures: 4 }) < good);
  assert.ok(scoreProxy({ ...base, latencyMs: 2000 }) < good);
  assert.ok(scoreProxy({ ...base, ageSec: 7 * 24 * 3600 }) < good);
  assert.ok(scoreProxy({ ...base, seenCountry: "DE" }) < good);
  const untested: ProxyHealth = { latencyMs: null, successes: 0, failures: 0, ageSec: Number.POSITIVE_INFINITY, wantCountry: "", seenCountry: "" };
  assert.ok(scoreProxy(untested) < 0.3);
  assert.ok(scoreProxy({ ...base, udpOk: false }) < good);
});

test("every score lands in 0..1", () => {
  for (const h of [base, { ...base, successes: 0, failures: 100 }, { ...base, latencyMs: 0, ageSec: 0 }]) {
    const s = scoreProxy(h);
    assert.ok(s >= 0 && s <= 1, `${s}`);
  }
});

test("rankProxies orders best first and keeps ties stable", () => {
  const rows = [
    { item: "slow", health: { ...base, latencyMs: 2000 } },
    { item: "good", health: base },
    { item: "dead", health: { ...base, successes: 0, failures: 9 } },
  ];
  assert.deepEqual(rankProxies(rows).map((r) => r.item), ["good", "slow", "dead"]);
});

test("weakestLinks names the worst part first", () => {
  const links = weakestLinks({ ...base, successes: 1, failures: 20, latencyMs: 3000, ageSec: 10, wantCountry: "US", seenCountry: "US" });
  assert.equal(links[0]?.factor, "reliability");
  assert.equal(links[links.length - 1]?.factor, "place");
  for (const l of links) assert.ok(l.value >= 0 && l.value <= 1);
});
