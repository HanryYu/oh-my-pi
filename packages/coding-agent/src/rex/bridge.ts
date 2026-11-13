import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import type { AgentMessage } from "@oh-my-pi/pi-agent-core";
import { getAgentDir, tryAcquireFileLock } from "@oh-my-pi/pi-utils";
import type { ExtensionAPI, ExtensionContext } from "../extensibility/extensions/types";
import type { AgentRef, AgentRegistryApi } from "../registry/agent-registry";

type NativeAssistant = Extract<AgentMessage, { role: "assistant" }>;
type LiveTool = { toolCallId: string; toolName: string; args: unknown; partialResult?: unknown;
	result?: unknown; isError?: boolean; ended: boolean };
type LiveAssistant = { streamId: string; ended: boolean; message: NativeAssistant;
	partialArgs?: Array<{ index: number; json: string }> };
type LiveSnapshot = { v: 1; key: string; taskId: string; sourceSessionFile: string; sessionFile: string;
	seq: number; assistant?: LiveAssistant; tools: LiveTool[] };
type LiveRoot = { taskId?: string; sessionFile?: string; closed?: boolean;
	agents?: Array<{ key: string; sessionFile?: string; taskId?: string }> };

type State = "idle" | "working" | "done" | "blocked" | "error";
type Identity = { id: string; key: string; kind: string; sessionId: string; sessionFile?: string; artifactsDir?: string };
type Child = { key: string; id: string; sessionFile?: string; parentToolCallId?: string; taskId?: string; observedTaskId?: string };
type Source = { v: 1; source: Identity; tools: Record<string, string>; children: Child[] };

function digest(value: string): string {
	return crypto.createHash("sha256").update(value).digest("hex").slice(0, 24);
}
function canonical(value: string | null | undefined): string | undefined {
	if (!value || !path.isAbsolute(value)) return undefined;
	try { return fs.realpathSync(value); }
	catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
		// SessionManager allocates the real transcript path before materializing it.
		return path.resolve(value);
	}
}
function key(ref: AgentRef): string { return `${ref.id}:${ref.generation}`; }
function sidebarWidth(): number | undefined {
	const raw = process.env.REX_WORKSPACE_WIDTH || "25";
	const width = /^\d+$/.test(raw) ? Number(raw) : NaN;
	return Number.isInteger(width) && width >= 15 && width <= 35 ? width : undefined;
}
function ompExecutable(): string {
	const configured = process.env.REX_WORKSPACE_OMP;
	if (configured && !path.isAbsolute(configured)) throw new Error("REX_WORKSPACE_OMP 必须为绝对可执行文件路径");
	const executable = canonical(configured || process.execPath);
	if (!executable || !fs.lstatSync(executable).isFile()) throw new Error("OMP 可执行文件不可用");
	fs.accessSync(executable, fs.constants.X_OK);
	return executable;
}

