import { expect, test } from "bun:test";
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { ModelRegistry } from "../../src/config/model-registry";
import { Settings } from "../../src/config/settings";
import {
	bindPreparedExtensions,
	importExtensionModule,
	loadExtensionFromFactory,
	loadExtensions,
} from "../../src/extensibility/extensions/loader";
import type { Extension, ExtensionAPI, ExtensionContext } from "../../src/extensibility/extensions/types";
import { rexWorkspaceBridge } from "../../src/rex/bridge";
import { mountRexBridge, REX_BRIDGE_SOURCE } from "../../src/rex/mount";
import { createAgentSession } from "../../src/sdk";
import { SessionManager } from "../../src/session/session-manager";
import { EventBus } from "../../src/utils/event-bus";
import { createInMemoryAuthStorage } from "../helpers/agent-session-setup";

type Ref = { id: string; generation: number; parentId: string; sessionFile: string; kind: string;
	displayName: string; status: string; createdAt: number; activity: string; session: { instanceKey: string } | null };
type RegistryEvent = { type: string; ref: Ref };
type Hook = (event: Record<string, unknown>, ctx: ExtensionContext) => unknown;
type FakeAgent = { emit(name: string, event?: Record<string, unknown>): unknown;
	frame(event: Record<string, unknown>): void; switchSession(file: string): void };
type Root = { v: 3; taskId: string; sessionFile: string; ompExecutable: string;
	agents: Array<{ id: string; name: string; key: string; liveFile: string; activity: string; status: string; taskId: string }>;
	diagnostics: Array<{ agentId: string }> };
type Live = { v: number; key: string; taskId: string; sourceSessionFile: string; sessionFile: string; seq: number;
	assistant?: { streamId: string; ended: boolean; message: Record<string, unknown>; partialArgs?: Array<{ index: number; json: string }> };
	tools: Array<{ toolCallId: string; args: unknown; partialResult?: unknown; result?: unknown; ended: boolean; isError?: boolean }> };

