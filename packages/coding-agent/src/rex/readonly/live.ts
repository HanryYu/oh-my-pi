import * as fs from "node:fs";
import * as path from "node:path";
import type { AgentMessage } from "@oh-my-pi/pi-agent-core";
import type { AssistantMessage } from "@oh-my-pi/pi-ai";
import type { ToolExecutionHandle } from "@oh-my-pi/pi-tui/chat/tool-execution";
import { object, privateJson, type SourceSelection } from "./security";

type ToolResult = Parameters<ToolExecutionHandle["updateResult"]>[0];
export interface LiveAssistant {
  streamId: string;
  ended: boolean;
  message: AssistantMessage;
  partialArgs: Array<{ index: number; json: string }>;
}
export interface LiveTool {
  toolCallId: string;
  toolName: string;
  args: unknown;
  partialResult?: ToolResult;
  result?: ToolResult;
  isError?: boolean;
  ended: boolean;
}
export interface LiveSnapshot {
  seq: number;
  assistant?: LiveAssistant;
  tools: LiveTool[];
}
function nativeResult(value: unknown): ToolResult | undefined {
  if (value === undefined) return undefined;
  const result = object(value);
  if (!Array.isArray(result.content)) throw new Error("Invalid native tool result snapshot");
  // Native tool result details are renderer-specific. Their full original
  // content/details payload is preserved; no execution capability is attached.
  return result as unknown as ToolResult;
}
function assistant(value: unknown): LiveAssistant | undefined {
  if (value === undefined) return undefined;
  const row = object(value);
  const message = object(row.message);
  if (typeof row.streamId !== "string" || !row.streamId || typeof row.ended !== "boolean" || message.role !== "assistant" || !Array.isArray(message.content) || typeof message.timestamp !== "number" || typeof message.provider !== "string" || typeof message.model !== "string" || !message.usage || typeof message.usage !== "object") throw new Error("Invalid native assistant snapshot");
  for (const value of message.content) {
    const block = object(value);
    if (block.type === "text" && typeof block.text === "string") continue;
    if (block.type === "thinking" && typeof block.thinking === "string") continue;
    if (block.type === "toolCall" && typeof block.id === "string" && typeof block.name === "string" && "arguments" in block) continue;
    if (block.type === "image" && typeof block.data === "string" && typeof block.mimeType === "string") continue;
    if (block.type === "redactedThinking" && typeof block.data === "string") continue;
    if (block.type === "fallback" && typeof object(block.from).model === "string" && typeof object(block.to).model === "string") continue;
    if (block.type === "anthropicServerTool" && block.block && typeof block.block === "object") continue;
    throw new Error("Invalid native assistant content block");
  }
  const partialArgs: Array<{ index: number; json: string }> = [];
  if (row.partialArgs !== undefined) {
    if (!Array.isArray(row.partialArgs)) throw new Error("Invalid streaming argument metadata");
    for (const value of row.partialArgs) {
      const part = object(value);
      if (!Number.isInteger(part.index) || Number(part.index) < 0 || typeof part.json !== "string") throw new Error("Invalid streaming argument metadata");
      partialArgs.push({ index: Number(part.index), json: part.json });
    }
  }
  // Native persisted-session loading uses this same AssistantMessage shape.
  const nativeMessage = message as unknown as AssistantMessage;
  return { streamId: row.streamId, ended: row.ended, message: nativeMessage, partialArgs };
}
/** Current native state only; terminal handoff messages are retained in memory
 * until their exact native identity/content appears in the read-only journal. */
