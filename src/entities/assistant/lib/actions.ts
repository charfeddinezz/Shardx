// The bridge from an offered card to the app: this is the only impure part
// of the assistant. Each action runs against the same stores the lists use,
// so a start from the chat is a start from the row menu — same refusals, same
// toasts, same Undo.

import { useNav } from "../../../shared/model/navigation";
import { toast } from "../../../shared/model/toast";
import { t } from "../../../shared/i18n";
import { command } from "../../../shared/commands";
import { useProfile, useProfileUi } from "../../profile";
import { useProxy } from "../../proxy";
import { PAGE_NAME_KEYS } from "./engine";
import type { AssistantAction, AssistantContext, AssistantProfile, AssistantProxy } from "../model/types";

/** The workspace as the assistant reads it, from the live stores. */
export function buildContext(): AssistantContext {
  const profiles = useProfile.getState();
  const ui = useProfileUi.getState();
  const proxiesStore = useProxy.getState();
  const rows: AssistantProfile[] = profiles.profiles.map((p) => ({
    id: p.id,
    name: p.name,
    running: !!profiles.running[p.id],
    pinned: p.pinned,
    folder: p.folder,
    proxyId: p.proxy_id,
  }));
  const proxies: AssistantProxy[] = profiles.proxies.map((x) => {
    const snap = proxiesStore.snapshots[x.id];
    return {
      id: x.id,
      name: x.name,
      country: x.country,
      latencyMs: snap?.tcp_ms ?? null,
      status: proxiesStore.proxyTesting[x.id] ? "testing" : snap ? "ok" : "idle",
    };
  });
  return { profiles: rows, proxies, section: useNav.getState().section, selectedIds: [...ui.selected] };
}

/** Runs one offered card. Returns false when it did nothing (rows that went
 *  away), so the dock can say so. */
export async function runAction(action: AssistantAction): Promise<boolean> {
  const store = useProfile.getState();
  switch (action.kind) {
    case "startProfiles":
    case "stopProfiles": {
      const wanted = new Set(action.ids);
      const rows = store.profiles.filter((p) => wanted.has(p.id));
      if (!rows.length) return false;
      const failed = action.kind === "startProfiles" ? await store.launchMany(rows.map((p) => p.id)) : await store.stopMany(rows.map((p) => p.id));
      return failed.length < rows.length;
    }
    case "filterProfiles": {
      const ui = useProfileUi.getState();
      await useNav.getState().setSection("profiles");
      ui.setOnly({ label: action.label, ids: action.ids });
      return true;
    }
    case "navigate":
      return useNav.getState().setSection(action.section);
    case "openAssign": {
      await useNav.getState().setSection("profiles");
      useProfileUi.getState().openAssign(action.ids);
      return true;
    }
    case "runCommand": {
      try {
        const c = command(action.commandId);
        void c.run?.({ ids: [] }, { from: "palette" });
        return true;
      } catch {
        return false;
      }
    }
    default:
      return false;
  }
}

/** The words on a card: the action's own verb with its subject. A runCommand
 *  card carries the command's own label, so it reads the same as the palette
 *  ("New profile"), and the fallback keeps a readable line. */
export function actionLabel(action: AssistantAction): string {
  const names3 = (names: string[]): string =>
    names.length > 3 ? `${names.slice(0, 3).join(", ")} +${names.length - 3}` : names.join(", ");
  switch (action.kind) {
    case "startProfiles":
      return t("assistant.action.start", { names: names3(action.names), n: action.names.length });
    case "stopProfiles":
      return t("assistant.action.stop", { names: names3(action.names), n: action.names.length });
    case "filterProfiles":
      return t("assistant.action.filter", { names: names3(action.label.split(", ")), n: action.ids.length });
    case "navigate":
      return t("assistant.action.navigate", { page: t(PAGE_NAME_KEYS[action.section]) });
    case "openAssign":
      return t("assistant.action.assign");
    case "runCommand": {
      try {
        return command(action.commandId).label(1);
      } catch {
        return t("assistant.action.command", { name: action.commandId });
      }
    }
  }
}

/** Reports a card that did nothing, without naming a raw error (check:errors). */
export function reportInert(): void {
  toast.warn({ title: t("assistant.actionInert") });
}