test("native request ownership, worker evidence, park/removal and fail-closed eval provenance", async () => {
	const saved = { REX_BLOCK: process.env.REX_BLOCK, REX_SESSION: process.env.REX_SESSION,
		REX_SUBAGENT_VIEWER: process.env.REX_SUBAGENT_VIEWER, REX_WORKSPACE_WIDTH: process.env.REX_WORKSPACE_WIDTH,
		REX_WORKSPACE_SCAN_DIR: process.env.REX_WORKSPACE_SCAN_DIR, REX_WORKSPACE_OMP: process.env.REX_WORKSPACE_OMP };
	const scan = fs.mkdtempSync("/tmp/rex-bridge-request-");
	process.env.REX_WORKSPACE_SCAN_DIR = scan;
	delete process.env.REX_WORKSPACE_OMP;
	process.env.REX_BLOCK = `bridge-smoke-${crypto.randomUUID()}`;
	process.env.REX_SESSION = "bridge-smoke";
	process.env.REX_SUBAGENT_VIEWER = "1";
	process.env.REX_WORKSPACE_WIDTH = "25";
	const hash = crypto.createHash("sha256").update(process.env.REX_BLOCK).digest("hex").slice(0, 10);
	const directory = path.join(scan, `rsv-${process.getuid?.()}-${process.pid}-${hash}`);

	const refs = new Map<string, Ref>();
	const listeners = new Set<(event: RegistryEvent) => void>();
	const registry = { get: (id: string) => refs.get(id), list: () => [...refs.values()],
		onChange(listener: (event: RegistryEvent) => void) { listeners.add(listener); return () => listeners.delete(listener); } };
	const timers = new Map<number, { callback: () => void; repeat: boolean }>();
	let timerId = 0;
	const warnings: string[] = [];
	function factory(id: string, kind: string, file: string): FakeAgent {
		const handlers = new Map<string, Hook>();
		const channels = new Map<string, Set<(event: Record<string, unknown>) => void>>();
		const manager = { getSessionId: () => file, getSessionFile: () => file,
			getSessionName: () => undefined, getArtifactsDir: () => file.slice(0, -6) };
		const ctx = { agent: { id, kind }, cwd: "/tmp", sessionManager: manager,
			setTimeout(callback: () => void) { const id = ++timerId; timers.set(id, { callback, repeat: false }); return id; },
			setInterval(callback: () => void) { const id = ++timerId; timers.set(id, { callback, repeat: true }); return id; },
			clearTimer(id: number) { timers.delete(id); }, ui: { notify() {} } };
		const pi = { pi: { AgentRegistry: { global: () => registry } }, logger: { warn: (value: string) => warnings.push(value) },
			on(name: string, handler: Hook) { handlers.set(name, handler); },
			registerCommand() {}, events: { on(name: string, handler: (event: Record<string, unknown>) => void) {
				const set = channels.get(name) || new Set(); channels.set(name, set); set.add(handler); return () => set.delete(handler);
			} } };
		// The fixture supplies only the public extension surface used by the bridge.
		rexWorkspaceBridge(pi as unknown as ExtensionAPI);
		return { emit(name: string, event: Record<string, unknown> = {}) { return handlers.get(name)?.(event, ctx as unknown as ExtensionContext); },
			frame(event: Record<string, unknown>) { for (const handler of channels.get("task:subagent:lifecycle") || []) handler(event); },
			switchSession(next: string) { file = next; handlers.get("session_switch")?.({}, ctx as unknown as ExtensionContext); } };
	}
	function ref(id: string, generation: number, parentId: string, file: string) {
		const value: Ref = { id, generation, parentId, sessionFile: file, kind: "sub", displayName: "scout",
			status: "running", createdAt: 1, activity: "x".repeat(5000), session: { instanceKey: `${id}:${generation}:first` } };
		refs.set(id, value);
		for (const listener of listeners) listener({ type: "registered", ref: value });
		return value;
	}
	function settle() {
		for (let pass = 0; pass < 4; pass++) for (const [id, timer] of [...timers]) {
			if (!timers.has(id)) continue;
			if (!timer.repeat) timers.delete(id);
			timer.callback();
		}
	}
	function root(): Root { settle(); return JSON.parse(fs.readFileSync(path.join(directory, "root.json"), "utf8")) as Root; }
	const mainFile = `/tmp/bridge-smoke-${crypto.randomUUID()}.jsonl`;
	const artifact = mainFile.slice(0, -6);
	const main = factory("Main", "main", mainFile);
	let nested: FakeAgent | undefined;
	let streaming: FakeAgent | undefined;
	let unowned: FakeAgent | undefined;
	try {
		ref("Historical", 1, "Main", `${artifact}/Historical.jsonl`).status = "parked";
		main.emit("session_start");
		main.emit("agent_start");
		expect(root().agents).toEqual([]);
		main.emit("message_start", { message: { role: "user", content: "first", timestamp: 1 } });
		const firstTask = root().taskId;
		main.emit("tool_execution_start", { toolCallId: "call-first", toolName: "task", args: {} });
		main.emit("tool_execution_start", { toolCallId: "old-eval", toolName: "eval", args: {} });
		const current = ref("Current", 2, "Main", `${artifact}/Current.jsonl`);
		main.frame({ id: current.id, status: "started", parentToolCallId: "call-first", sessionFile: current.sessionFile });
		expect(root().agents.map(agent => agent.id)).toEqual(["Current"]);
		expect(root().agents[0].activity.length).toBe(5000);
		expect(root().agents[0].taskId).toBe(firstTask);
		expect(root().sessionFile).toBe(mainFile);
		expect(root().v).toBe(3);
		expect(root().ompExecutable.startsWith("/")).toBe(true);
		expect(root().agents[0].name).toBe("Current");
		const liveFile = path.join(directory, root().agents[0].liveFile);
		const live = (): Live => { settle(); return JSON.parse(fs.readFileSync(liveFile, "utf8")) as Live; };
		streaming = factory("Current", "sub", current.sessionFile);
		streaming.emit("session_start");
		const fullText = "完整原生正文".repeat(6000);
		const toolBlock = { type: "toolCall", id: "streamed-tool", name: "read", arguments: {} };
		Object.defineProperty(toolBlock, Symbol("provider.block.partialJson"), { value: '{"path":"incomplete' });
		const firstMessage = { role: "assistant", timestamp: 100, provider: "openai", api: "openai-responses", model: "fixture",
			content: [{ type: "thinking", thinking: "native reasoning", thinkingSignature: "opaque-signature" },
				{ type: "text", text: "initial" }, toolBlock] };
		streaming.emit("message_start", { message: firstMessage });
		const cumulative = { ...firstMessage, content: [firstMessage.content[0], { type: "text", text: fullText }, toolBlock] };
		streaming.emit("message_update", { message: cumulative, assistantMessageEvent: { type: "text_delta", delta: "irrelevant delta" } });
		expect(live().assistant?.message).toEqual(JSON.parse(JSON.stringify(cumulative)));
		expect(live().assistant?.partialArgs).toEqual([{ index: 2, json: '{"path":"incomplete' }]);
		expect(live().taskId).toBe(firstTask);
		expect(live().sourceSessionFile).toBe(mainFile);
		expect(live().sessionFile).toBe(current.sessionFile);
		expect(live().key).toBe("Current:2");
		expect(fs.statSync(liveFile).mode & 0o777).toBe(0o600);
		expect(JSON.stringify(root())).not.toContain(fullText);
		const firstStream = live().assistant?.streamId;
		const firstSeq = live().seq;
		const args = { command: fullText };
		const partial = { content: [{ type: "text", text: fullText }], details: { native: true } };
		const result = { content: [{ type: "text", text: `${fullText}final` }], details: { complete: [1, 2, 3] } };
		streaming.emit("tool_execution_start", { toolCallId: "native-tool", toolName: "bash", args });
		streaming.emit("tool_execution_update", { toolCallId: "native-tool", toolName: "bash", args, partialResult: partial });
		expect(live().tools[0]).toMatchObject({ args, partialResult: partial, ended: false });
		streaming.emit("tool_execution_end", { toolCallId: "native-tool", toolName: "bash", result, isError: false });
		// End is an immediate write, not a delta or a wait for the coalescing timer.
		expect((JSON.parse(fs.readFileSync(liveFile, "utf8")) as Live).tools[0]).toMatchObject({ result, ended: true, isError: false });
		streaming.emit("message_end", { message: cumulative });
		expect((JSON.parse(fs.readFileSync(liveFile, "utf8")) as Live).assistant?.ended).toBe(true);
		expect(live().seq).toBeGreaterThan(firstSeq);
		streaming.emit("tool_execution_start", { toolCallId: "pending-tool", toolName: "read", args: { path: "/tmp/pending" } });
		streaming.emit("message_start", { message: { ...firstMessage, timestamp: 101 } });
		expect(live().assistant?.streamId).not.toBe(firstStream);
		expect(live().tools.map(tool => tool.toolCallId)).toEqual(["pending-tool"]);
		main.emit("agent_end", { messages: [], awaitingAsyncWork: true });
		main.emit("agent_start");
		main.emit("message_start", { message: { role: "user", content: "auto", timestamp: 2, synthetic: true, attribution: "agent" } });
		expect(root().taskId).toBe(firstTask);
		current.status = "parked";
		current.session = null;
		for (const listener of listeners) listener({ type: "status_changed", ref: current });
		expect(root().agents[0].status).toBe("parked");

		current.session = { instanceKey: "Current:2:woken" };
		nested = factory("Current", "sub", current.sessionFile);
		nested.emit("session_start");
		nested.emit("tool_execution_start", { toolCallId: "nested-call", toolName: "task", args: {} });
		const grandchild = ref("Nested", 3, "Current", `${current.sessionFile.slice(0, -6)}/Nested.jsonl`);
		nested.frame({ id: grandchild.id, status: "started", parentToolCallId: "nested-call", sessionFile: grandchild.sessionFile });
		expect(root().agents.map(agent => agent.id).sort()).toEqual(["Current", "Nested"]);
		const resumedSeq = live().seq;
		streaming.emit("message_end", { message: cumulative });
		expect(live().seq).toBe(resumedSeq);

		main.emit("message_start", { message: { role: "user", content: "second", timestamp: 3 } });
		expect(root().taskId).not.toBe(firstTask);
		expect(root().agents).toEqual([]);
		main.frame({ id: current.id, status: "completed", parentToolCallId: "call-first", sessionFile: current.sessionFile });
		expect(root().agents).toEqual([]);
		main.emit("tool_call", { toolCallId: "old-eval:js-task-delayed", toolName: "task", input: {} });
		const delayed = ref("DelayedOldEval", 8, "Main", `${artifact}/DelayedOldEval.jsonl`);
		main.frame({ id: delayed.id, status: "started", parentToolCallId: "old-eval:js-task-delayed", sessionFile: delayed.sessionFile });
		expect(root().agents).toEqual([]);

		main.emit("tool_execution_start", { toolCallId: "outer-eval", toolName: "eval", args: {} });
		const speculative = ref("Speculative", 4, "Main", `${artifact}/Speculative.jsonl`);
		main.frame({ id: speculative.id, status: "started", parentToolCallId: "outer-eval:operation", sessionFile: speculative.sessionFile });
		main.emit("tool_execution_start", { toolCallId: "bare-agent-eval", toolName: "eval", args: {} });
		const bare = ref("BareAgent", 9, "Main", `${artifact}/BareAgent.jsonl`);
		main.frame({ id: bare.id, status: "started", parentToolCallId: "bare-agent-eval", sessionFile: bare.sessionFile });
		expect(root().agents.map(agent => agent.id).sort()).toEqual(["BareAgent", "Speculative"]);
		refs.delete(bare.id);
		for (const listener of listeners) listener({ type: "removed", ref: bare });
		const missing = ref("MissingParent", 5, "Main", `${artifact}/MissingParent.jsonl`);
		main.frame({ id: missing.id, status: "started", sessionFile: missing.sessionFile });
		main.emit("tool_call", { toolCallId: "js-task-unrelated", toolName: "task", input: {} });
		const unbound = ref("UnboundEval", 6, "Main", `${artifact}/UnboundEval.jsonl`);
		main.frame({ id: unbound.id, status: "started", parentToolCallId: "js-task-unrelated", sessionFile: unbound.sessionFile });
		expect(root().agents.map(agent => agent.id)).toEqual(["Speculative"]);
		expect(root().diagnostics.map(item => item.agentId).sort()).toEqual(["MissingParent", "UnboundEval"]);
		unowned = factory(missing.id, "sub", missing.sessionFile);
		unowned.emit("session_start");
		unowned.emit("message_start", { message: { role: "assistant", timestamp: 102, content: [{ type: "text", text: "unconfirmed" }] } });
		settle();
		const missingDigest = crypto.createHash("sha256").update(`${missing.id}:${missing.generation}`).digest("hex").slice(0, 24);
		expect(fs.existsSync(path.join(directory, `live-${missingDigest}.json`))).toBe(false);
		main.frame({ id: missing.id, status: "started", parentToolCallId: "outer-eval", sessionFile: missing.sessionFile });
		settle();
		const confirmed = JSON.parse(fs.readFileSync(path.join(directory, `live-${missingDigest}.json`), "utf8")) as Live;
		expect(confirmed.taskId).toBe(root().taskId);
		expect(confirmed.assistant?.message.content).toEqual([{ type: "text", text: "unconfirmed" }]);
		refs.delete(missing.id);
		for (const listener of listeners) listener({ type: "removed", ref: missing });

		main.emit("tool_execution_start", { toolCallId: "wake-call", toolName: "write", args: { path: "agent://Current" } });
		main.emit("tool_result", { toolCallId: "wake-call", toolName: "write", input: { path: "agent://Current" }, isError: false });
		expect(root().agents.map(agent => agent.id).sort()).toEqual(["Current", "Speculative"]);
		refs.delete("Current");
		for (const listener of listeners) listener({ type: "removed", ref: current });
		expect(root().agents.map(agent => agent.id)).toEqual(["Speculative"]);
		ref("Current", 7, "Main", `${artifact}/Replacement.jsonl`);
		expect(root().agents.map(agent => agent.id)).toEqual(["Speculative"]);
		const beforeSwitch = root().taskId;
		main.switchSession(`/tmp/bridge-switched-${crypto.randomUUID()}.jsonl`);
		expect(root().taskId).not.toBe(beforeSwitch);
		expect(root().agents).toEqual([]);
		expect(fs.readdirSync(directory).every(name => name === "root.json" || /^source-.*\.json$/.test(name) ||
			/^live-[0-9a-f]{24}\.(?:json|owner\.json|lock)$/.test(name))).toBe(true);
		expect(warnings).toEqual([]);
	} finally {
		nested?.emit("session_shutdown");
		streaming?.emit("session_shutdown");
		unowned?.emit("session_shutdown");
		main.emit("session_shutdown");
		fs.rmSync(scan, { recursive: true, force: true });
		for (const [name, value] of Object.entries(saved)) {
			if (value === undefined) delete process.env[name]; else process.env[name] = value;
		}
	}
});

