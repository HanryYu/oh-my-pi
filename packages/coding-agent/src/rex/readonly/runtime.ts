/**
 * A read-only capability host for the LOCAL focus/replay pipeline, not the
 * AgentTranscriptViewer fallback. Presentation imports use this same OMP
 * source tree; no executing session or historical extension host is created.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import * as crypto from "node:crypto";
import { TUI, type Component } from "@oh-my-pi/pi-tui/tui";
import { ProcessTerminal } from "@oh-my-pi/pi-tui/terminal";
import { matchesKey } from "@oh-my-pi/pi-tui/keys";
import { TranscriptBrowser } from "@oh-my-pi/pi-tui/chat/transcript-browser";
import { TranscriptContainer } from "@oh-my-pi/pi-tui/chrome/transcript-container";
import { isSettledJobFrame, parksAsBackground, settleWithJobFrame } from "@oh-my-pi/pi-tui/chat/tool-execution";
import { toolResultReplies } from "@oh-my-pi/pi-tui/chat/reply-write";
import type { AgentMessage } from "@oh-my-pi/pi-agent-core";
import { toolRenderers } from "@oh-my-pi/pi-tui/tools";
import { initTheme, theme } from "@oh-my-pi/pi-tui/theme";
import { truncateToWidth } from "@oh-my-pi/pi-tui/utils";
import { getBlobsDir } from "@oh-my-pi/pi-utils/dirs";
import { UiHelpers } from "../../modes/utils/ui-helpers";
import type { InteractiveModeContext } from "../../modes/types";
import { Settings } from "../../config/settings";
import { all as allSettings } from "../../config/registry";
import {
  cfgThemeDark, cfgThemeLight, cfgSymbolPreset, cfgColorBlindMode,
  cfgDisplayHideToolActivity, cfgTerminalShowImages,
} from "../../modes/settings";
import { parseSessionContent, resolveBlobRefsInEntries } from "../../session/session-loader";
import { migrateToCurrentVersion } from "../../session/session-migrations";
import { buildSessionContext } from "../../session/session-context";
import { BlobStore, lazyImageDataSync, type BlobPutResult } from "../../session/blob-store";
import type { FileEntry, SessionEntry, SessionHeader } from "../../session/session-entries";
import { sessionMessagePersistenceKey, sameMessageContent } from "../../session/turn-persistence";
import { cfgExpandThinkingBlocks, cfgProseOnlyThinking, cfgHideThinkingBlock } from "../../session/settings";
import { LiveState } from "./live";
import { NativeStreamView } from "./live-renderer";
import { SessionJournal } from "./journal";
import { WorkspaceSource, type SourceSelection } from "./security";

export interface ViewerOptions { root: string; module: string }
interface FileBinding { path: string; dev: number; ino: number; headerId?: string }

/** Image chip links may refer to existing blobs; the host NEVER materializes one. */
function existingBlobLink(data: Buffer): BlobPutResult {
  const hash = crypto.createHash("sha256").update(data).digest("hex");
  const file = path.join(getBlobsDir(), hash);
  return { hash, path: file, displayPath: fs.existsSync(file) ? file : "", ref: `blob:sha256:${hash}` };
}

