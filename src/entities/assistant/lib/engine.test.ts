// node --test: the turn engine over a fixture workspace (`npm run test:unit`).
import { test } from "node:test";
import assert from "node:assert/strict";
import { interpret } from "./engine.ts";
import { factsOf } from "./context.ts";
import type { AssistantContext } from "../model/types.ts";

const ctx: AssistantContext = {
  profiles: [
    { id: "p1", name: "Sandbox", running: true, pinned: true, folder: "Ads", proxyId: "x1" },
    { id: "p2", name: "FB Ads acc-02", running: true, pinned: false, folder: "Ads", proxyId: null },
    { id: "p3", name: "Etsy craft shop", running: false, pinned: false, folder: "", proxyId: "x2" },
    { id: "p4", name: "Main store", running: false, pinned: true, folder: "Shop", proxyId: "x1" },
  ],
  proxies: [
    { id: "x1", name: "DC Tokyo", country: "JP", latencyMs: 120, status: "ok" },
    { id: "x2", name: "RS Berlin", country: "DE", latencyMs: 400, status: "fail" },
    { id: "x3", name: "DC New York", country: "US", latencyMs: null, status: "idle" },
  ],
  section: "profiles",
  selectedIds: ["p3"],
};

test("greet and help answer with their own keys", () => {
  assert.equal(interpret({ text: "hello" }, ctx).reply.key, "assistant.reply.greet");
  const help = interpret({ text: "what can you do" }, ctx);
  assert.equal(help.reply.key, "assistant.reply.help");
  assert.ok(help.suggestions.length > 0);
});

test("countProfiles fills every number from the workspace", () => {
  const turn = interpret({ text: "how many profiles?" }, ctx);
  assert.equal(turn.reply.key, "assistant.reply.countProfiles");
  assert.deepEqual(turn.reply.vars, { total: 4, running: 2, pinned: 2, folders: 2, noProxy: 1, proxies: 3, failedProxies: 1 });
});

test("runningNow lists the running rows and offers to stop them", () => {
  const turn = interpret({ text: "what is running now?" }, ctx);
  assert.equal(turn.reply.key, "assistant.reply.runningNow");
  assert.equal(turn.reply.vars?.n, 2);
  assert.match(String(turn.reply.vars?.names), /Sandbox/);
  assert.deepEqual(turn.actions[0]?.kind, "filterProfiles");
  assert.deepEqual(turn.actions[1]?.kind, "stopProfiles");
});

test("no session running gets its own quiet answer", () => {
  const idle = { ...ctx, profiles: ctx.profiles.map((p) => ({ ...p, running: false })) };
  const turn = interpret({ text: "what is running now?" }, idle);
  assert.equal(turn.reply.key, "assistant.reply.runningNone");
  assert.equal(turn.actions.length, 0);
});

test("start by name resolves the name through typos", () => {
  const turn = interpret({ text: "start sandbx" }, ctx);
  assert.equal(turn.reply.key, "assistant.reply.startNamed");
  assert.deepEqual(turn.actions[0], { kind: "startProfiles", ids: ["p1"], names: ["Sandbox"] });
});

test("start all and the selected take the right rows", () => {
  const all = interpret({ text: "start all" }, ctx);
  assert.equal(all.reply.key, "assistant.reply.startAll");
  assert.equal(all.reply.vars?.n, 4);
  const sel = interpret({ text: "run the selected ones" }, ctx);
  assert.equal(sel.reply.key, "assistant.reply.startSelected");
  assert.deepEqual(sel.actions[0]?.kind, "startProfiles");
  assert.deepEqual((sel.actions[0] as { ids: string[] }).ids, ["p3"]);
});

test("stop with no subject asks which", () => {
  const turn = interpret({ text: "stop" }, ctx);
  assert.equal(turn.reply.key, "assistant.reply.askWhich");
  const named = interpret({ text: "close etsy" }, ctx);
  assert.equal(named.reply.key, "assistant.reply.stopNamed");
  assert.deepEqual((named.actions[0] as { ids: string[] }).ids, ["p3"]);
});

test("a name that matches nothing says so", () => {
  const turn = interpret({ text: "start zzzzqqq" }, ctx);
  assert.equal(turn.reply.key, "assistant.reply.findNone");
});

test("find offers to show the rows in the list", () => {
  const turn = interpret({ text: "find the ads profiles" }, ctx);
  assert.equal(turn.reply.key, "assistant.reply.findNamed");
  assert.equal(turn.actions[0]?.kind, "filterProfiles");
});

test("proxy health names the workspace's numbers and the best rows", () => {
  const turn = interpret({ text: "how is proxy health?" }, ctx);
  assert.equal(turn.reply.key, "assistant.reply.proxyHealth");
  assert.equal(turn.reply.vars?.n, 3);
  assert.equal(turn.reply.vars?.failed, 1);
  assert.match(String(turn.reply.vars?.names), /DC Tokyo/);
  const empty = interpret({ text: "how is proxy health?" }, { ...ctx, proxies: [] });
  assert.equal(empty.reply.key, "assistant.reply.proxyHealthEmpty");
});

test("assign and create carry their cards", () => {
  const assign = interpret({ text: "assign a proxy to sandbox" }, ctx);
  assert.equal(assign.reply.key, "assistant.reply.assignProxy");
  assert.deepEqual(assign.actions[0], { kind: "openAssign", ids: ["p1"] });
  const create = interpret({ text: "create a profile" }, ctx);
  assert.deepEqual(create.actions[0], { kind: "runCommand", commandId: "profile.new" });
});

test("a page name navigates, a bare verb asks", () => {
  const go = interpret({ text: "go to settings" }, ctx);
  assert.equal(go.reply.key, "assistant.reply.openPage");
  assert.deepEqual(go.actions[0], { kind: "navigate", section: "settings" });
  const bare = interpret({ text: "go" }, ctx);
  assert.equal(bare.reply.key, "assistant.reply.askWhich");
});

test("a question about the app answers from the knowledge base", () => {
  const turn = interpret({ text: "what is a fingerprint?" }, ctx);
  assert.equal(turn.reply.key, "assistant.kb.fingerprint");
  assert.equal(turn.knowledgeId, "fingerprint");
});

test("a question with no entry admits it", () => {
  const turn = interpret({ text: "explain quantum foam" }, ctx);
  assert.equal(turn.reply.key, "assistant.reply.explainNone");
  assert.equal(turn.reply.vars?.topic, "quantum foam");
});

test("noise gets the unknown answer with ways on", () => {
  const turn = interpret({ text: "purple monkey dishwasher" }, ctx);
  assert.equal(turn.reply.key, "assistant.reply.unknown");
  assert.ok(turn.suggestions.includes("assistant.suggest.help"));
});

test("a follow-up pronoun keeps the rows of the turn before", () => {
  const idleSelection = { ...ctx, selectedIds: [] };
  const first = interpret({ text: "start sandbox" }, idleSelection);
  assert.deepEqual(first.targetIds, ["p1"]);
  const follow = interpret({ text: "stop them", prevIntent: first.intent, prevTargetIds: first.targetIds }, idleSelection);
  assert.equal(follow.reply.key, "assistant.reply.stopSelected");
  assert.deepEqual((follow.actions[0] as { ids: string[] }).ids, ["p1"]);
});

test("factsOf reads the numbers as one line", () => {
  const line = factsOf(ctx);
  assert.match(line, /4 profiles/);
  assert.match(line, /2 running/);
  assert.match(line, /1 with a failed test/);
});
