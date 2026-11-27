import { useEffect, useRef, useState } from "react";
import { Button, IconButton, Input, Modal, Switch } from "@proxyshard/shardx-ui-kit";
import { AssistantIcon, ClearIcon, CloseIcon, NavSettingsIcon, SendIcon, SpinnerIcon } from "../../shared/icons";
import { useT } from "../../shared/i18n";
import { toast } from "../../shared/model/toast";
import {
  actionLabel,
  intentOf,
  loadLlmSettings,
  saveLlmSettings,
  useAssistant,
  type ChatMessage,
  type LlmSettings,
} from "../../entities/assistant";

/// The assistant's dock: a small chat window at the corner of the workspace,
/// opened by its floating button or Mod+J. The built-in brain answers offline
/// from the workspace itself; when the person turns on a local model in the
/// dock's settings, questions go to that model first and fall back to the
/// brain whenever it is off or unreachable.
export function AssistantHost() {
  const t = useT();
  const open = useAssistant((s) => s.open);
  const toggle = useAssistant((s) => s.toggle);
  return (
    <>
      {!open && (
        <IconButton
          icon={<AssistantIcon />}
          label={t("assistant.open")}
          variant="primary"
          mode="filled"
          size="medium"
          onClick={toggle}
          className="fixed right-4 bottom-4 z-(--layer-overlay) rounded-full shadow-(--shadow-float)"
        />
      )}
      {open && <AssistantDock />}
    </>
  );
}

function AssistantDock() {
  const t = useT();
  const messages = useAssistant((s) => s.messages);
  const busy = useAssistant((s) => s.busy);
  const setOpen = useAssistant((s) => s.setOpen);
  const clear = useAssistant((s) => s.clear);
  const send = useAssistant((s) => s.send);
  const [draft, setDraft] = useState("");
  const [settings, setSettings] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // New lines show themselves; the input keeps the keyboard on open.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, busy]);
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const submit = () => {
    const line = draft.trim();
    if (!line || busy) return;
    setDraft("");
    void send(line);
  };

  return (
    <section
      data-assistant-dock
      aria-label={t("assistant.title")}
      className="fixed right-4 bottom-4 z-(--layer-overlay) flex h-[540px] max-h-[calc(100vh-2rem)] w-[380px] max-w-[calc(100vw-2rem)] flex-col overflow-hidden rounded-surface bg-bg-white-0 shadow-(--shadow-float)"
    >
      <header className="flex shrink-0 items-center gap-2 px-3 pt-2.5 pb-1.5">
        <AssistantIcon className="size-3.5 shrink-0 text-primary-text" />
        <h2 className="flex-1 truncate text-secondary font-medium text-text-strong-950">{t("assistant.title")}</h2>
        <IconButton
          icon={<NavSettingsIcon />}
          label={t("assistant.settings")}
          onClick={() => setSettings(true)}
        />
        <IconButton
          icon={<ClearIcon />}
          label={t("assistant.clear")}
          onClick={() => {
            clear();
            toast.ok(t("assistant.cleared"));
          }}
        />
        <IconButton icon={<CloseIcon />} label={t("common.close")} onClick={() => setOpen(false)} />
      </header>

      <div ref={logRef} role="log" aria-live="polite" className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto overscroll-contain px-3 py-2">
        {messages.length === 0 && <Greeting onPick={(line) => void send(line)} />}
        {messages.map((m) => (
          <MessageLine key={m.id} message={m} onPick={(line) => void send(line)} />
        ))}
        {busy && (
          <p className="m-0 flex items-center gap-1.5 text-secondary text-text-sub-600">
            <SpinnerIcon className="size-3.5 animate-spin" />
            {t("assistant.thinking")}
          </p>
        )}
      </div>

      <footer className="flex shrink-0 items-center gap-1.5 px-3 pt-1.5 pb-3">
        <Input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder={t("assistant.placeholder")}
          aria-label={t("assistant.placeholder")}
          className="min-w-0 flex-1"
        />
        <IconButton
          icon={<SendIcon />}
          label={t("assistant.send")}
          variant="primary"
          mode="filled"
          onClick={submit}
          disabled={busy || !draft.trim()}
        />
      </footer>

      <LlmSettings open={settings} onClose={() => setSettings(false)} />
    </section>
  );
}

