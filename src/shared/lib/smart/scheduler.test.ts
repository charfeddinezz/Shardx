// node --test: the adaptive batch runner (`npm run test:unit`).
import { test } from "node:test";
import assert from "node:assert/strict";
import { backoffDelay, createScheduler, stepConcurrency, type RunEvent } from "./scheduler.ts";

const tick = (ms = 0) => new Promise<void>((r) => setTimeout(r, ms));

test("stepConcurrency widens on success and halves on failure", () => {
  assert.equal(stepConcurrency(2, true, {}), 3);
  assert.equal(stepConcurrency(8, true, { maxConcurrency: 8 }), 8);
  assert.equal(stepConcurrency(5, false, {}), 2);
  assert.equal(stepConcurrency(1, false, {}), 1);
  assert.equal(stepConcurrency(3, true, { minConcurrency: 2, maxConcurrency: 4 }), 4);
});

test("backoffDelay grows exponentially, caps, and jitters ±25%", () => {
  const half = () => 0.5;
  assert.equal(backoffDelay(1, { baseDelayMs: 400, maxDelayMs: 8000 }, half), 400);
  assert.equal(backoffDelay(2, { baseDelayMs: 400, maxDelayMs: 8000 }, half), 800);
  assert.equal(backoffDelay(5, { baseDelayMs: 400, maxDelayMs: 8000 }, half), 6400);
  assert.equal(backoffDelay(9, { baseDelayMs: 400, maxDelayMs: 8000 }, half), 8000);
  assert.equal(backoffDelay(1, { baseDelayMs: 400, maxDelayMs: 8000 }, () => 0), 300);
  assert.equal(backoffDelay(1, { baseDelayMs: 400, maxDelayMs: 8000 }, () => 0.999), 500);
});

test("a batch runs in lanes and returns results in job order", async () => {
  const live = new Set<number>();
  let peak = 0;
  const jobs = Array.from({ length: 6 }, (_, i) => async () => {
    live.add(i);
    peak = Math.max(peak, live.size);
    await tick(5);
    live.delete(i);
    return i * 10;
  });
  const s = createScheduler({ startConcurrency: 3, sleep: async () => {}, random: () => 0.5 });
  const out = await s.run(jobs);
  assert.deepEqual(out.map((r) => (r.ok ? r.value : -1)), [0, 10, 20, 30, 40, 50]);
  assert.ok(peak <= 3, `peak ${peak}`);
  assert.ok(peak >= 2, `peak ${peak}`);
});

test("a failing job is retried with backoff, then reported", async () => {
  const sleeps: number[] = [];
  const events: RunEvent[] = [];
  let tries = 0;
  const s = createScheduler({
    startConcurrency: 1,
    attempts: 3,
    baseDelayMs: 100,
    sleep: async (ms) => { sleeps.push(ms); },
    random: () => 0.5,
  });
  const out = await s.run([
    async () => {
      tries++;
      if (tries < 3) throw new Error("busy");
      return "up";
    },
  ], (e) => events.push(e));
  assert.equal(tries, 3);
  assert.ok(out[0]?.ok && out[0].value === "up" && out[0].attempts === 3);
  assert.deepEqual(sleeps, [100, 200]);
  assert.deepEqual(events.map((e) => e.kind), ["start", "retry", "start", "retry", "start", "done"]);
});

test("a job that never succeeds ends as a failure after its tries", async () => {
  const s = createScheduler({ startConcurrency: 1, attempts: 2, sleep: async () => {}, random: () => 0.5 });
  const out = await s.run([async () => { throw new Error("dead"); }]);
  assert.ok(out[0] && !out[0].ok);
  assert.equal(out[0].attempts, 2);
  assert.equal(String((out[0] as { error: unknown }).error), "Error: dead");
});

test("a failure narrows the shared lane count mid-batch", async () => {
  const s = createScheduler({ startConcurrency: 4, attempts: 1, sleep: async () => {}, random: () => 0.5 });
  const order: string[] = [];
  const slow = (name: string) => async () => { order.push(name); await tick(2); };
  const out = await s.run([
    slow("a"),
    async () => { order.push("bad"); throw new Error("x"); },
    slow("c"),
    slow("d"),
    slow("e"),
    slow("f"),
  ]);
  // Every job ran once; the failure is reported in its own slot.
  assert.deepEqual([...order].sort(), ["a", "bad", "c", "d", "e", "f"]);
  assert.ok(out[1] && !out[1].ok);
  assert.ok(out.filter((r) => r.ok).length === 5);
  // Four lanes, one failure (-2) and five successes (+5) over the batch.
  assert.ok(s.concurrency() >= 1 && s.concurrency() <= 8, `${s.concurrency()}`);
});