export async function run(options: ViewerOptions): Promise<void> {
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw new Error("Read-only viewer requires a TTY");
  const source = new WorkspaceSource(options.root);
  // An effective global/project display configuration, loaded without a DB,
  // migration, marker file or writable configuration handle.
  const initial = source.read();
  const readOnlySettings = await Settings.loadReadOnly({ cwd: initial.cwd });
  // UiHelpers also reads the upstream global settings proxy. Seed an
  // in-memory singleton from the correctly merged read-only effective values;
  // treating global config as a late overlay would invert project precedence.
  const overrides = Object.fromEntries(allSettings().map(setting => [setting.id, setting.get(readOnlySettings)]).filter(([, value]) => value !== undefined));
  const settings = await Settings.init({ inMemory: true, cwd: initial.cwd, overrides });
  await initTheme(false, cfgSymbolPreset.get(settings), cfgColorBlindMode.get(settings), cfgThemeDark.get(settings), cfgThemeLight.get(settings));
  const terminal = new ProcessTerminal();
  const ui = new TUI(terminal, false);
  const blobs = new BlobStore(getBlobsDir()); // Constructor and get/size methods are read-only.
  const stopped = Promise.withResolvers<void>();
  let ending = false;
  let epoch = 0;
  let selection = "";
  let active: SourceSelection | undefined;
  let journal: SessionJournal | undefined;
  let entries: FileEntry[] = [];
  let durableMessages: AgentMessage[] = [];
  const durableReplies = new Map<string, readonly string[]>();
  let live = new LiveState(source.root);
  let replayedLiveRevision = -1;
  let expanded = true;
  let notice = "";
  let chat = new TranscriptContainer();
  let displayedChat = chat;
  let painting = false;
  let reading = false;
  let repaintRequested = false;
  const bindings = new Map<string, FileBinding>();
  let rejected = "";

  const sessionCapabilities = {
    getCwd: () => active?.cwd ?? initial.cwd,
    getSessionId: () => entries.find((entry): entry is SessionHeader => entry.type === "session")?.id ?? "",
    getArtifactsDir: () => journal?.file.slice(0, -6) ?? "",
    // UiHelpers uses these only for optional image-chip destinations. It must
    // not create blob sidecars or write into a session merely to display it.
    putBlobSync: existingBlobLink,
    putBlob: async (data: Buffer) => existingBlobLink(data),
    getConversationTip: () => undefined,
  };
  const viewCapabilities = {
    sessionManager: sessionCapabilities,
    get isStreaming() { return live.active; },
    activeToolExecutionUpdates: () => live.tools.filter(tool => tool.partialResult !== undefined).map(tool => ({ type: "tool_execution_update" as const, toolCallId: tool.toolCallId, toolName: tool.toolName, args: tool.args, partialResult: tool.partialResult! })),
    getToolByName: (_name: string) => undefined,
    hasBuiltInTool: (name: string) => Object.hasOwn(toolRenderers, name),
    retryAttempt: 0,
  };
  const displayContext = {
    ui,
    settings,
    get chatContainer() { return chat; },
    viewSession: viewCapabilities,
    sessionManager: sessionCapabilities,
    pendingTools: new Map(),
    transcriptMessageComponents: new Map(),
    lastAssistantUsage: undefined,
    servedModelTracker: undefined,
    get toolOutputExpanded() { return expanded; },
    effectiveHideThinkingBlock: cfgHideThinkingBlock.get(settings),
    proseOnlyThinking: cfgProseOnlyThinking.get(settings),
    expandThinkingBlocks: cfgExpandThinkingBlocks.get(settings),
    assistantImagesVisible: cfgTerminalShowImages.get(settings),
    tableChartsVisible: false,
    hideToolActivity: cfgDisplayHideToolActivity.get(settings),
    addMessageToChat: (message: Parameters<UiHelpers["addMessageToChat"]>[0], messageOptions?: Parameters<UiHelpers["addMessageToChat"]>[1]) => helpers.addMessageToChat(message, messageOptions),
  };
  // UiHelpers' declared host also includes execution/composer capabilities used
  // by unrelated methods. Only addMessageToChat/renderSessionContextIncrementally
  // are invoked; the read-only host deliberately does not implement that surface.
  const context = displayContext as unknown as InteractiveModeContext;
  const helpers = new UiHelpers(context);
  const streaming = new NativeStreamView(context);
  const browser = new TranscriptBrowser({
    getHeight: () => terminal.rows,
    minimumBodyRows: 0,
    followBottom: true,
    frame: ({ contentWidth }) => {
      const history = displayedChat.render(contentWidth);
      const tail = streaming.container.render(contentWidth);
      return {
        header: [truncateToWidth(active?.selected?.name ?? "Subagent", contentWidth)],
        body: { lines: notice ? [theme.fg("warning", truncateToWidth(notice, contentWidth))] : [...history, ...(history.length && tail.length ? [""] : []), ...tail] },
        footer: [theme.fg("dim", contentWidth >= 24 ? "↑↓  o expand  q close" : "↑↓  o  q")],
      };
    },
  });

  function disposeChat(): void {
    for (const tool of context.pendingTools.values()) tool.seal();
    context.pendingTools.clear();
    context.transcriptMessageComponents.clear();
    chat.dispose();
    chat.clear();
  }
  function clearSelection(): void {
    ++epoch;
    journal?.close();
    journal = undefined;
    entries = [];
    durableMessages = [];
    durableReplies.clear();
    live = new LiveState(source.root);
    replayedLiveRevision = -1;
    streaming.reset();
    active = undefined;
    if (!painting) disposeChat();
    browser.invalidate();
    ui.requestRender();
  }
  function assertSelection(generation: number): void {
    if (generation !== epoch || ending) throw new Error("Selection changed");
    const latest = source.read();
    if (latest.closed || `${latest.taskId}\0${latest.selected?.key ?? ""}` !== selection || latest.selected?.sessionFile !== journal?.file) throw new Error("Selection changed");
  }
  function syncLive(): void {
    for (const tool of live.tools) {
      const component = context.pendingTools.get(tool.toolCallId);
      if (!component) continue;
      if (tool.args !== undefined) component.updateArgs(tool.args, tool.toolCallId);
      component.setArgsComplete(tool.toolCallId);
      component.setExecutionStarted(tool.toolCallId);
      const frame = tool.ended ? tool.result : tool.partialResult;
      if (!frame) continue;
      const durable = durableMessages.findLast(message => message.role === "toolResult" && message.toolCallId === tool.toolCallId);
      const background = durable?.role === "toolResult" && parksAsBackground(tool.toolName, durable.details);
      if (background && isSettledJobFrame(frame.details)) {
        settleWithJobFrame(component, frame, tool.toolCallId);
        context.pendingTools.delete(tool.toolCallId);
      } else {
        component.updateResult({ ...frame, isError: tool.isError === true || frame.isError }, !tool.ended, tool.toolCallId);
        if (tool.ended && parksAsBackground(tool.toolName, frame.details)) component.parkAsBackground();
        else if (tool.ended) context.pendingTools.delete(tool.toolCallId);
      }
    }
    streaming.update(live.activeAssistant, live.tools, durableReplies);
    browser.invalidate();
    ui.requestRender();
  }
  async function paint(): Promise<void> {
    if (painting || ending) return;
    const generation = epoch;
    painting = true;
    const previous = displayedChat;
    chat = new TranscriptContainer();
    chat.setToolActivityVisible(!context.hideToolActivity);
    context.pendingTools.clear();
    context.transcriptMessageComponents.clear();
    try {
      const sessionEntries = entries.filter((entry): entry is SessionEntry => entry.type !== "session");
      const inFlightToolCallIds = new Set<string>();
      for (const entry of sessionEntries) {
        if (entry.type !== "message") continue;
        if (entry.message.role === "user") inFlightToolCallIds.clear();
        if (entry.message.role === "assistant") for (const part of entry.message.content) if (part.type === "toolCall") inFlightToolCallIds.add(part.id);
        if (entry.message.role === "toolResult") inFlightToolCallIds.delete(entry.message.toolCallId);
      }
      for (const tool of live.tools) inFlightToolCallIds.add(tool.toolCallId);
      const transcript = buildSessionContext(sessionEntries, undefined, undefined, {
        transcript: true,
        collapseCompactedHistory: false,
        inFlightToolCallIds: live.active || active?.selected?.status === "running" ? inFlightToolCallIds : undefined,
        resolveFrameData: data => lazyImageDataSync(blobs, data),
      });
      transcript.messages.push(...live.settling);
      await helpers.renderSessionContextIncrementally(transcript, {}, () => {
        assertSelection(generation);
        ui.requestRender();
      });
      assertSelection(generation);
      displayedChat = chat;
      replayedLiveRevision = live.revision;
      syncLive();
      notice = "";
      browser.invalidate();
      ui.requestRender();
    } finally {
      previous.dispose();
      previous.clear();
      painting = false;
    }
  }
  async function refresh(): Promise<void> {
    if (ending || reading || painting) return;
    reading = true;
    try {
      const next = source.read();
      if (next.closed) { finish(); return; }
      const token = `${next.taskId}\0${next.selected?.key ?? ""}`;
      if (token !== selection) {
        clearSelection();
        selection = token;
        rejected = "";
        browser.scrollToBottom();
      }
      active = next;
      if (!next.selected) { notice = "Select a subagent"; return; }
      if (!next.selected.sessionFile) { notice = "Waiting for the native session file"; return; }
      if (rejected === token) return;
      if (!journal) {
        const file = next.selected.sessionFile;
        const info = fs.lstatSync(file);
        const known = bindings.get(token);
        if (known && (known.path !== file || known.dev !== info.dev || known.ino !== info.ino)) { rejected = token; throw new Error("Selected transcript was replaced"); }
        journal = new SessionJournal(file);
        if (!known) bindings.set(token, { path: file, dev: info.dev, ino: info.ino });
      } else if (journal.file !== next.selected.sessionFile) {
        rejected = token;
        throw new Error("Selected transcript path changed");
      }
      const generation = epoch;
      const beforeRevision = live.revision;
      const liveChanged = live.poll(next);
      const update = await journal.poll(() => {
        assertSelection(generation);
        return false;
      });
      if (generation !== epoch || ending) return;
      if (update) {
        const parsed = parseSessionContent(update.text);
        if (update.reset) { entries = []; durableMessages = []; durableReplies.clear(); }
        for (const entry of parsed.entries) {
          entries.push(entry);
          if (entry.type === "message") durableMessages.push(entry.message);
          if (entry.type === "message" && entry.message.role === "toolResult") durableReplies.set(entry.message.toolCallId, toolResultReplies(entry.message.details));
        }
        const header = entries.find((entry): entry is SessionHeader => entry.type === "session");
        const binding = bindings.get(token)!;
        if (header && binding.headerId && header.id !== binding.headerId) { rejected = token; throw new Error("Transcript session identity changed"); }
        if (header) binding.headerId = header.id;
        migrateToCurrentVersion(entries); // In-memory migration; never persists.
        await resolveBlobRefsInEntries(parsed.entries, blobs);
        if (parsed.malformedRecords) throw new Error("Native transcript contains malformed records");
      }
      assertSelection(generation);
      if (update || live.revision !== beforeRevision) live.reconcile(durableMessages, sessionMessagePersistenceKey, sameMessageContent);
      if (update?.text || update?.reset || repaintRequested || replayedLiveRevision !== live.revision) {
        repaintRequested = false;
        await paint();
      } else if (liveChanged) {
        notice = "";
        syncLive();
      }
    } catch (error: unknown) {
      if (ending) return;
      const message = error instanceof Error ? error.message : String(error);
      if (message === "Selection changed") { clearSelection(); notice = ""; return; }
      // Do not keep an old agent's content on screen after its authority fails.
      clearSelection();
      notice = message;
    } finally {
      reading = false;
      browser.invalidate();
      if (!ending) ui.requestRender();
    }
  }
  function finish(): void {
    if (ending) return;
    ending = true;
    ++epoch;
    journal?.close();
    clearInterval(timer);
    ui.stop(); // ProcessTerminal restores raw mode, cursor and alternate screen.
    stopped.resolve();
  }
  async function closeDetail(): Promise<void> {
    finish(); // Restore the TTY before asking the controller to remove the pane.
    // The deployed controller may have updated since this immutable runtime
    // started. Execute only this runtime's trusted Python source, never code
    // chosen by --module; retain the controller path for its IPC namespace.
    const worker = path.resolve(import.meta.dir, "../../../rex-workspace/worker.py");
    const child = Bun.spawn(["/usr/bin/python3", worker, "request", "--root", source.root, "--module", options.module, "--view", "detail"], { stdin: "pipe", stdout: "ignore", stderr: "pipe", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", OMP_CONFIG_SYNC: "0" } });
    child.stdin.end(JSON.stringify({ type: "close-detail" }));
    const error = await new Response(child.stderr).text();
    if (await child.exited !== 0) process.stderr.write(error || "Could not close the detail pane\n");
  }
  const surface: Component = {
    render: width => browser.render(width),
    invalidate: () => browser.invalidate(),
    handleInput: data => {
      if (data === "q" || matchesKey(data, "escape") || matchesKey(data, "ctrl+c")) { void closeDetail().catch(error => process.stderr.write(`${String(error)}\n`)); return; }
      if (data === "o" || matchesKey(data, "ctrl+o")) { expanded = !expanded; repaintRequested = true; void refresh(); return; }
      if (data === "G" || matchesKey(data, "end")) browser.scrollToBottom();
      else if (data === "g" || matchesKey(data, "home")) browser.scrollToTop();
      else if (data === "j") browser.scroll(1);
      else if (data === "k") browser.scroll(-1);
      else browser.handleScrollKey(data);
      ui.requestRender();
    },
  };
  const timer = setInterval(() => { void refresh(); }, 50);
  const onSignal = () => { finish(); };
  const onEof = () => { finish(); };
  process.once("SIGINT", onSignal);
  process.once("SIGTERM", onSignal);
  process.once("SIGHUP", onSignal);
  process.stdin.once("end", onEof);
  try {
    ui.start();
    ui.showOverlay(surface, { fullscreen: true, width: "100%", maxHeight: "100%", mouseTracking: false, margin: 0 });
    await refresh();
    await stopped.promise;
  } finally {
    finish();
    streaming.reset();
    // A replay yields between chunks. Let its epoch guard unwind before
    // disposing the components it still owns.
    while (reading || painting) await Bun.sleep(1);
    disposeChat();
    process.off("SIGINT", onSignal);
    process.off("SIGTERM", onSignal);
    process.off("SIGHUP", onSignal);
    process.stdin.off("end", onEof);
  }
}
