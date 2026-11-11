import * as fs from "node:fs";
import * as path from "node:path";
import * as crypto from "node:crypto";

export interface AgentRow {
  key: string;
  id: string;
  generation: number;
  name: string;
  parentId?: string;
  taskId: string;
  parentToolCallId?: string;
  liveFile?: string;
  sessionFile?: string;
  status: string;
}
export interface SourceSelection {
  taskId: string;
  sourceSessionFile: string;
  sessionId: string;
  ompExecutable: string;
  pid: number;
  closed: boolean;
  selected?: AgentRow;
  cwd: string;
}
export function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid workspace object");
  return value as Record<string, unknown>;
}
function requiredString(row: Record<string, unknown>, key: string): string {
  const value = row[key];
  if (typeof value !== "string" || !value || value.includes("\0")) throw new Error(`Invalid workspace ${key}`);
  return value;
}
export function absolute(value: string): string {
  if (!path.isAbsolute(value) || value.includes("\0")) throw new Error("Expected an absolute path");
  return path.normalize(value);
}
export function privateRoot(value: string): string {
  const input = absolute(value);
  const info = fs.lstatSync(input);
  if (!info.isDirectory() || info.uid !== process.getuid!() || (info.mode & 0o777) !== 0o700) throw new Error("Workspace root must be an owned 0700 directory");
  // macOS /tmp is a system symlink; never follow a symlink at the root leaf.
  return path.join(fs.realpathSync(path.dirname(input)), path.basename(input));
}
export function privateJson(file: string, maximum = 1024 * 1024): Record<string, unknown> {
  const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.uid !== process.getuid!() || (info.mode & 0o777) !== 0o600 || info.nlink !== 1 || info.size > maximum) throw new Error("Unsafe workspace snapshot");
    return object(JSON.parse(fs.readFileSync(fd, "utf8")));
  } finally { fs.closeSync(fd); }
}

function assertModuleFile(file: string): void {
  const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.uid !== process.getuid!() || (info.mode & 0o022) || info.nlink !== 1) throw new Error("Unsafe workspace module file");
  } finally { fs.closeSync(fd); }
}

/** Controller resources are paths only; Python execution uses this fork's source. */
export function workspaceModule(value: string): string {
  const input = absolute(value);
  const info = fs.lstatSync(input);
  if (!info.isDirectory() || info.uid !== process.getuid!() || (info.mode & 0o022)) throw new Error("Unsafe workspace module directory");
  const module = fs.realpathSync(input);
  for (const name of ["worker.py", "project.py"]) {
    assertModuleFile(path.join(module, name));
  }
  return module;
}
function agentRow(value: unknown): AgentRow {
  const row = object(value);
  const id = requiredString(row, "id");
  const key = requiredString(row, "key");
  if (!Number.isInteger(row.generation) || Number(row.generation) < 0 || key !== `${id}:${row.generation}`) throw new Error("Invalid selected agent identity");
  const file = row.sessionFile;
  if (file !== undefined && file !== null && typeof file !== "string") throw new Error("Invalid agent session file");
  const liveFile = row.liveFile;
  if (liveFile !== undefined && liveFile !== `live-${crypto.createHash("sha256").update(key).digest("hex").slice(0, 24)}.json`) throw new Error("Invalid native live snapshot location");
  return {
    key, id, generation: Number(row.generation), name: typeof row.name === "string" ? row.name : id,
    parentId: typeof row.parentId === "string" ? row.parentId : undefined,
    taskId: requiredString(row, "taskId"),
    parentToolCallId: row.parentToolCallId === undefined ? undefined : requiredString(row, "parentToolCallId"),
    liveFile: typeof liveFile === "string" ? liveFile : undefined,
    sessionFile: typeof file === "string" && file ? absolute(file) : undefined,
    status: typeof row.status === "string" ? row.status : "unknown",
  };
}
/** Cross-check controller selection against the bridge authority on every poll. */
export class WorkspaceSource {
  readonly root: string;
  #identity: string | undefined;
  constructor(root: string) { this.root = privateRoot(root); }
  read(): SourceSelection {
    if (privateRoot(this.root) !== this.root) throw new Error("Workspace root changed");
    const source = privateJson(path.join(this.root, "root.json"));
    const view = privateJson(path.join(this.root, "view.json"));
    const match = /^rsv-(\d+)-(\d+)-([a-f0-9]{10})$/.exec(path.basename(this.root));
    const pid = Number(source.pid);
    if (!match || Number(match[1]) !== process.getuid!() || Number(match[2]) !== pid || !Number.isInteger(pid) || pid <= 0 || source.v !== 3 || view.v !== 1) throw new Error("Invalid workspace source identity");
    const anchor = requiredString(source, "anchor");
    if (crypto.createHash("sha256").update(anchor).digest("hex").slice(0, 10) !== match[3]) throw new Error("Workspace anchor changed");
    const taskId = requiredString(source, "taskId");
    const sessionId = requiredString(source, "sessionId");
    const sourceSessionFile = fs.realpathSync(absolute(requiredString(source, "sessionFile")));
    const ompExecutable = absolute(requiredString(source, "ompExecutable"));
    const identity = JSON.stringify([pid, anchor, sessionId, sourceSessionFile, ompExecutable]);
    if (this.#identity && identity !== this.#identity) throw new Error("Workspace source replaced");
    this.#identity = identity;
    if (view.taskId !== taskId || view.sessionFile !== source.sessionFile || view.ompExecutable !== source.ompExecutable) throw new Error("Workspace selection is awaiting the current source");
    if (typeof view.root === "string" && privateRoot(view.root) !== this.root) throw new Error("Selection belongs to another workspace");
    if (!Array.isArray(source.agents) || !Array.isArray(view.agents)) throw new Error("Invalid workspace agent roster");
    let selected: AgentRow | undefined;
    if (typeof view.selected === "string" && view.selected) {
      const bridge = source.agents.map(agentRow).find(row => row.key === view.selected);
      const controller = view.agents.map(agentRow).find(row => row.key === view.selected);
      if (!bridge || !controller || bridge.taskId !== taskId || controller.taskId !== taskId || bridge.id !== controller.id || bridge.generation !== controller.generation || bridge.sessionFile !== controller.sessionFile || bridge.parentToolCallId !== controller.parentToolCallId || bridge.liveFile !== controller.liveFile) throw new Error("Selection no longer belongs to the current task");
      selected = bridge;
      if (selected.sessionFile) {
        const canonical = fs.realpathSync(selected.sessionFile);
        const artifacts = sourceSessionFile.endsWith(".jsonl") ? sourceSessionFile.slice(0, -6) : "";
        const relative = artifacts ? path.relative(artifacts, canonical) : "..";
        if (!relative || relative.startsWith("..") || path.isAbsolute(relative) || !canonical.endsWith(".jsonl")) throw new Error("Agent transcript is outside the source artifact tree");
        if (fs.lstatSync(selected.sessionFile).isSymbolicLink()) throw new Error("Agent transcript cannot be a symlink");
        selected = { ...selected, sessionFile: canonical };
      }
    }
    const main = object(source.main);
    let alive = true;
    try { process.kill(pid, 0); } catch { alive = false; }
    return { taskId, sourceSessionFile, sessionId, ompExecutable, pid, closed: source.closed === true || view.closed === true || !alive, selected, cwd: absolute(requiredString(main, "cwd")) };
  }
}
