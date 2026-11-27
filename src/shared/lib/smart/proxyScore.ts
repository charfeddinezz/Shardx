// Proxy health scoring: turns a proxy's test history into one 0..1 number the
// assistant (and any picker) can sort by. Pure functions; the launcher's
// proxy rows are adapted to `ProxyHealth` at the call site
// (smart/proxyScore.test.ts).
//
// The parts, multiplied as odds-friendly factors so one weak part cannot hide
// behind a strong other:
// - reliability: the Wilson score interval's lower bound at 95%, so a proxy
//   that passed 3 of 3 tests does not outrank one that passed 90 of 100;
// - latency: 1 / (1 + rtt / 250 ms), so 250 ms halves it;
// - freshness: exp(-age / 30 min), so a stale verdict fades;
// - place: the exit country the binding wants against the one a test saw.

export type ProxyHealth = {
  /** Smoothed round-trip time in ms from the tests, or null when untested. */
  latencyMs: number | null;
  successes: number;
  failures: number;
  /** Seconds since the last successful test; Number.POSITIVE_INFINITY when never. */
  ageSec: number;
  /** Uppercase ISO code the proxy should exit in, or "" when any will do. */
  wantCountry: string;
  /** Uppercase ISO code the last test saw, or "" when unknown. */
  seenCountry: string;
  /** True when the last test proved UDP relay (WebRTC, QUIC). */
  udpOk?: boolean;
};

/** The lower end of Wilson's 95% interval for a success rate: a small sample
 *  stays cautious, a long one settles near its raw rate. */
export function wilsonLower(successes: number, failures: number, z = 1.96): number {
  const n = successes + failures;
  if (n === 0) return 0;
  const p = successes / n;
  const z2 = z * z;
  const denom = 1 + z2 / n;
  const centre = p + z2 / (2 * n);
  const spread = z * Math.sqrt((p * (1 - p) + z2 / (4 * n)) / n);
  return Math.max(0, (centre - spread) / denom);
}

/** 1 at 0 ms, 0.5 at 250 ms, 0.25 at 750 ms; 0.4 for an untested proxy. */
export function latencyFactor(latencyMs: number | null): number {
  if (latencyMs === null || !Number.isFinite(latencyMs) || latencyMs < 0) return 0.4;
  return 1 / (1 + latencyMs / 250);
}

/** exp(-age / halfLife), halfLife in seconds (default 30 minutes). */
export function freshnessFactor(ageSec: number, halfLifeSec = 1800): number {
  if (!Number.isFinite(ageSec)) return 0.15;
  return Math.exp(-Math.max(0, ageSec) / halfLifeSec);
}

/** 1 for the wanted country, 0.6 when either side is unknown, 0.35 for another. */
export function placeFactor(want: string, seen: string): number {
  if (!want || !seen) return 0.6;
  return want === seen ? 1 : 0.35;
}

/** The 0..1 health of one proxy. An untested proxy lands near 0.2: present,
 *  but below anything with a clean record. */
export function scoreProxy(h: ProxyHealth): number {
  const reliability = wilsonLower(h.successes, h.failures);
  const parts = reliability * latencyFactor(h.latencyMs) * freshnessFactor(h.ageSec) * placeFactor(h.wantCountry, h.seenCountry);
  // A proven UDP relay is worth a small, capped lift (QUIC and WebRTC work).
  const bonus = h.udpOk ? 1.08 : 1;
  return Math.min(1, parts * bonus);
}

export type Scored<T> = { item: T; score: number };

/** `rows` best health first. Ties keep the input order. */
export function rankProxies<T>(rows: readonly { item: T; health: ProxyHealth }[]): Scored<T>[] {
  return rows
    .map((r, index) => ({ item: r.item, score: scoreProxy(r.health), index }))
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .map(({ item, score }) => ({ item, score }));
}

/** A short, ordered list of what pulls `h` down, as factors of its score:
 *  "reliability", "latency", "freshness", "place", each with its 0..1 value,
 *  worst first. An assistant reply turns these into words. */
export function weakestLinks(h: ProxyHealth): { factor: "reliability" | "latency" | "freshness" | "place"; value: number }[] {
  const parts = [
    { factor: "reliability" as const, value: wilsonLower(h.successes, h.failures) },
    { factor: "latency" as const, value: latencyFactor(h.latencyMs) },
    { factor: "freshness" as const, value: freshnessFactor(h.ageSec) },
    { factor: "place" as const, value: placeFactor(h.wantCountry, h.seenCountry) },
  ];
  return parts.sort((a, b) => a.value - b.value);
}