test("native mount rebinds preloaded observers without sharing shutdown, buses or switched-session context", async () => {
	const saved = {
		REX_SESSION: process.env.REX_SESSION,
		REX_BLOCK: process.env.REX_BLOCK,
		REX_SUBAGENT_VIEWER: process.env.REX_SUBAGENT_VIEWER,
		REX_WORKSPACE_SCAN_DIR: process.env.REX_WORKSPACE_SCAN_DIR,
		REX_WORKSPACE_OMP: process.env.REX_WORKSPACE_OMP,
		REX_WORKSPACE_WIDTH: process.env.REX_WORKSPACE_WIDTH,
	};
	const scan = fs.mkdtempSync("/tmp/rex-bridge-binding-");
	process.env.REX_SESSION = "binding";
	process.env.REX_BLOCK = crypto.randomUUID();
	process.env.REX_WORKSPACE_SCAN_DIR = scan;
	process.env.REX_WORKSPACE_WIDTH = "25";
	delete process.env.REX_SUBAGENT_VIEWER;
	delete process.env.REX_WORKSPACE_OMP;
	const callbacks = new Map<number, () => void>();
	let timerId = 0;
	function context(id: string, file: string): ExtensionContext {
		return {
			agent: { id, kind: "sub" },
			cwd: "/tmp",
			// Keep sessionId fixed: a rebind must also inspect the transcript path.
			sessionManager: {
				getSessionId: () => "stable-session",
				getSessionFile: () => file,
				getArtifactsDir: () => file.slice(0, -6),
			},
			setTimeout(callback: () => void) {
				const id = ++timerId;
				callbacks.set(id, callback);
				return id;
			},
			clearTimer(id: number) { callbacks.delete(id); },
		} as unknown as ExtensionContext;
	}
	async function emit(extension: Extension, name: string, ctx: ExtensionContext, event: Record<string, unknown> = {}) {
		for (const handler of extension.handlers.get(name) ?? []) await handler(event, ctx);
	}
	function settle() {
		for (const [id, callback] of [...callbacks]) {
			callbacks.delete(id);
			callback();
		}
	}
	try {
		const parentBus = new EventBus();
		const childBus = new EventBus();
		const loaded = await loadExtensions([], "/tmp", parentBus);
		const parent = await mountRexBridge(loaded, "/tmp", scan, parentBus);
		// A preloaded parent-bound observer is replaced, never reused.
		const child = await mountRexBridge(parent, "/tmp/child", scan, childBus);
		const prepared = await bindPreparedExtensions([
			{ path: REX_BRIDGE_SOURCE, resolvedPath: REX_BRIDGE_SOURCE, factory: rexWorkspaceBridge, error: null },
			{ path: REX_BRIDGE_SOURCE, resolvedPath: REX_BRIDGE_SOURCE, factory: rexWorkspaceBridge, error: null },
		], "/tmp", childBus);
		const rebound = await mountRexBridge(prepared, "/tmp", scan, childBus);
		expect(rebound.extensions.flatMap(extension => [...extension.commands.keys()])).toEqual(["rex-subagents"]);
		const parentExtension = parent.extensions[0];
		const childExtension = child.extensions[0];
		const parentContext = context("binding-parent", "/tmp/parent.jsonl");
		let childContext = context("binding-child", "/tmp/child.jsonl");
		await emit(parentExtension, "session_start", parentContext);
		await emit(childExtension, "session_start", childContext);
		settle();
		expect(parentBus.hasListeners("task:subagent:lifecycle")).toBe(true);
		expect(childBus.hasListeners("task:subagent:lifecycle")).toBe(true);
		const stateDir = path.join(scan, fs.readdirSync(scan)[0]);
		const evidence = () => fs.readdirSync(stateDir)
			.filter(name => name.startsWith("source-"))
			.map(name => JSON.parse(fs.readFileSync(path.join(stateDir, name), "utf8")) as {
				source: { id: string; sessionFile: string };
			});
		// Session switches with a stable ID revoke the old subscription and context.
		childContext = context("binding-child", "/tmp/switched-child.jsonl");
		await emit(childExtension, "session_switch", childContext);
		await emit(childExtension, "session_shutdown", childContext);
		expect(childBus.hasListeners("task:subagent:lifecycle")).toBe(false);
		expect(parentBus.hasListeners("task:subagent:lifecycle")).toBe(true);
		expect(evidence().filter(source => source.source.id === "binding-child")
			.map(source => source.source.sessionFile).sort()).toEqual(["/tmp/child.jsonl", "/tmp/switched-child.jsonl"]);
		await emit(parentExtension, "session_shutdown", parentContext);
		expect(parentBus.hasListeners("task:subagent:lifecycle")).toBe(false);
	} finally {
		fs.rmSync(scan, { recursive: true, force: true });
		for (const [name, value] of Object.entries(saved)) {
			if (value === undefined) delete process.env[name];
			else process.env[name] = value;
		}
	}
});

