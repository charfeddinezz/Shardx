// Adaptive batch runner for starting many sessions at once: it paces the
// starts, widens the lane while starts succeed, narrows it the moment they
// fail, and retries a failed job with jittered exponential backoff
// (smart/scheduler.test.ts). The wait and the randomness are injected, so
// tests run in milliseconds over a recorded timeline.
//
// The concurrency rule is AIMD (additive increase, multiplicative decrease),
// the same shape TCP uses: +1 lane per clean job, halve on a failure (floor
// minConcurrency). A batch of starts then finds the engine's real ceiling on
// its own instead of trusting a fixed number.

export type SchedulerOptions = {
  /** Lanes at the start. Default 2. */
  startConcurrency?: number;
  /** Lanes never drop below this. Default 1. */
  minConcurrency?: number;
  /** Lanes never rise above this. Default 8. */
  maxConcurrency?: number;
  /** First retry delay in ms. Default 400. */
  baseDelayMs?: number;
  /** A retry delay never grows past this. Default 8000. */
  maxDelayMs?: number;
  /** Full tries per job, the first included. Default 3. */
  attempts?: number;
  /** Injected wait. Default a real timeout. */
  sleep?: (ms: number) => Promise<void>;
  /** Injected randomness in [0, 1). Default Math.random. */
  random?: () => number;
};

export type JobResult<T> = { ok: true; value: T; attempts: number } | { ok: false; error: unknown; attempts: number };

export type RunEvent =
  | { kind: "start"; index: number; attempt: number }
  | { kind: "done"; index: number; attempts: number }
  | { kind: "retry"; index: number; attempt: number; delayMs: number }
  | { kind: "failed"; index: number; attempts: number };

export type Scheduler = {
  /** The lane count after the last job (how wide the batch found the door). */
  concurrency: () => number;
  run<T>(jobs: readonly (() => Promise<T>)[], onEvent?: (e: RunEvent) => void): Promise<JobResult<T>[]>;
};

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** The next lane count after one job's outcome (AIMD, pure). */
export function stepConcurrency(current: number, ok: boolean, opts: Pick<SchedulerOptions, "minConcurrency" | "maxConcurrency">): number {
  const min = Math.max(1, opts.minConcurrency ?? 1);
  const max = Math.max(min, opts.maxConcurrency ?? 8);
  if (ok) return Math.min(max, current + 1);
  return Math.max(min, Math.floor(current / 2));
}

/** The wait before retry `attempt` (1-based, so 1 is the first retry):
 *  base * 2^(attempt-1), capped, then ±25% jitter so retries spread out. */
export function backoffDelay(attempt: number, opts: Pick<SchedulerOptions, "baseDelayMs" | "maxDelayMs">, random: () => number): number {
  const base = opts.baseDelayMs ?? 400;
  const cap = opts.maxDelayMs ?? 8000;
  const plain = Math.min(cap, base * Math.pow(2, Math.max(0, attempt - 1)));
  const jitter = 0.75 + 0.5 * random();
  return Math.round(plain * jitter);
}

/** A scheduler that runs `jobs` through one shared lane pool. Each job is
 *  tried up to `attempts` times; the returned array lines up with `jobs`.
 *  Lanes share one queue, so a lane that frees up picks up the next job even
 *  while another lane is still working. */
export function createScheduler(opts: SchedulerOptions = {}): Scheduler {
  const sleep = opts.sleep ?? defaultSleep;
  const random = opts.random ?? Math.random;
  const attempts = Math.max(1, opts.attempts ?? 3);
  let concurrency = Math.max(1, opts.startConcurrency ?? 2);

  return {
    concurrency: () => concurrency,
    run: async <T>(jobs: readonly (() => Promise<T>)[], onEvent?: (e: RunEvent) => void): Promise<JobResult<T>[]> => {
      const results = new Array<JobResult<T>>(jobs.length);
      let next = 0;
      const lane = async (): Promise<void> => {
        for (;;) {
          const index = next++;
          if (index >= jobs.length) return;
          let tries = 0;
          for (;;) {
            tries++;
            onEvent?.({ kind: "start", index, attempt: tries });
            try {
              const value = await jobs[index]();
              results[index] = { ok: true, value, attempts: tries };
              concurrency = stepConcurrency(concurrency, true, opts);
              onEvent?.({ kind: "done", index, attempts: tries });
              break;
            } catch (error) {
              if (tries >= attempts) {
                results[index] = { ok: false, error, attempts: tries };
                concurrency = stepConcurrency(concurrency, false, opts);
                onEvent?.({ kind: "failed", index, attempts: tries });
                break;
              }
              const delayMs = backoffDelay(tries, opts, random);
              onEvent?.({ kind: "retry", index, attempt: tries, delayMs });
              await sleep(delayMs);
            }
          }
        }
      };
      const lanes: Promise<void>[] = [];
      for (let i = 0; i < Math.min(concurrency, jobs.length); i++) lanes.push(lane());
      await Promise.all(lanes);
      return results;
    },
  };
}