/** The empty dock's first words, with the greet intent's own follow-ups. */
function Greeting({ onPick }: { onPick: (line: string) => void }) {
  const t = useT();
  return (
    <div className="flex flex-col gap-2">
      <div className="max-w-[92%] self-start rounded-control bg-bg-weak-50 px-3 py-2 text-body text-text-strong-950">
        {t("assistant.greeting")}
      </div>
      <SuggestionRow suggestions={intentOf("greet").suggestions ?? []} onPick={onPick} />
    </div>
  );
}

/** One chat line: the person's words, or the assistant's answer with the
 *  cards it offers and the follow-ups it suggests. */
function MessageLine({ message, onPick }: { message: ChatMessage; onPick: (line: string) => void }) {
  const runCard = useAssistant((s) => s.runCard);
  if (message.role === "user") {
    return (
      <p className="m-0 max-w-[92%] self-end rounded-control bg-bg-weak-50 px-3 py-2 text-body text-text-strong-950">
        {message.text}
      </p>
    );
  }
  return (
    <div className="flex flex-col items-start gap-2">
      <p className="m-0 max-w-[92%] rounded-control px-1 py-0.5 text-body text-text-strong-950">{message.text}</p>
      {message.actions?.map((action, i) => (
        <Button
          key={`${message.id}-${i}`}
          variant="primary"
          mode="stroke"
          size="xsmall"
          onClick={() => void runCard(action)}
        >
          {actionLabel(action)}
        </Button>
      ))}
      {message.suggestions && <SuggestionRow suggestions={message.suggestions} onPick={onPick} />}
    </div>
  );
}

/** The follow-up chips: each one is a line the person can send as it is. */
function SuggestionRow({ suggestions, onPick }: { suggestions: readonly string[]; onPick: (line: string) => void }) {
  const t = useT();
  return (
    <div className="flex flex-wrap gap-1.5">
      {suggestions.map((key) => (
        <Button key={key} variant="neutral" mode="ghost" size="xsmall" onClick={() => onPick(t(key))}>
          {t(key)}
        </Button>
      ))}
    </div>
  );
}

/** The dock's own settings: an optional local model, kept on this computer. */
function LlmSettings({ open, onClose }: { open: boolean; onClose: () => void }) {
  const t = useT();
  const [draft, setDraft] = useState<LlmSettings>(loadLlmSettings);
  // Saving on close keeps the fields editable while the server answers.
  useEffect(() => {
    if (!open) return;
    setDraft(loadLlmSettings());
  }, [open]);
  const close = () => {
    saveLlmSettings(draft);
    onClose();
  };
  return (
    <Modal open={open} onClose={close} size="sm" aria-label={t("assistant.llmTitle")} closeLabel={t("common.close")}>
      <div className="flex flex-col gap-3 px-2 pb-2 pt-4">
        <h2 className="m-0 text-panel-title text-text-strong-950">{t("assistant.llmTitle")}</h2>
        <p className="m-0 text-body text-text-sub-600">
          {t.rich("assistant.llmHelp", { code: (s) => <code className="rounded-chip-sm bg-bg-weak-50 px-1 py-0.5 text-ui">{s}</code> })}
        </p>
        <Switch
          label={t("assistant.llmOn")}
          checked={draft.on}
          onChange={(on) => setDraft((d) => ({ ...d, on }))}
        />
        <Input
          label={t("assistant.llmUrl")}
          value={draft.url}
          technical
          onChange={(e) => setDraft((d) => ({ ...d, url: e.target.value }))}
        />
        <Input
          label={t("assistant.llmModel")}
          value={draft.model}
          technical
          onChange={(e) => setDraft((d) => ({ ...d, model: e.target.value }))}
        />
        <div className="flex justify-end">
          <Button variant="primary" mode="filled" size="xsmall" onClick={close}>
            {t("common.close")}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
