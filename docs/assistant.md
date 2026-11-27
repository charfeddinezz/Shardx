# The ShardBrowser assistant

A chat dock at the corner of the workspace (`Mod+J`, or the floating button).
It runs on this computer: the built-in brain answers offline from the
workspace itself, and an optional local model (Ollama, LM Studio, llama.cpp —
anything OpenAI-compatible) can answer first when one is turned on in the
dock's settings. Nothing is sent anywhere unless a local endpoint is
configured, and the built-in brain is the fallback for every turn.

## What it does

* **Runs what it offers.** An answer carries cards ("Start Sandbox", "Open
  the settings page", "Show FB Ads acc-02 in the list"); nothing runs until a
  card is pressed. A start or a stop goes through the same store the row menu
  uses — the same refusals, toasts and Undo.
* **Understands names through typos.** "start sandbx" finds "Sandbox";
  "FB" finds "FB Ads acc-02" on its word starts; quoted names always win.
* **Keeps the thread.** "stop them" after "start sandbox" stops Sandbox; with
  a selection in the list, "them" is the selection.
* **Answers about the app.** A local knowledge base (profiles, fingerprints,
  proxies, the browser engine, folders, views, sync, the Local API, Trash) is
  retrieved offline by TF-IDF over the question's words.
* **Reports the workspace.** Counts, what is running, and proxy health ranked
  by a real score (see below).

## How the brain works

The turn pipeline is pure TypeScript, tested without a browser
(`src/entities/assistant/lib/engine.test.ts`):

1. **Tokens** (`lib/tokenize.ts`): the sentence is folded (plurals and verb
   endings map to one stem) and quoted spans are kept as names.
2. **Slots** (`lib/slots.ts`): names, a count ("start 3"), "all", "the
   selected", a page ("go to settings") and a topic ("what is a fingerprint").
3. **Intent** (`lib/match.ts` + `model/intents.ts`): weighted trigger words,
   phrase bonuses and a continuity nudge from the turn before, scaled by
   sentence length. Below the confidence floor, the knowledge base answers
   instead, and below that the unknown answer offers ways on.
4. **Rows** (`lib/context.ts`): the sentence's names and leftover words are
   resolved against the profile list by the fuzzy ranker.
5. **Turn** (`lib/engine.ts`): the reply key, its variables, the action cards
   and the follow-up chips — as data. The store resolves the words
   (`t(...)`), the widget draws the cards.

## The shared algorithms (`src/shared/lib/smart/`)

* **fuzzy.ts** — a 0..1 likeness of one string to another: exact and prefix
  hits, word-initials matches ("fa" finds "FB Ads acc-02"), contiguous-run
  subsequence scoring and a Damerau-Levenshtein safety net for typos. Used by
  the assistant's name resolution and anything that must survive a typo.
* **textRank.ts** — TF-IDF vectors, cosine similarity and keyword selection,
  smoothed so a word in every document weighs nothing. Ranks the knowledge
  base's answers.
* **proxyScore.ts** — one health number per proxy: the Wilson score interval's
  95% lower bound for reliability (3 of 3 tests do not outrank 90 of 100), a
  latency term, a freshness decay and an exit-country match. `weakestLinks`
  names what pulls a proxy down.
* **scheduler.ts** — an adaptive batch runner: AIMD concurrency (the shape TCP
  uses), jittered exponential backoff for retries, injected clock and
  randomness so tests run in milliseconds.

Every module has unit tests (`*.test.ts`, `npm run test:unit`).

## A local model

The dock's settings (gear icon) take an endpoint and a model name:

* **Endpoint** — the server's base URL, e.g. `http://127.0.0.1:11434/v1` for
  Ollama's OpenAI-compatible API.
* **Model** — the name the server knows, e.g. `qwen2.5:3b`.
* **Answer with a local model** — when on, the question and a one-line
  workspace summary go to the model first. Any failure (off, unreachable,
  timeout) falls back to the built-in brain in the same turn.

The settings are kept on this computer (`shardx.assistant.llm`).

## Checks

`npm run check:assistant` (part of `npm run check`) validates the intent
database and the knowledge base against the locales: every reply,
suggestion and page key exists, every intent can actually match, and every
suggestion chip reads as a real question when it is sent. The checker is
pure (`scripts/check-assistant.mjs`) and tested on fixtures
(`scripts/check-assistant.test.mjs`).
