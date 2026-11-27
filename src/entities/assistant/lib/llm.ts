// The optional local model bridge: when the person runs a model server on
// this computer (Ollama, LM Studio, llama.cpp, anything OpenAI-compatible),
// the assistant may answer with it. Everything here is best-effort: a wrong
// endpoint, a down server or a timeout returns null and the built-in brain
// answers instead. Nothing leaves the configured endpoint (SEC-04 stays:
// this fetches only what the person typed into the dock's own settings).

export type LlmSettings = {
  /** The server's base URL, like http://127.0.0.1:11434/v1. */
  url: string;
  /** The model name the server knows. */
  model: string;
  /** On: ask the model first. Off: never call out. */
  on: boolean;
};

const KEY = "shardx.assistant.llm";
const EMPTY: LlmSettings = { url: "http://127.0.0.1:11434/v1", model: "qwen2.5:3b", on: false };

export function loadLlmSettings(): LlmSettings {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? "null") as Partial<LlmSettings> | null;
    if (raw && typeof raw === "object") {
      return {
        url: typeof raw.url === "string" && raw.url ? raw.url : EMPTY.url,
        model: typeof raw.model === "string" && raw.model ? raw.model : EMPTY.model,
        on: raw.on === true,
      };
    }
  } catch { /* broken or private: fall through */ }
  return { ...EMPTY };
}

export function saveLlmSettings(s: LlmSettings): void {
  try { localStorage.setItem(KEY, JSON.stringify(s)); } catch { /* private mode: session only */ }
}

/** One system prompt for every turn: who the assistant is, the workspace's
 *  numbers, and the words CONTEXT.md keeps. */
function systemPrompt(facts: string): string {
  return [
    "You are the ShardBrowser Launcher assistant, inside a desktop app that manages browser profiles.",
    "A profile is one saved browser identity (fingerprint, proxy, cookies). A session is one running profile.",
    "Answer in the language of the user's question, in two to four sentences, without markdown.",
    "Never invent profile names or numbers; the workspace facts are the truth.",
    `Workspace facts: ${facts}`,
  ].join(" ");
}

/** Asks the local model. Returns its answer, or null on any failure (the
 *  caller falls back to the built-in brain). A hard timeout keeps a hung
 *  server from holding the dock. */
export async function askLlm(question: string, facts: string, timeoutMs = 12_000): Promise<string | null> {
  const s = loadLlmSettings();
  if (!s.on || !s.url || !s.model) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const base = s.url.replace(/\/+$/, "");
    const res = await fetch(`${base}/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: s.model,
        messages: [
          { role: "system", content: systemPrompt(facts) },
          { role: "user", content: question },
        ],
        temperature: 0.3,
        max_tokens: 320,
      }),
      signal: controller.signal,
    });
    if (!res.ok) return null;
    const body = (await res.json()) as { choices?: { message?: { content?: unknown } }[] };
    const text = body.choices?.[0]?.message?.content;
    return typeof text === "string" && text.trim() ? text.trim() : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