test("disabled or non-Rex mounts create no native commands, bus subscriptions or workspace files", async () => {
	const saved = {
		REX_SESSION: process.env.REX_SESSION,
		REX_BLOCK: process.env.REX_BLOCK,
		REX_SUBAGENT_VIEWER: process.env.REX_SUBAGENT_VIEWER,
		REX_WORKSPACE_SCAN_DIR: process.env.REX_WORKSPACE_SCAN_DIR,
	};
	const scan = fs.mkdtempSync("/tmp/rex-bridge-disabled-");
	process.env.REX_WORKSPACE_SCAN_DIR = scan;
	try {
		for (const disabled of [false, true]) {
			if (disabled) {
				process.env.REX_SESSION = "disabled";
				process.env.REX_BLOCK = "disabled";
				process.env.REX_SUBAGENT_VIEWER = "0";
			} else {
				delete process.env.REX_SESSION;
				delete process.env.REX_BLOCK;
				delete process.env.REX_SUBAGENT_VIEWER;
			}
			const bus = new EventBus();
			const loaded = await loadExtensions([], "/tmp", bus);
			const mounted = await mountRexBridge(loaded, "/tmp", scan, bus);
			expect(mounted.extensions.flatMap(extension => [...extension.commands.keys()])).toEqual([]);
			expect(bus.hasListeners("task:subagent:lifecycle")).toBe(false);
			expect(fs.readdirSync(scan)).toEqual([]);
		}
	} finally {
		fs.rmSync(scan, { recursive: true, force: true });
		for (const [name, value] of Object.entries(saved)) {
			if (value === undefined) delete process.env[name];
			else process.env[name] = value;
		}
	}
});

