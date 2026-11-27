// Reading the workspace: summaries and name resolution over the context
// snapshot (model/types.ts). Pure functions; engine.test.ts feeds them plain
// fixtures. The snapshot itself is built in lib/actions.ts from the stores.

import { fuzzyRank, fuzzyScore } from "../../../shared/lib/smart/fuzzy.ts";
import { rankProxies, type ProxyHealth } from "../../../shared/lib/smart/proxyScore.ts";
import type { AssistantContext, AssistantProfile, AssistantProxy, WorkspaceSummary } from "../model/types";

/** The numbers every count answer uses. */
export function summarize(ctx: AssistantContext): WorkspaceSummary {
  const folders = new Set<string>();
  let running = 0;
  let pinned = 0;
  let noProxy = 0;
  for (const p of ctx.profiles) {
    if (p.folder) folders.add(p.folder);
    if (p.running) running++;
    if (p.pinned) pinned++;
    if (!p.proxyId) noProxy++;
  }
  let failedProxies = 0;
  for (const x of ctx.proxies) if (x.status === "fail") failedProxies++;
  return {
    total: ctx.profiles.length,
    running,
    pinned,
    folders: folders.size,
    noProxy,
    proxies: ctx.proxies.length,
    failedProxies,
  };
}

/** How close a typed name is to a row's name, in the caller's terms. */
const NAME_FLOOR = 0.42;

/** The profiles a list of typed names points at. Each name takes its best
 *  row over the floor; one row answers once even for several names. */
export function resolveProfiles(names: readonly string[], profiles: readonly AssistantProfile[]): AssistantProfile[] {
  const out: AssistantProfile[] = [];
  const seen = new Set<string>();
  for (const name of names) {
    const hit = fuzzyRank(name, profiles, (p) => p.name, { minScore: NAME_FLOOR, limit: 1 })[0];
    if (hit && !seen.has(hit.id)) {
      seen.add(hit.id);
      out.push(hit);
    }
  }
  return out;
}

/** The rows a loose phrase points at, best first ("ads" finds every row with
 *  "Ads" in its name). Used when the sentence named no profile exactly. */
export function searchProfiles(query: string, profiles: readonly AssistantProfile[], limit = 8): AssistantProfile[] {
  return fuzzyRank(query, profiles, (p) => p.name, { minScore: 0.32, limit });
}

/** The profiles with a Session now. */
export function runningProfiles(ctx: AssistantContext): AssistantProfile[] {
  return ctx.profiles.filter((p) => p.running);
}

/** The health of one proxy row, as the scorer reads it. A clean test is a
 *  short clean record; a failed one is a short bad one; untested is untested. */
export function healthOf(proxy: AssistantProxy): ProxyHealth {
  const base: ProxyHealth = {
    latencyMs: proxy.latencyMs,
    successes: proxy.status === "ok" ? 4 : 0,
    failures: proxy.status === "fail" ? 3 : 0,
    ageSec: proxy.status === "idle" ? Number.POSITIVE_INFINITY : 60,
    wantCountry: proxy.country,
    seenCountry: proxy.country,
  };
  return base;
}

/** The proxies best health first, with their scores. */
export function rankProxyRows(proxies: readonly AssistantProxy[]): { proxy: AssistantProxy; score: number }[] {
  return rankProxies(proxies.map((proxy) => ({ item: proxy, health: healthOf(proxy) }))).map((r) => ({ proxy: r.item, score: r.score }));
}

/** The best proxy by health, or null when the workspace has none. */
export function bestProxy(proxies: readonly AssistantProxy[]): AssistantProxy | null {
  return rankProxyRows(proxies)[0]?.proxy ?? null;
}

/** How alike two profile names are (for "is X the same as Y" answers). */
export function nameLikeness(a: string, b: string): number {
  return fuzzyScore(a, b);
}

/** The workspace's numbers as one line, for the local model's prompt. */
export function factsOf(ctx: AssistantContext): string {
  const s = summarize(ctx);
  return [
    `${s.total} profiles`,
    `${s.running} running`,
    `${s.pinned} pinned`,
    `${s.folders} folders`,
    `${s.noProxy} without a proxy`,
    `${s.proxies} proxies`,
    `${s.failedProxies} with a failed test`,
  ].join(", ");
}
