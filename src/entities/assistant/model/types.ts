// What the assistant reads from a sentence and answers with. Pure shapes:
// the engine (lib/engine.ts) fills them, the widget draws them, and the
// intent database (model/intents.ts) stays plain data a script can check.

import type { Section } from "../../../shared/types";

/** Who spoke in the dock. */
export type ChatRole = "user" | "assistant";

/** A chat line. The bot's text is already resolved copy; a bot line may carry
 *  actions (run on a press) and follow-up suggestions (fill the input). */
export type ChatMessage = {
  id: number;
  role: ChatRole;
  text: string;
  /** Cards under a bot line: what the assistant offers to do. */
  actions?: AssistantAction[];
  /** Follow-up chips under a bot line. */
  suggestions?: string[];
  /** The bot was answered by the local model, not the built-in brain. */
  viaLlm?: boolean;
};

/** What the assistant offers to do about the answer. Nothing runs until a
 *  card is pressed: the assistant proposes, the person decides. */
export type AssistantAction =
  | { kind: "startProfiles"; ids: string[]; names: string[] }
  | { kind: "stopProfiles"; ids: string[]; names: string[] }
  | { kind: "filterProfiles"; ids: string[]; label: string }
  | { kind: "openAssign"; ids: string[] }
  | { kind: "navigate"; section: Section }
  | { kind: "runCommand"; commandId: string };

/** The intents the built-in brain knows. */
export type IntentId =
  | "greet"
  | "help"
  | "countProfiles"
  | "runningNow"
  | "startProfile"
  | "stopProfile"
  | "findProfile"
  | "proxyHealth"
  | "assignProxy"
  | "createProfile"
  | "openPage"
  | "explain"
  | "unknown";

/** Which entity a slot extracts. */
export type SlotKind = "names" | "count" | "page" | "topic" | "all" | "selected";

/** What a sentence named, before it is resolved against the app's rows. */
export type Slots = {
  /** Profile names as typed (quotes honored), resolved later by fuzzy match. */
  names: string[];
  /** A count the sentence asked for ("start 5"), if any. */
  count: number | null;
  /** The page a sentence names ("go to settings"). */
  page: Section | null;
  /** The knowledge topic a sentence names ("what is a fingerprint"). */
  topic: string | null;
  /** "all of them", "everything". */
  all: boolean;
  /** "the selected ones", "them" after a selection talk. */
  selected: boolean;
};

export type MatchResult = {
  intent: IntentId;
  /** 0..1; under the engine's threshold the knowledge base answers instead. */
  confidence: number;
  slots: Slots;
  /** The runner-ups, for the engine's ambiguity handling. */
  runnersUp: { intent: IntentId; confidence: number }[];
};

/** A row of the app as the assistant sees it (lib/context.ts builds one view). */
export type AssistantProfile = {
  id: string;
  name: string;
  running: boolean;
  pinned: boolean;
  folder: string;
  proxyId: string | null;
};

export type AssistantProxy = {
  id: string;
  name: string;
  country: string;
  /** From the last test, when the launcher has one. */
  latencyMs: number | null;
  status: "idle" | "testing" | "ok" | "fail";
};

/** Everything the brain may answer from. Built fresh for each turn. */
export type AssistantContext = {
  profiles: AssistantProfile[];
  proxies: AssistantProxy[];
  section: Section;
  /** The list's current selection. */
  selectedIds: string[];
};

/** A summary of the workspace, the numbers every count answer uses. */
export type WorkspaceSummary = {
  total: number;
  running: number;
  pinned: number;
  folders: number;
  noProxy: number;
  proxies: number;
  failedProxies: number;
};