test("createAgentSession keeps the native command when external extension discovery is disabled", async () => {
	const saved = {
		REX_SESSION: process.env.REX_SESSION,
		REX_BLOCK: process.env.REX_BLOCK,
		REX_SUBAGENT_VIEWER: process.env.REX_SUBAGENT_VIEWER,
		REX_WORKSPACE_SCAN_DIR: process.env.REX_WORKSPACE_SCAN_DIR,
	};
	const scan = fs.mkdtempSync("/tmp/rex-sdk-native-");
	const authStorage = createInMemoryAuthStorage();
	process.env.REX_SESSION = "sdk-native";
	process.env.REX_BLOCK = crypto.randomUUID();
	process.env.REX_WORKSPACE_SCAN_DIR = scan;
	delete process.env.REX_SUBAGENT_VIEWER;
	try {
		const created = await createAgentSession({
			cwd: scan,
			agentDir: scan,
			sessionManager: SessionManager.inMemory(),
			modelRegistry: new ModelRegistry(authStorage, path.join(scan, "models.yml")),
			settings: Settings.isolated(),
			disableExtensionDiscovery: true,
			enableLsp: false,
			enableMCP: false,
			skipPythonPreflight: true,
			skills: [],
			rules: [],
			preloadedCustomToolPaths: [],
			contextFiles: [],
			promptTemplates: [],
			slashCommands: [],
			toolNames: ["read"],
		});
		try {
			// This is the executable slash-command registry exposed to the session,
			// not a separate list of native-source declarations.
			expect(created.extensionsResult.extensions
				.flatMap(extension => [...extension.commands.keys()])
				.filter(command => command === "rex-subagents")).toEqual(["rex-subagents"]);
		} finally {
			await created.session.dispose();
		}
	} finally {
		authStorage.close();
		fs.rmSync(scan, { recursive: true, force: true });
		for (const [name, value] of Object.entries(saved)) {
			if (value === undefined) delete process.env[name];
			else process.env[name] = value;
		}
	}
});

