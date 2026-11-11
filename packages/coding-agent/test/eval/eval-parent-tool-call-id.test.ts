import { afterEach, describe, expect, it, vi } from "bun:test";
import type { AgentTool } from "@oh-my-pi/pi-agent-core";
import { AsyncJobManager } from "@oh-my-pi/pi-coding-agent/async";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import * as evalIndex from "@oh-my-pi/pi-coding-agent/eval";
import type { ExecutorBackendExecOptions } from "@oh-my-pi/pi-coding-agent/eval/backend";
import { executeWithKernelBase } from "@oh-my-pi/pi-coding-agent/eval/executor-base";
import * as jsExecutor from "@oh-my-pi/pi-coding-agent/eval/js/executor";
import * as pythonExecutor from "@oh-my-pi/pi-coding-agent/eval/py/executor";
import {
  disposeAllVmContexts,
  executeInVmContext,
  type JsEvalWorkerHandle,
  setJsEvalWorkerFactoriesForTests,
} from "@oh-my-pi/pi-coding-agent/eval/js/context-manager";
import { callSessionTool } from "@oh-my-pi/pi-coding-agent/eval/js/tool-bridge";
import type { WorkerInbound, WorkerOutbound } from "@oh-my-pi/pi-coding-agent/eval/js/worker-protocol";
import { disposePyToolBridge, ensurePyToolBridge, registerPyToolBridge } from "@oh-my-pi/pi-coding-agent/eval/py/tool-bridge";
import { EvalTool } from "@oh-my-pi/pi-coding-agent/tools/eval";
import type { ToolSession } from "@oh-my-pi/pi-coding-agent/tools";

const managers: AsyncJobManager[] = [];
let restoreFactories: (() => void) | undefined;

afterEach(async () => {
  restoreFactories?.();
  restoreFactories = undefined;
  await disposeAllVmContexts();
  await disposePyToolBridge();
  await Promise.all(managers.splice(0).map(manager => manager.dispose()));
  vi.restoreAllMocks();
});

function sessionFor(ids: string[]): ToolSession {
  const task = {
    name: "task", label: "task", description: "record causal tool ids",
    parameters: { type: "object" },
    async execute(id: string) {
      ids.push(id);
      return { content: [{ type: "text", text: "done" }] };
    },
  } as unknown as AgentTool;
  return {
    cwd: process.cwd(), hasUI: false, settings: Settings.isolated(),
    getSessionFile: () => null, getSessionSpawns: () => null,
    getToolByName: name => name === "task" ? task : undefined,
  };
}

const result = {
  output: "", exitCode: 0, cancelled: false, truncated: false,
  artifactId: undefined, totalLines: 0, totalBytes: 0, outputLines: 0, outputBytes: 0, displayOutputs: [],
  stdinRequested: false,
};