// Bun workers share ownership metadata and one replaceable native snapshot per child, never a log.
function directory(): string {
	const sourceDirectory = process.env.REX_WORKSPACE_SCAN_DIR || "/tmp";
	if (!path.isAbsolute(sourceDirectory)) throw new Error("REX_WORKSPACE_SCAN_DIR 必须是绝对路径");
	return path.join(sourceDirectory, `rsv-${process.getuid?.()}-${process.pid}-${digest(process.env.REX_BLOCK || "").slice(0, 10)}`);
}
function privateDirectory(): string {
	const target = directory();
	try { fs.mkdirSync(target, { mode: 0o700 }); }
	catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error; }
	const info = fs.lstatSync(target);
	if (!info.isDirectory() || info.uid !== process.getuid?.() || (info.mode & 0o077)) {
		throw new Error("工作区目录必须为当前用户拥有的 0700 普通目录");
	}
	return target;
}
function publish(name: string, data: unknown): void {
	const target = path.join(privateDirectory(), name);
	const temporary = `${target}.${crypto.randomUUID()}.tmp`;
	fs.writeFileSync(temporary, JSON.stringify(data), { mode: 0o600, flag: "wx" });
	fs.renameSync(temporary, target);
}
function readPrivate<T>(name: string): T | undefined {
	let fd: number | undefined;
	try {
		fd = fs.openSync(path.join(privateDirectory(), name), fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
		const info = fs.fstatSync(fd);
		if (!info.isFile() || info.uid !== process.getuid?.() || (info.mode & 0o077)) return undefined;
		return JSON.parse(fs.readFileSync(fd, "utf8")) as T;
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
		throw error;
	} finally { if (fd !== undefined) fs.closeSync(fd); }
}
function userTask(message: AgentMessage): boolean {
	return message.role === "user" ? !message.synthetic && message.attribution !== "agent" :
		message.role === "custom" && message.customType === "skill-prompt" && message.attribution === "user";
}

/** One factory/session's native event evidence; never a transcript or tool-output mirror. */
class Evidence {
	readonly file = `source-${crypto.randomUUID()}.json`;
	readonly data: Source;
	#children = new Map<string, Child>();
	#subscriptions: Array<() => void> = [];
	#timer?: Timer;

	constructor(private pi: ExtensionAPI, private ctx: ExtensionContext,
							private task: () => string | undefined, private changed: () => void) {
		const ref = pi.pi.AgentRegistry.global().get(ctx.agent.id);
		this.data = {
			v: 1,
			source: { id: ctx.agent.id, key: ref ? key(ref) : `${ctx.agent.id}:${ctx.sessionManager.getSessionId()}`,
				kind: ctx.agent.kind, sessionId: ctx.sessionManager.getSessionId(),
				sessionFile: canonical(ctx.sessionManager.getSessionFile()),
				artifactsDir: canonical(ctx.sessionManager.getArtifactsDir()) },
			tools: {}, children: [],
		};
		// pi.events is the owning session's bus. In workers RemoteEventBus forwards these exact
		// subscriptions to its main-thread bus; no global-registry/Main-chain inference is needed.
		this.#subscriptions.push(
			pi.events.on("task:subagent:lifecycle", value => this.#frame(value, true)),
			pi.events.on("task:subagent:progress", value => this.#frame(value, false)),
		);
		this.flush();
	}

	#child(ref: AgentRef): Child {
		const identity = key(ref);
		let child = this.#children.get(identity);
		if (!child) {
			child = { key: identity, id: ref.id, sessionFile: canonical(ref.sessionFile) };
			this.#children.set(identity, child);
		}
		return child;
	}

	#frame(value: unknown, lifecycle: boolean): void {
		if (!value || typeof value !== "object") return;
		const payload = value as { id?: string; status?: string; sessionFile?: string; parentToolCallId?: string; progress?: { id?: string } };
		const id = lifecycle ? payload.id : payload.progress?.id;
		if (!id) return;
		const ref = this.pi.pi.AgentRegistry.global().get(id);
		if (!ref || ref.kind !== "sub" || ref.parentId !== this.data.source.id) return;
		const file = canonical(payload.sessionFile);
		const actualFile = canonical(ref.sessionFile);
		if (file && actualFile && file !== actualFile) return;
		if (file && path.dirname(file) !== this.data.source.artifactsDir) return;
		const child = this.#child(ref);
		child.sessionFile = actualFile || file || child.sessionFile;
		if (payload.parentToolCallId) child.parentToolCallId = payload.parentToolCallId;
		child.observedTaskId ??= readPrivate<{ taskId?: string }>("root.json")?.taskId;
		this.#schedule();
	}

	tool(id: string, dispatched = false): void {
		// Only loop-dispatched IDs or IDs scoped to a captured outer call are authoritative.
		if (!dispatched && /^js-/.test(id)) return;
		let taskId: string | undefined;
		for (const [outer, owner] of Object.entries(this.data.tools)) {
			if (id.startsWith(`${outer}:`)) { taskId = owner; break; }
		}
		// A not-yet-admitted child may publish its first nested tool immediately. This
		// candidate token is accepted only after the main bridge proves the exact source edge.
		taskId ??= this.task() || (this.data.source.kind === "sub" ?
			readPrivate<{ taskId?: string }>("root.json")?.taskId : undefined);
		if (taskId) this.data.tools[id] ??= taskId;
		this.#schedule();
	}

	/** Explicit agent:// writes can intentionally resume a prior parked generation. */
	address(id: string, input: unknown): void {
		if (!input || typeof input !== "object" || !("path" in input) || typeof input.path !== "string") return;
		const match = /^agent:\/\/([^/]+)$/.exec(input.path);
		if (!match) return;
		const ref = this.pi.pi.AgentRegistry.global().get(match[1]);
		const taskId = this.data.tools[id];
		const file = canonical(ref?.sessionFile);
		if (!ref || ref.kind !== "sub" || ref.parentId !== this.data.source.id || !taskId || !file ||
				path.dirname(file) !== this.data.source.artifactsDir) return;
		const child = this.#child(ref);
		child.taskId = taskId;
		this.#schedule();
	}

	#schedule(): void {
		this.#timer ??= this.ctx.setTimeout(() => { this.#timer = undefined; this.flush(); }, 30);
	}
	flush(): void {
		const ref = this.pi.pi.AgentRegistry.global().get(this.ctx.agent.id);
		if (ref && canonical(ref.sessionFile) === canonical(this.ctx.sessionManager.getSessionFile())) {
			this.data.source.key = key(ref);
		}
		this.data.source.sessionFile = canonical(this.ctx.sessionManager.getSessionFile());
		this.data.source.artifactsDir = canonical(this.ctx.sessionManager.getArtifactsDir());
		this.data.children = [...this.#children.values()];
		publish(this.file, this.data);
		this.changed();
	}
	finish(): void {
		for (const unsubscribe of this.#subscriptions) unsubscribe();
		this.#subscriptions = [];
		if (this.#timer !== undefined) this.ctx.clearTimer(this.#timer);
		this.#timer = undefined;
		this.flush();
	}
}

/** Latest complete native state only: persisted JSONL remains the transcript authority. */
class NativeLiveState {
	#identity?: string;
	#sessionFile?: string;
	#instanceKey?: string;
	#owner = crypto.randomUUID();
	#claimed = false;
	#revoked = false;
	#assistant?: LiveAssistant;
	#tools = new Map<string, LiveTool>();
	#timer?: Timer;
	#dirty = false;

	constructor(private pi: ExtensionAPI, private ctx: ExtensionContext, private warn: (message: string) => void) {}

	#bind(): boolean {
		if (this.#revoked) return false;
		const ref = this.pi.pi.AgentRegistry.global().get(this.ctx.agent.id);
		const file = canonical(this.ctx.sessionManager.getSessionFile());
		if (!ref || ref.kind !== "sub" || !file || canonical(ref.sessionFile) !== file) return false;
		if (this.#identity && (this.#identity !== key(ref) || this.#sessionFile !== file ||
				this.#instanceKey && ref.session && this.#instanceKey !== ref.session.instanceKey)) {
			this.#revoked = true;
			return false;
		}
		this.#instanceKey ??= ref.session?.instanceKey;
		if (!this.#identity) {
			this.#identity = key(ref);
			this.#sessionFile = file;
			const previous = readPrivate<LiveSnapshot>(`live-${digest(this.#identity)}.json`);
			if (previous?.v === 1 && previous.key === this.#identity && previous.sessionFile === file) {
				this.#assistant = previous.assistant;
				for (const tool of previous.tools) this.#tools.set(tool.toolCallId, tool);
			}
		}
		return true;
	}

	message(message: AgentMessage, phase: "start" | "update" | "end"): void {
		if (message.role !== "assistant" || !this.#bind()) return;
		if (phase === "start") {
			this.#assistant = { streamId: crypto.randomUUID(), ended: false, message };
			for (const [id, tool] of this.#tools) if (tool.ended) this.#tools.delete(id);
		} else {
			this.#assistant = { streamId: this.#assistant?.streamId || crypto.randomUUID(), ended: phase === "end", message };
		}
		const partialArgs: Array<{ index: number; json: string }> = [];
		for (let index = 0; index < message.content.length; index++) {
			const block = message.content[index];
			if (block.type !== "toolCall") continue;
			for (const symbol of Object.getOwnPropertySymbols(block)) {
				if (symbol.description !== "provider.block.partialJson") continue;
				const json: unknown = Reflect.get(block, symbol);
				if (typeof json === "string") partialArgs.push({ index, json });
			}
		}
		if (partialArgs.length) this.#assistant.partialArgs = partialArgs;
		this.#changed(phase === "end");
	}

	toolStart(toolCallId: string, toolName: string, args: unknown): void {
		if (!this.#bind()) return;
		this.#tools.set(toolCallId, { toolCallId, toolName, args, ended: false });
		this.#changed(false);
	}
	toolUpdate(toolCallId: string, toolName: string, args: unknown, partialResult: unknown): void {
		if (!this.#bind()) return;
		const tool = this.#tools.get(toolCallId) || { toolCallId, toolName, args, ended: false };
		tool.partialResult = partialResult;
		this.#tools.set(toolCallId, tool);
		this.#changed(false);
	}
	toolEnd(toolCallId: string, toolName: string, result: unknown, isError: boolean): void {
		if (!this.#bind()) return;
		const tool = this.#tools.get(toolCallId) || { toolCallId, toolName, args: undefined, ended: false };
		tool.result = result;
		tool.isError = isError;
		tool.ended = true;
		this.#tools.set(toolCallId, tool);
		this.#changed(true);
	}
	#changed(immediate: boolean): void {
		this.#dirty = true;
		if (immediate) this.#flush();
		if (!this.#revoked && this.#dirty) this.#timer ??= this.ctx.setTimeout(() => {
			this.#timer = undefined;
			this.#flush();
			if (this.#dirty && !this.#revoked) this.#changed(false);
		}, 50);
	}
	#flush(): void {
		if (!this.#dirty || !this.#bind() || !this.#identity || !this.#sessionFile) return;
		let lock: { release(): void } | null = null;
		try {
			const root = readPrivate<LiveRoot>("root.json");
			const member = root?.agents?.find(agent => agent.key === this.#identity && agent.sessionFile === this.#sessionFile);
			const sourceSessionFile = canonical(root?.sessionFile);
			if (root?.closed || !root?.taskId || member?.taskId !== root.taskId || !sourceSessionFile) return;
			const basename = `live-${digest(this.#identity)}`;
			lock = tryAcquireFileLock(path.join(privateDirectory(), basename));
			if (!lock) return;
			// A new factory can revoke an old parked factory only while holding the same OS lock.
			// The old owner cannot race a late shutdown flush over the new snapshot.
			const leaseName = `${basename}.owner.json`;
			const lease = readPrivate<{ key: string; sessionFile: string; owner: string }>(leaseName);
			if (this.#claimed && lease?.owner !== this.#owner) { this.#revoked = true; return; }
			if (!this.#claimed) {
				publish(leaseName, { key: this.#identity, sessionFile: this.#sessionFile, owner: this.#owner });
				this.#claimed = true;
			}
			const previous = readPrivate<LiveSnapshot>(`${basename}.json`);
			const seq = previous?.key === this.#identity && Number.isSafeInteger(previous.seq) ? previous.seq + 1 : 1;
			publish(`${basename}.json`, { v: 1, key: this.#identity, taskId: root.taskId, sourceSessionFile,
				sessionFile: this.#sessionFile, seq, assistant: this.#assistant, tools: [...this.#tools.values()] } satisfies LiveSnapshot);
			this.#dirty = false;
		} catch (error) {
			this.#revoked = true;
			this.warn(`原生实时状态写入失败：${String(error)}；原任务未受影响`);
		}
		finally { lock?.release(); }
	}
	finish(): void {
		this.#flush();
		if (this.#timer !== undefined) this.ctx.clearTimer(this.#timer);
		this.#timer = undefined;
	}
}

type MainState = { state: State; intent: string; cwd: string; title?: string; startedAt: number };
class Workspace {
	#registry: Pick<AgentRegistryApi, "onChange" | "get">;
	#unsubscribe: () => void;
	#timer: Timer;
	#display: { action: "show" | "hide"; seq: number; width: number };
	#closed = false;
	#tools = new Map<string, string>();
	#approvals = new Map<string, string>();
	#seenMessages = new WeakSet<object>();
	#members = new Map<string, Child>();
	#main: MainState;
	readonly sessionId: string;
	taskId = crypto.randomUUID();
	#accepting = false;
	#snapshot = "";
	#executable = ompExecutable();
	#diagnostics: Array<{ agentId: string; code: string; message: string }> = [];

	constructor(private ctx: ExtensionContext, pi: ExtensionAPI, private warn: (message: string) => void, width: number, private agentDir: string) {
		this.sessionId = ctx.sessionManager.getSessionId();
		this.#display = { action: "hide", seq: 0, width };
		this.#registry = pi.pi.AgentRegistry.global();
		this.#main = { state: "idle", intent: "", cwd: ctx.cwd, startedAt: Date.now() };
		this.#unsubscribe = this.#registry.onChange(event => {
			if (event.type === "removed") this.#members.delete(key(event.ref));
			this.flush();
		});
		// Atomic metadata documents are the cross-worker rendezvous, including parked/woken
		// factories. Polling does not read session content, restart a renderer, or call a model.
		this.#timer = ctx.setInterval(() => this.flush(), 150);
		this.flush();
	}

	currentTask(): string | undefined { return this.#accepting ? this.taskId : undefined; }
	input(message: AgentMessage): void {
		if (!userTask(message) || this.#seenMessages.has(message)) return;
		this.#seenMessages.add(message);
		this.taskId = crypto.randomUUID();
		this.#accepting = true;
		this.#members.clear();
		this.#tools.clear();
		this.#approvals.clear();
		this.#main = { state: "working", intent: "", cwd: this.ctx.cwd, startedAt: Date.now() };
		this.show();
	}

	#agents() {
		const sessionFile = canonical(this.ctx.sessionManager.getSessionFile());
		if (!sessionFile || !this.#accepting) return [];
		const sources: Source[] = [];
		this.#diagnostics = [];
		for (const name of fs.readdirSync(privateDirectory())) {
			if (!/^source-[0-9a-f-]+\.json$/.test(name)) continue;
			const source = readPrivate<Source>(name);
			if (source?.v === 1) sources.push(source);
		}
		// Resolve child evidence transitively. Each edge must come from the exact owning
		// session+generation; a historical ref merely sharing Main's parent chain is insufficient.
		let added: boolean;
		do {
			added = false;
			for (const { source, tools, children } of sources) {
				const isMain = source.kind === "main" && source.id === this.ctx.agent.id &&
					source.sessionId === this.sessionId && source.sessionFile === sessionFile;
				const parent = this.#members.get(source.key);
				if (!isMain && (!parent || parent.taskId !== this.taskId || !parent.sessionFile ||
						parent.sessionFile !== source.sessionFile)) continue;
				for (const child of children) {
					const ref = this.#registry.get(child.id);
					if (!ref || key(ref) !== child.key || ref.kind !== "sub" || ref.parentId !== source.id) continue;
					const file = canonical(ref.sessionFile) || child.sessionFile;
					if (child.sessionFile && file !== child.sessionFile) continue;
					if (file && path.dirname(file) !== source.artifactsDir) continue;
					let owner = child.taskId;
					if (!owner && child.parentToolCallId) {
						owner = tools[child.parentToolCallId];
						// Speculative eval uses `${outerToolCallId}:${operation.call.id}` natively.
						// Match a captured full outer ID, never a timestamp or agent name.
						if (!owner) {
							for (const [outer, taskId] of Object.entries(tools)) {
								if (child.parentToolCallId.startsWith(`${outer}:`)) { owner = taskId; break; }
							}
						}
					}
					if (!owner && child.observedTaskId === this.taskId && !this.#diagnostics.some(item => item.agentId === child.id)) {
						this.#diagnostics.push({ agentId: child.id, code: "native_parent_request_unavailable",
							message: "原生任务事件未提供可核实的父请求标识；未将此代理归入当前任务。" });
					}
					if (owner !== this.taskId) continue;
					const existing = this.#members.get(child.key);
					if (!existing) added = true;
					this.#members.set(child.key, { ...child, sessionFile: file, taskId: owner });
				}
			}
		} while (added);
		const result = [];
		for (const [identity, member] of this.#members) {
			const ref = this.#registry.get(member.id);
			if (!ref || key(ref) !== identity) { this.#members.delete(identity); continue; }
			const currentFile = canonical(ref.sessionFile);
			if (currentFile && member.sessionFile && currentFile !== member.sessionFile) {
				this.#members.delete(identity);
				continue;
			}
			result.push({ key: identity, id: ref.id, generation: ref.generation,
				name: ref.id || ref.displayName, parentId: ref.parentId, status: ref.status,
				activity: ref.activity || "", createdAt: ref.createdAt, model: ref.history?.resolvedModel || "",
				sessionFile: currentFile || member.sessionFile,
				parentToolCallId: member.parentToolCallId, taskId: this.taskId,
				liveFile: `live-${digest(identity)}.json` });
		}
		return result;
	}

	flush(): void {
		try {
			this.#main.title = this.ctx.sessionManager.getSessionName();
			const agents = this.#agents();
			const snapshot = JSON.stringify({ v: 3, pid: process.pid, session: process.env.REX_SESSION,
				anchor: process.env.REX_BLOCK, sessionId: this.sessionId,
				sessionFile: canonical(this.ctx.sessionManager.getSessionFile()),
				ompExecutable: this.#executable,
				agentDir: canonical(this.agentDir),
				commandPath: (process.env.PATH || "").split(":").filter(part => path.isAbsolute(part)).join(":"),
				taskId: this.taskId, main: this.#main, agents, diagnostics: this.#diagnostics,
				closed: this.#closed, display: this.#display });
			if (snapshot === this.#snapshot) return;
			publish("root.json", JSON.parse(snapshot));
			this.#snapshot = snapshot;
		} catch (error) { this.warn(`工作区状态写入失败：${String(error)}`); }
	}
	show(): void {
		if (this.#closed) return;
		this.#display = { ...this.#display, action: "show", seq: this.#display.seq + 1 };
		this.flush();
	}
	hide(): void {
		if (this.#closed) return;
		this.#display = { ...this.#display, action: "hide", seq: this.#display.seq + 1 };
		this.flush();
	}
	start(ctx: ExtensionContext): void {
		this.ctx = ctx;
		this.#main.state = "working";
		this.#main.cwd = ctx.cwd;
		// agent_start also fires for async result delivery/retry/continue. Never rotate taskId here.
		this.flush();
	}
	toolStart(id: string, name: string, input: unknown, intent?: string): void {
		const label = intent || (input && typeof input === "object" && "i" in input && typeof input.i === "string" ? input.i : name);
		this.#tools.set(id, label);
		this.#main.intent = label;
		this.flush();
	}
	toolEnd(id: string): void {
		this.#tools.delete(id);
		this.#main.intent = [...this.#tools.values()].at(-1) || "";
		this.flush();
	}
	approval(id: string, name: string, requested: boolean): void {
		if (requested) this.#approvals.set(id, name); else this.#approvals.delete(id);
		this.#main.state = this.#approvals.size ? "blocked" : "working";
		this.flush();
	}
	end(messages: AgentMessage[], willContinue?: boolean, awaitingAsyncWork?: boolean): void {
		const last = messages.findLast(message => message.role === "assistant");
		this.#main.state = willContinue || awaitingAsyncWork ? "working" :
			last?.role !== "assistant" || last.stopReason === "aborted" ? "idle" : last.stopReason === "error" ? "error" : "done";
		this.#main.intent = awaitingAsyncWork ? "等待后台任务" : "";
		this.flush();
	}
	finish(): void {
		this.#closed = true;
		this.#unsubscribe();
		this.ctx.clearTimer(this.#timer);
		this.flush();
	}
}

export const REX_WORKSPACE_SKILLS_PROVIDER_ID = "rex-workspace";

export function rexWorkspaceEnabled(): boolean {
	return process.env.REX_SUBAGENT_VIEWER !== "0" && Boolean(process.env.REX_SESSION && process.env.REX_BLOCK);
}

export function rexWorkspaceBridge(pi: ExtensionAPI, agentDir = getAgentDir()): void {
	let evidence: Evidence | undefined;
	let workspace: Workspace | undefined;
	let live: NativeLiveState | undefined;
	let unavailable = false;
	function warn(message: string): void {
		try { pi.logger.warn(`rex-subagent-viewer: ${message}`); }
		catch { /* Observation must never terminate the agent. */ }
	}
	function ensure(ctx: ExtensionContext): void {
		if (unavailable || !rexWorkspaceEnabled() || ctx.agent.id === "advisor") return;
		const width = sidebarWidth();
		if (width === undefined) { unavailable = true; warn("REX_WORKSPACE_WIDTH 必须是 15–35 的整数"); return; }
		try {
			if (evidence && (evidence.data.source.sessionId !== ctx.sessionManager.getSessionId() ||
					evidence.data.source.sessionFile !== canonical(ctx.sessionManager.getSessionFile()) ||
					evidence.data.source.id !== ctx.agent.id || evidence.data.source.kind !== ctx.agent.kind)) {
				evidence?.finish();
				live?.finish();
				live = undefined;
				evidence = undefined;
				workspace?.finish();
				workspace = undefined;
			}
			if (ctx.agent.kind === "main") workspace ??= new Workspace(ctx, pi, warn, width, agentDir);
			if (ctx.agent.kind === "sub") live ??= new NativeLiveState(pi, ctx, warn);
			evidence ??= new Evidence(pi, ctx, () => {
				if (workspace) return workspace.currentTask();
				const root = readPrivate<{ taskId?: string; agents?: Array<{ key: string; sessionFile?: string; taskId?: string }> }>("root.json");
				const ref = pi.pi.AgentRegistry.global().get(ctx.agent.id);
				const own = root?.agents?.find(agent => ref && agent.key === key(ref) && agent.sessionFile === canonical(ctx.sessionManager.getSessionFile()));
				return own?.taskId === root?.taskId ? own?.taskId : undefined;
			}, () => workspace?.flush());
		} catch (error) { unavailable = true; warn(`无法初始化观察：${String(error)}；原任务未受影响`); }
	}
	pi.on("session_start", (_event, ctx) => { ensure(ctx); });
	pi.on("session_switch", (_event, ctx) => { ensure(ctx); });
	pi.on("agent_start", (_event, ctx) => { ensure(ctx); workspace?.start(ctx); });
	pi.on("message_start", (event, ctx) => {
		ensure(ctx);
		workspace?.input(event.message);
		live?.message(event.message, "start");
	});
	pi.on("message_update", (event, ctx) => { ensure(ctx); live?.message(event.message, "update"); });
	pi.on("message_end", (event, ctx) => { ensure(ctx); live?.message(event.message, "end"); });
	pi.on("tool_call", (event, ctx) => {
		ensure(ctx);
		evidence?.tool(event.toolCallId);
	});
	pi.on("tool_result", (event, ctx) => {
		ensure(ctx);
		if (event.toolName === "write" && !event.isError) evidence?.address(event.toolCallId, event.input);
	});
	pi.on("tool_execution_start", (event, ctx) => {
		ensure(ctx);
		evidence?.tool(event.toolCallId, true);
		workspace?.toolStart(event.toolCallId, event.toolName, event.args, event.intent);
		live?.toolStart(event.toolCallId, event.toolName, event.args);
	});
	pi.on("tool_execution_update", (event, ctx) => {
		ensure(ctx);
		live?.toolUpdate(event.toolCallId, event.toolName, event.args, event.partialResult);
	});
	pi.on("tool_execution_end", (event, ctx) => {
		ensure(ctx);
		workspace?.toolEnd(event.toolCallId);
		live?.toolEnd(event.toolCallId, event.toolName, event.result, event.isError);
	});
	pi.on("tool_approval_requested", (event, ctx) => { ensure(ctx); workspace?.approval(event.toolCallId, event.toolName, true); });
	pi.on("tool_approval_resolved", (event, ctx) => { ensure(ctx); workspace?.approval(event.toolCallId, event.toolName, false); });
	pi.on("agent_end", (event, ctx) => { ensure(ctx); workspace?.end(event.messages, event.willContinue, event.awaitingAsyncWork); });
	pi.on("session_shutdown", () => { live?.finish(); evidence?.finish(); workspace?.finish(); });
	pi.registerCommand("rex-subagents", {
		description: "打开或隐藏 Rex 工作区：show / hide / status",
		handler: async (args, ctx) => {
			ensure(ctx);
			if (!workspace) { ctx.ui.notify("当前未启用 Rex 工作区", "warning"); return; }
			const action = args.trim() || "show";
			if (action === "show") workspace.show();
			else if (action === "hide") workspace.hide();
			else if (action === "status") ctx.ui.notify(`Rex 工作区 · ${directory()}`, "info");
			else ctx.ui.notify("用法：/rex-subagents [show|hide|status]", "warning");
		},
	});
}