test("retired Rex modules and prepared factories never execute, while ordinary extensions still bind and run", async () => {
	const directory = fs.mkdtempSync("/tmp/rex-retired-extension-");
	const retiredDirectory = path.join(directory, "rex-subagent-viewer");
	fs.mkdirSync(retiredDirectory);
	const retiredPath = path.join(retiredDirectory, "index.ts");
	const controlPath = path.join(directory, "ordinary.ts");
	const disabled = process.env.REX_SUBAGENT_VIEWER;
	process.env.REX_SUBAGENT_VIEWER = "0";
	let retiredCalls = 0;
	let ordinaryCalls = 0;
	try {
		await Bun.write(retiredPath, 'throw new Error("retired module was evaluated"); export default () => {};');
		await Bun.write(controlPath, 'export default api => { api.events.emit("ordinary-extension", {}); };');
		const bus = new EventBus();
		bus.on("ordinary-extension", () => { ordinaryCalls++; });
		const loaded = await loadExtensions([retiredDirectory, retiredPath, controlPath], directory, bus);
		expect(loaded.errors).toEqual([]);
		expect(ordinaryCalls).toBe(1);
		const direct = await importExtensionModule(retiredPath, directory);
		expect(direct.error).toBeNull();
		const retiredFactory = () => { retiredCalls++; };
		const prepared = await bindPreparedExtensions([
			{ path: retiredDirectory, resolvedPath: retiredPath, factory: retiredFactory, error: null },
			{ path: "<aliased-old-rex>", resolvedPath: retiredPath, factory: retiredFactory, error: null },
			{
				path: "<ordinary-prepared>",
				resolvedPath: "<ordinary-prepared>",
				factory: api => { api.events.emit("ordinary-extension", {}); },
				error: null,
			},
		], directory, bus);
		await loadExtensionFromFactory(retiredFactory, directory, bus, prepared.runtime, retiredPath);
		expect(retiredCalls).toBe(0);
		expect(ordinaryCalls).toBe(2);

		// Simulate a caller handing over an already-bound old observer. Its handlers
		// must be excluded rather than receiving the new session's native events.
		const stale = await loadExtensionFromFactory(api => {
			api.on("session_start", () => { retiredCalls++; });
		}, directory, bus, prepared.runtime, "<stale-bound>");
		stale.path = retiredPath;
		stale.resolvedPath = retiredPath;
		const ordinary = await loadExtensionFromFactory(api => {
			api.on("session_start", () => { ordinaryCalls++; });
		}, directory, bus, prepared.runtime, "<ordinary-bound>");
		const mounted = await mountRexBridge({
			...prepared,
			extensions: [stale, ordinary],
			preparedExtensions: [
				{ path: retiredPath, resolvedPath: retiredPath, factory: retiredFactory, error: null },
			],
		}, directory, directory, bus);
		for (const extension of mounted.extensions) {
			for (const handler of extension.handlers.get("session_start") ?? []) await handler({}, {});
		}
		expect(retiredCalls).toBe(0);
		expect(ordinaryCalls).toBe(3);
		expect(mounted.preparedExtensions).toEqual([]);
	} finally {
		fs.rmSync(directory, { recursive: true, force: true });
		if (disabled === undefined) delete process.env.REX_SUBAGENT_VIEWER;
		else process.env.REX_SUBAGENT_VIEWER = disabled;
	}
});