describe("eval parent tool causality", () => {
  it("passes the outer owner through both language backend adapters", async () => {
    const session = sessionFor([]);
    const js = vi.spyOn(jsExecutor, "executeJs").mockResolvedValue(result);
    const python = vi.spyOn(pythonExecutor, "executePython").mockResolvedValue(result);
    const options: ExecutorBackendExecOptions = {
      parentToolCallId: "outer-adapter", session, cwd: process.cwd(), sessionId: "adapter",
      sessionFile: undefined, kernelOwnerId: undefined, reset: false, onChunk() {},
    };
    await evalIndex.jsBackend.execute("", options);
    await evalIndex.pythonBackend.execute("", options);
    expect(js.mock.calls[0]?.[1].parentToolCallId).toBe("outer-adapter");
    expect(python.mock.calls[0]?.[1]?.parentToolCallId).toBe("outer-adapter");
  });

  it("gives same-cell delegated task calls independent ids under the real outer call", async () => {
    const ids: string[] = [];
    const session = sessionFor(ids);
    await Promise.all([
      callSessionTool("task", {}, { session, parentToolCallId: "outer-eval" }),
      callSessionTool("task", {}, { session, parentToolCallId: "outer-eval" }),
    ]);
    expect(ids).toHaveLength(2);
    expect(new Set(ids).size).toBe(2);
    for (const id of ids) expect(id).toMatch(/^outer-eval:[0-9a-f-]{36}$/);
  });

  it("retains the original EvalTool owner after backgrounding and a newer eval", async () => {
    const ids: string[] = [];
    const manager = new AsyncJobManager({});
    managers.push(manager);
    const session = sessionFor(ids);
    session.asyncJobManager = manager;
    session.settings = Settings.isolated({ "eval.autoBackground.enabled": true, "eval.autoBackground.thresholdMs": 0 });
    const started = Promise.withResolvers<void>();
    const release = Promise.withResolvers<void>();
    const seen: string[] = [];
    vi.spyOn(evalIndex.jsBackend, "execute").mockImplementation(async (code: string, options: ExecutorBackendExecOptions) => {
      if (code === "old") { started.resolve(); await release.promise; }
      seen.push(options.parentToolCallId!);
      await callSessionTool("task", {}, { session: options.session, parentToolCallId: options.parentToolCallId });
      return result;
    });
    const tool = new EvalTool(session);
    try {
      await tool.execute("old-outer", { language: "js", code: "old" });
      await started.promise;
      await tool.execute("new-outer", { language: "js", code: "new" });
      release.resolve();
      await manager.waitForAll();
      expect(seen.sort()).toEqual(["new-outer", "old-outer"]);
      expect(ids.some(id => id.startsWith("old-outer:"))).toBe(true);
      expect(ids.some(id => id.startsWith("new-outer:"))).toBe(true);
    } finally { release.resolve(); }
  });

  it("routes delayed JavaScript worker calls by their captured run rather than the latest cell", async () => {
    const ids: string[] = [];
    const session = sessionFor(ids);
    const listeners = new Set<(message: WorkerOutbound) => void>();
    const runs = new Map<string, string>();
    const started = Promise.withResolvers<void>();
    const emit = (message: WorkerOutbound) => { for (const listener of listeners) listener(message); };
    const worker: JsEvalWorkerHandle = {
      mode: "process",
      send(message: WorkerInbound) {
        if (message.type === "init") queueMicrotask(() => emit({ type: "ready" }));
        if (message.type === "run") {
          runs.set(message.code, message.runId);
          if (runs.size === 2) started.resolve();
        }
        if (message.type === "tool-reply") {
          const runId = message.id.slice("nested:".length);
          queueMicrotask(() => emit({ type: "result", runId, ok: true }));
        }
      },
      onMessage(handler) { listeners.add(handler); return () => listeners.delete(handler); },
      onError() { return () => {}; },
      async close() { return true; },
      async terminate() { listeners.clear(); },
    };
    restoreFactories = setJsEvalWorkerFactoriesForTests({ spawnProcess: () => worker });
    const identity = crypto.randomUUID();
    const execute = (owner: string) => executeInVmContext({
      sessionKey: identity, sessionId: identity, cwd: process.cwd(), session,
      code: owner, filename: `${owner}.js`, runState: { parentToolCallId: owner },
    });
    const old = execute("old-outer");
    const newer = execute("new-outer");
    await started.promise;
    for (const name of ["new-outer", "old-outer"]) {
      const runId = runs.get(name)!;
      emit({ type: "tool-call", id: `nested:${runId}`, runId, name: "task", args: {} });
    }
    await Promise.all([old, newer]);
    expect(ids).toHaveLength(2);
    expect(ids[0]).toStartWith("new-outer:");
    expect(ids[1]).toStartWith("old-outer:");
  });

  it("captures the Python execution owner in its per-run HTTP registration", async () => {
    const ids: string[] = [];
    const session = sessionFor(ids);
    const bridge = await ensurePyToolBridge();
    const sessionId = crypto.randomUUID();
    class Cancelled extends Error {
      constructor(public timedOut: boolean) { super("cancelled"); }
    }
    await executeWithKernelBase({
      kernel: {
        async execute(_code, options) {
          const response = await fetch(`${bridge.url}/v1/tool`, {
            method: "POST", headers: { authorization: `Bearer ${bridge.token}`, "content-type": "application/json" },
            body: JSON.stringify({ session: sessionId, run: options.id, name: "task", args: {} }),
          });
          expect(await response.json()).toEqual({ ok: true, value: "done" });
          return { status: "ok", cancelled: false, timedOut: false };
        },
      },
      code: "",
      options: { toolSession: session, bridgeSessionId: sessionId, parentToolCallId: "python-outer" },
      runIdPrefix: "py-owner", errorLogLabel: "Python", cancelledErrorClass: Cancelled,
      buildKernelEnvPatch: () => ({}),
      formatKernelTimeoutAnnotation: () => "",
      formatTimeoutAnnotation: () => undefined,
    });
    expect(ids).toHaveLength(1);
    expect(ids[0]).toStartWith("python-outer:");
  });

  it("routes Python HTTP calls by their original registration after a newer run registers", async () => {
    const ids: string[] = [];
    const session = sessionFor(ids);
    const bridge = await ensurePyToolBridge();
    const sessionId = crypto.randomUUID();
    const old = registerPyToolBridge(sessionId, "old", { toolSession: session, parentToolCallId: "old-outer" });
    const newer = registerPyToolBridge(sessionId, "new", { toolSession: session, parentToolCallId: "new-outer" });
    try {
      for (const run of ["new", "old"]) {
        const response = await fetch(`${bridge.url}/v1/tool`, {
          method: "POST", headers: { authorization: `Bearer ${bridge.token}`, "content-type": "application/json" },
          body: JSON.stringify({ session: sessionId, run, name: "task", args: {} }),
        });
        expect(await response.json()).toEqual({ ok: true, value: "done" });
      }
      expect(ids[0]).toStartWith("new-outer:");
      expect(ids[1]).toStartWith("old-outer:");
    } finally { old(); newer(); }
  });
});
