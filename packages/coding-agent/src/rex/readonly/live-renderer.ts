import type { Component } from "@oh-my-pi/pi-tui/tui";
import { TranscriptContainer } from "@oh-my-pi/pi-tui/chrome/transcript-container";
import { AssistantMessageComponent } from "@oh-my-pi/pi-tui/chat/assistant-message";
import type { ToolExecutionHandle } from "@oh-my-pi/pi-tui/chat/tool-execution";
import { getStreamingPartialJson, setStreamingPartialJson } from "@oh-my-pi/pi-ai/utils/block-symbols";
import { createAssistantMessageComponent } from "@oh-my-pi/pi-tui/prompt/interactive-context-helpers";
import { assistantHasVisibleContent, splitAssistantMessageToolTimeline } from "@oh-my-pi/pi-tui/chat/transcript-render-helpers";
import { isReplyWrite, replyDisplayMessage, toolResultReplies } from "@oh-my-pi/pi-tui/chat/reply-write";
import type { InteractiveModeContext } from "../../modes/types";
import { createToolCallBlock } from "../../modes/controllers/tool-call-block";
import { decodeStreamedToolArgs, streamingStringKeysForTool } from "../../modes/controllers/tool-args-reveal";
import { StreamingRevealController } from "../../modes/controllers/streaming-reveal";
import { cfgDisplaySmoothStreaming } from "../../modes/settings";
import type { LiveAssistant, LiveTool } from "./live";

/** Only production presentation components: no EventController, speech, session
 * subscription, lifecycle executor, extension code, editor, or tool execution. */
