import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { AgentMessage } from "@oh-my-pi/pi-agent-core";
import type { AssistantMessage } from "@oh-my-pi/pi-ai";
import { LiveState } from "../../src/rex/readonly/live";
import type { SourceSelection } from "../../src/rex/readonly/security";
import { sameMessageContent as same, sessionMessagePersistenceKey as identity } from "../../src/session/turn-persistence";

const fixtures: string[] = [];
afterEach(() => { for (const directory of fixtures.splice(0)) fs.rmSync(directory, { recursive: true, force: true }); });
const usage = { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
function message(text: string, timestamp = 1): AssistantMessage {
  return { role: "assistant", timestamp, api: "openai-responses", provider: "test", model: "model", usage, content: [{ type: "text", text }], stopReason: "stop" };
}
function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "rsv-live-"));
  fixtures.push(root);
  const selected = { key: "Agent:1", id: "Agent", generation: 1, name: "Agent", taskId: "current", sessionFile: "/native/main/Agent.jsonl", liveFile: "live-test.json", status: "running" };
  const selection: SourceSelection = { taskId: "current", sourceSessionFile: "/native/main.jsonl", sessionId: "main", ompExecutable: "/bin/omp", pid: process.pid, closed: false, selected, cwd: root };
  let seq = 0;
  const publish = (body: Record<string, unknown>, extra: Record<string, unknown> = {}) => {
    const row = { v: 1, key: selected.key, taskId: selection.taskId, sourceSessionFile: selection.sourceSessionFile, sessionFile: selected.sessionFile, seq: ++seq, tools: [], ...body, ...extra };
    const temporary = path.join(root, "next.json");
    fs.writeFileSync(temporary, JSON.stringify(row), { mode: 0o600 });
    fs.renameSync(temporary, path.join(root, selected.liveFile));
  };
  return { root, selection, publish, live: new LiveState(root) };
}
test("complete cumulative assistant snapshots exceed both 4000 chars and 1MiB", () => {
  const state = fixture();
  const first = message("partial ```ts\nconst value = 1");
  state.publish({ assistant: { streamId: "stream-1", ended: false, message: first } });
  expect(state.live.poll(state.selection)).toBe(true);
  expect(state.live.activeAssistant?.message).toEqual(first);
  const second = message("x".repeat(1100000) + "TAIL-MARKER");
  state.publish({ assistant: { streamId: "stream-1", ended: false, message: second } });
  state.live.poll(state.selection);
  expect(state.live.activeAssistant?.message).toEqual(second);
  expect(state.live.poll(state.selection)).toBe(false);
});
test("terminal handoff stays visible until exact native persisted message appears", () => {
  const state = fixture();
  const final = message("final answer");
  state.publish({ assistant: { streamId: "stream-1", ended: true, message: final } });
  state.live.poll(state.selection);
  expect(state.live.settling).toEqual([final]);
  state.publish({ assistant: { streamId: "stream-2", ended: false, message: message("new stream", 2) } });
  state.live.poll(state.selection);
  state.live.reconcile([message("different same-timestamp content")], identity, same);
  expect(state.live.settling).toEqual([final]);
  state.live.reconcile([final], identity, same);
  expect(state.live.settling).toEqual([]);
  expect(state.live.activeAssistant?.streamId).toBe("stream-2");
  const revision = state.live.revision;
  state.publish({ assistant: { streamId: "stream-1", ended: true, message: final } });
  state.live.poll(state.selection);
  expect(state.live.revision).toBe(revision);
  expect(state.live.settling).toEqual([]);
});
test("full tool partials and completed results hand off by native toolCallId", () => {
  const state = fixture();
  const partial = { content: [{ type: "text" as const, text: "output-".repeat(2000) }], details: { native: { progress: [1, 2, 3] } } };
  const tool = { toolCallId: "call-native", toolName: "bash", args: { command: "example" }, partialResult: partial, ended: false };
  state.publish({ tools: [tool] });
  state.live.poll(state.selection);
  expect(state.live.tools[0].partialResult).toEqual(partial);
  state.publish({ tools: [{ ...tool, result: partial, ended: true }] });
  state.live.poll(state.selection);
  state.publish({ tools: [] });
  state.live.poll(state.selection);
  expect(state.live.tools[0].result).toEqual(partial);
  const durable: AgentMessage = { role: "toolResult", toolCallId: "call-native", toolName: "bash", content: partial.content, isError: false, timestamp: 3 };
  state.live.reconcile([durable], identity, same);
  expect(state.live.tools).toEqual([]);
});
test("in-flight duplicate is suppressed if durable final beats its end snapshot", () => {
  const state = fixture();
  const final = message("complete");
  state.publish({ assistant: { streamId: "stream-1", ended: false, message: final } });
  state.live.poll(state.selection);
  state.live.reconcile([final], identity, same);
  expect(state.live.activeAssistant).toBeUndefined();
});
test("source/task/selected-generation mismatches never expose snapshot content", () => {
  const state = fixture();
  state.publish({ assistant: { streamId: "s", ended: false, message: message("wrong task") } }, { taskId: "previous" });
  expect(() => state.live.poll(state.selection)).toThrow("selected native session");
  state.publish({}, { key: "Agent:0" });
  expect(() => state.live.poll(state.selection)).toThrow("selected native session");
  state.publish({}, { sessionFile: "/elsewhere/secret.jsonl" });
  expect(() => state.live.poll(state.selection)).toThrow("selected native session");
});
test("partial JSON arguments survive unmodified transport metadata", () => {
  const state = fixture();
  const native = message("");
  native.content = [{ type: "toolCall", id: "call", name: "write", arguments: {} }];
  const json = '{"path":"file.ts","content":"' + "x".repeat(20000);
  state.publish({ assistant: { streamId: "s", ended: false, message: native, partialArgs: [{ index: 0, json }] } });
  state.live.poll(state.selection);
  expect(state.live.activeAssistant?.partialArgs).toEqual([{ index: 0, json }]);
});