export class LiveState {
  snapshot: LiveSnapshot = { seq: -1, tools: [] };
  revision = 0;
  readonly #settling = new Map<string, AssistantMessage>();
  readonly #observedFinals = new Set<string>();
  readonly #settledTools = new Map<string, LiveTool>();
  #persistedToolIds = new Set<string>();
  #fileStamp = "";
  #durableByTimestamp = new Map<number, AssistantMessage[]>();
  #toolView: readonly LiveTool[] = [];
  #sameContent: ((left: AgentMessage, right: AgentMessage) => boolean) | undefined;
  constructor(readonly root: string) {}
  poll(selection: SourceSelection): boolean {
    const selected = selection.selected;
    if (!selected?.liveFile) return false;
    let row: Record<string, unknown>;
    try {
      const file = path.join(this.root, selected.liveFile);
      const info = fs.lstatSync(file);
      if (!info.isFile() || info.uid !== process.getuid!() || (info.mode & 0o777) !== 0o600 || info.nlink !== 1) throw new Error("Unsafe native live snapshot");
      const stamp = `${info.dev}:${info.ino}:${info.size}:${info.mtimeMs}`;
      if (stamp === this.#fileStamp) return false;
      row = privateJson(file, Number.MAX_SAFE_INTEGER);
      this.#fileStamp = stamp;
    }
    catch (error: unknown) {
      if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") return false;
      throw error;
    }
    if (row.v !== 1 || row.key !== selected.key || row.taskId !== selection.taskId || row.sourceSessionFile !== selection.sourceSessionFile || row.sessionFile !== selected.sessionFile || !Number.isSafeInteger(row.seq) || Number(row.seq) < 0 || !Array.isArray(row.tools)) throw new Error("Live snapshot does not belong to the selected native session");
    if (Number(row.seq) < this.snapshot.seq) throw new Error("Native live snapshot sequence moved backwards");
    if (Number(row.seq) === this.snapshot.seq) return false;
    const tools: LiveTool[] = [];
    const ids = new Set<string>();
    for (const value of row.tools) {
      const tool = object(value);
      if (typeof tool.toolCallId !== "string" || !tool.toolCallId || typeof tool.toolName !== "string" || !tool.toolName || typeof tool.ended !== "boolean" || ids.has(tool.toolCallId)) throw new Error("Invalid live native tool identity");
      ids.add(tool.toolCallId);
      tools.push({ toolCallId: tool.toolCallId, toolName: tool.toolName, args: tool.args, partialResult: nativeResult(tool.partialResult), result: nativeResult(tool.result), isError: tool.isError === true, ended: tool.ended });
    }
    const current = assistant(row.assistant);
    if (current?.ended && !this.#observedFinals.has(current.streamId)) {
      this.#observedFinals.add(current.streamId);
      this.#settling.set(current.streamId, current.message);
      this.revision++;
    }
    for (const tool of tools) if (tool.ended && !this.#persistedToolIds.has(tool.toolCallId)) this.#settledTools.set(tool.toolCallId, tool);
    this.snapshot = { seq: Number(row.seq), assistant: current, tools };
    this.#rebuildTools();
    return true;
  }
  reconcile(messages: readonly AgentMessage[], key: (message: AgentMessage) => string | undefined, same: (left: AgentMessage, right: AgentMessage) => boolean): void {
    this.#durableByTimestamp.clear();
    for (const message of messages) {
      if (message.role !== "assistant") continue;
      const group = this.#durableByTimestamp.get(message.timestamp);
      if (group) group.push(message);
      else this.#durableByTimestamp.set(message.timestamp, [message]);
    }
    this.#sameContent = same;
    this.#persistedToolIds = new Set(messages.filter(message => message.role === "toolResult").map(message => message.toolCallId));
    for (const id of this.#persistedToolIds) this.#settledTools.delete(id);
    for (const [streamId, pending] of this.#settling) {
      const identity = key(pending);
      if (identity && messages.some(message => key(message) === identity && same(message, pending))) {
        this.#settling.delete(streamId);
        this.revision++;
      }
    }
    this.#rebuildTools();
  }
  get settling(): readonly AssistantMessage[] { return [...this.#settling.values()]; }
  get activeAssistant(): LiveAssistant | undefined {
    const current = this.snapshot.assistant;
    if (!current || current.ended) return undefined;
    const message = current.message;
    // A durable final may become visible just before the end snapshot. Native
    // content equality disambiguates same-millisecond turns without dropping
    // a genuinely different message that happens to share identity metadata.
    if (this.#durableByTimestamp.get(message.timestamp)?.some(candidate => candidate.provider === message.provider && candidate.model === message.model && (!candidate.responseId || !message.responseId || candidate.responseId === message.responseId) && this.#sameContent?.(candidate, message))) return undefined;
    return current;
  }
  #rebuildTools(): void {
    const result = new Map(this.#settledTools);
    for (const tool of this.snapshot.tools) {
      if (tool.ended && this.#persistedToolIds.has(tool.toolCallId)) continue;
      result.set(tool.toolCallId, tool);
    }
    this.#toolView = [...result.values()];
  }
  get tools(): readonly LiveTool[] { return this.#toolView; }
  get active(): boolean { return Boolean(this.activeAssistant || this.#settling.size || this.tools.length); }
}