export class NativeStreamView {
  readonly container = new TranscriptContainer();
  readonly #reveal: StreamingRevealController;
  #streamId = "";
  #leading: AssistantMessageComponent | undefined;
  #cards = new Map<number, { id: string; reply: boolean; component: ToolExecutionHandle }>();
  #segments = new Map<number, AssistantMessageComponent>();
  #replies = new Map<string, AssistantMessageComponent>();
  constructor(readonly context: InteractiveModeContext) {
    this.#reveal = new StreamingRevealController({
      getSmoothStreaming: () => cfgDisplaySmoothStreaming.get(context.settings),
      getHideThinkingBlock: () => context.effectiveHideThinkingBlock,
      getProseOnlyThinking: () => context.proseOnlyThinking,
      requestRender: () => context.ui.requestRender(),
    });
    this.container.setToolActivityVisible(!context.hideToolActivity);
  }
  update(current: LiveAssistant | undefined, tools: readonly LiveTool[], durableReplies: ReadonlyMap<string, readonly string[]>): void {
    if (!current) {
      if (this.#streamId) this.reset();
      const ordered: Component[] = [];
      this.#appendReplies(tools, ordered, durableReplies);
      this.#mount(ordered);
      return;
    }
    const fresh = current.streamId !== this.#streamId;
    if (fresh) { this.reset(); this.#streamId = current.streamId; }
    // JSON transport cannot carry native Symbols. Restore the upstream symbol
    // on this private parsed snapshot, never on an executing agent's message.
    for (const part of current.partialArgs) {
      const block = current.message.content[part.index];
      if (!block || block.type !== "toolCall") throw new Error("Streaming argument index is not a tool call");
      setStreamingPartialJson(block, part.json);
    }
    const timeline = splitAssistantMessageToolTimeline(current.message);
    if (!this.#leading) this.#leading = createAssistantMessageComponent(this.context);
    this.#leading.setExpanded(this.context.toolOutputExpanded);
    if (fresh) this.#reveal.begin(this.#leading, timeline.beforeTools, timeline.hasToolCalls);
    else this.#reveal.setTarget(timeline.beforeTools, timeline.hasToolCalls);
    if (timeline.hasToolCalls && !this.#leading.isTranscriptBlockFinalized()) this.#leading.markTranscriptBlockFinalized();
    const ordered: Component[] = [this.#leading];
    const present = new Set<number>();
    for (let index = 0; index < current.message.content.length; index++) {
      const call = current.message.content[index];
      if (call.type !== "toolCall") continue;
      present.add(index);
      const nativeTool = this.context.viewSession.getToolByName(call.name);
      const name = nativeTool?.name ?? call.name;
      const partial = getStreamingPartialJson(call);
      const args = partial ? decodeStreamedToolArgs(partial, { rawInput: call.customWireName !== undefined, fullArgs: call.arguments, streamingStringKeys: streamingStringKeysForTool(name, call.customWireName !== undefined) }) : call.arguments;
      const reply = this.context.viewSession.hasBuiltInTool(name) && isReplyWrite(name, args) === true;
      let card = this.#cards.get(index);
      if (card && (card.id !== call.id || card.reply !== reply)) { card.component.seal(); card.component.dispose?.(); this.#cards.delete(index); card = undefined; }
      if (!card) {
        card = { id: call.id, reply, component: createToolCallBlock(this.context, { name, args, tool: nativeTool, id: call.id, cwd: this.context.viewSession.sessionManager.getCwd() }) };
        this.#cards.set(index, card);
      } else card.component.updateArgs(args, call.id);
      card.component.setExpanded(this.context.toolOutputExpanded);
      const execution = tools.find(tool => tool.toolCallId === call.id);
      if (execution) {
        card.component.setArgsComplete(call.id);
        card.component.setExecutionStarted(call.id);
        const result = execution.ended ? execution.result : execution.partialResult;
        if (result) card.component.updateResult({ ...result, isError: execution.isError === true }, !execution.ended, call.id);
      }
      ordered.push(card.component);
      const prose = timeline.afterToolCalls.get(call.id);
      if (prose && assistantHasVisibleContent(prose)) {
        let segment = this.#segments.get(index);
        if (!segment) { segment = createAssistantMessageComponent(this.context); this.#segments.set(index, segment); }
        segment.setExpanded(this.context.toolOutputExpanded);
        segment.updateContent(prose, { transient: true });
        ordered.push(segment);
      } else {
        this.#segments.get(index)?.dispose();
        this.#segments.delete(index);
      }
    }
    for (const [index, card] of this.#cards) if (!present.has(index)) { card.component.seal(); card.component.dispose?.(); this.#cards.delete(index); }
    for (const [index, segment] of this.#segments) if (!present.has(index)) { segment.dispose(); this.#segments.delete(index); }
    this.#appendReplies(tools, ordered, durableReplies);
    this.#mount(ordered);
  }
  #appendReplies(tools: readonly LiveTool[], ordered: Component[], durableReplies: ReadonlyMap<string, readonly string[]>): void {
    const present = new Set<string>();
    for (const tool of tools) {
      const frame = tool.ended ? tool.result : tool.partialResult;
      if (!frame) continue;
      const replies = toolResultReplies(frame.details);
      for (let index = 0; index < replies.length; index++) {
        if (durableReplies.get(tool.toolCallId)?.[index] === replies[index]) continue;
        const key = `${tool.toolCallId}:${index}`;
        present.add(key);
        let component = this.#replies.get(key);
        if (!component) { component = createAssistantMessageComponent(this.context); this.#replies.set(key, component); }
        component.setExpanded(this.context.toolOutputExpanded);
        component.updateContent(replyDisplayMessage(replies[index]), { transient: !tool.ended });
        if (tool.ended) component.markTranscriptBlockFinalized();
        ordered.push(component);
      }
    }
    for (const [key, component] of this.#replies) if (!present.has(key)) { component.dispose(); this.#replies.delete(key); }
  }
  #mount(ordered: Component[]): void {
    if (ordered.length !== this.container.children.length || ordered.some((component, index) => component !== this.container.children[index])) {
      this.container.clear();
      for (const component of ordered) this.container.addChild(component);
    }
  }
  reset(): void {
    this.#reveal.stop();
    for (const card of this.#cards.values()) card.component.seal();
    this.container.dispose();
    this.container.clear();
    this.#cards.clear();
    this.#segments.clear();
    this.#replies.clear();
    this.#leading = undefined;
    this.#streamId = "";
  }
}
