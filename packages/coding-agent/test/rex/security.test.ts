import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as crypto from "node:crypto";
import { WorkspaceSource, workspaceModule } from "../../src/rex/readonly/security";

const fixtures: string[] = [];
afterEach(() => { for (const directory of fixtures.splice(0)) fs.rmSync(directory, { recursive: true, force: true }); });
function fixture() {
  const anchor = crypto.randomUUID();
  const digest = crypto.createHash("sha256").update(anchor).digest("hex").slice(0, 10);
  const root = path.join(fs.realpathSync(os.tmpdir()), `rsv-${process.getuid!()}-${process.pid}-${digest}`);
  fs.mkdirSync(root, { mode: 0o700 });
  fixtures.push(root);
  const main = path.join(root, "main.jsonl");
  const artifacts = main.slice(0, -6);
  fs.writeFileSync(main, "{}\n", { mode: 0o600 });
  fs.mkdirSync(artifacts, { mode: 0o700 });
  const file = path.join(artifacts, "Agent.jsonl");
  fs.writeFileSync(file, "{}\n", { mode: 0o600 });
  const agent = { id: "Agent", generation: 0, key: "Agent:0", name: "Agent", parentId: "Main", status: "running", taskId: "turn-current", parentToolCallId: "call-current", sessionFile: file };
  const source = { v: 3, pid: process.pid, sessionId: "main-id", session: "rex-session", anchor, sessionFile: main, ompExecutable: "/usr/local/bin/omp", taskId: "turn-current", main: { cwd: root }, agents: [agent], closed: false };
  const view = { v: 1, root, sessionFile: main, ompExecutable: source.ompExecutable, taskId: source.taskId, agents: [{ ...agent }], selected: agent.key, closed: false };
  const publish = () => {
    fs.writeFileSync(path.join(root, "root.json"), JSON.stringify(source), { mode: 0o600 });
    fs.writeFileSync(path.join(root, "view.json"), JSON.stringify(view), { mode: 0o600 });
  };
  publish();
  return { root, source, view, agent, publish };
}
test("legacy extension roots cannot authorize the fork's native readonly viewer", () => {
  const state = fixture();
  state.source.v = 2;
  state.publish();
  expect(() => new WorkspaceSource(state.root).read()).toThrow("Invalid workspace source identity");
});
test("selection requires matching current task and exact bridge membership", () => {
  const state = fixture();
  const source = new WorkspaceSource(state.root);
  expect(source.read().selected?.key).toBe("Agent:0");
  state.view.taskId = "turn-previous";
  state.publish();
  expect(() => source.read()).toThrow("awaiting the current source");
  state.view.taskId = state.source.taskId;
  state.source.agents = [];
  state.publish();
  expect(() => source.read()).toThrow("current task");
});
test("a controller cannot replace the selected file or tool-call ownership", () => {
  const state = fixture();
  const source = new WorkspaceSource(state.root);
  state.view.agents[0].parentToolCallId = "another-call";
  state.publish();
  expect(() => source.read()).toThrow("current task");
  state.view.agents[0].parentToolCallId = state.agent.parentToolCallId;
  state.view.agents[0].sessionFile = state.source.sessionFile;
  state.publish();
  expect(() => source.read()).toThrow("current task");
});
test("matching bridge/controller paths still cannot escape the artifact tree", () => {
  const state = fixture();
  const source = new WorkspaceSource(state.root);
  state.agent.sessionFile = state.source.sessionFile;
  state.view.agents[0].sessionFile = state.source.sessionFile;
  state.publish();
  expect(() => source.read()).toThrow("outside the source artifact tree");
});
test("root-source substitution is rejected and the source stays byte-identical", () => {
  const state = fixture();
  const source = new WorkspaceSource(state.root);
  const before = fs.readFileSync(path.join(state.root, "root.json"));
  source.read();
  source.read();
  expect(fs.readFileSync(path.join(state.root, "root.json"))).toEqual(before);
  state.source.sessionId = "other-source";
  state.publish();
  expect(() => source.read()).toThrow("source replaced");
});
test("explicit current-task membership does not require a parent call id", () => {
  const state = fixture();
  Reflect.deleteProperty(state.agent, "parentToolCallId");
  Reflect.deleteProperty(state.view.agents[0], "parentToolCallId");
  state.publish();
  expect(new WorkspaceSource(state.root).read().selected?.taskId).toBe(state.source.taskId);
  state.agent.taskId = "previous-task";
  state.publish();
  expect(() => new WorkspaceSource(state.root).read()).toThrow("current task");
});

function moduleFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "rsv-module-"));
  fixtures.push(root);
  const source = path.join(root, "source");
  const installed = path.join(root, "installed");
  for (const directory of [source, installed]) {
    fs.mkdirSync(directory, { mode: 0o700 });
    for (const name of ["worker.py", "project.py"]) fs.writeFileSync(path.join(directory, name), `# ${name}\n`, { mode: 0o600 });
  }
  return { source, installed };
}

test("a controller resource update does not invalidate an existing runtime's detail pane", () => {
  const state = moduleFixture();
  expect(workspaceModule(state.installed)).toBe(fs.realpathSync(state.installed));
  fs.appendFileSync(path.join(state.installed, "worker.py"), "print('different code')\n");
  expect(workspaceModule(state.installed)).toBe(fs.realpathSync(state.installed));
});

test("unsafe installed resource permissions and symlinks cannot execute Python", () => {
  const state = moduleFixture();
  const worker = path.join(state.installed, "worker.py");
  fs.chmodSync(worker, 0o620);
  expect(() => workspaceModule(state.installed)).toThrow("Unsafe workspace module file");
  fs.chmodSync(worker, 0o600);
  fs.unlinkSync(worker);
  fs.symlinkSync(path.join(state.source, "worker.py"), worker);
  expect(() => workspaceModule(state.installed)).toThrow();
  fs.chmodSync(state.installed, 0o720);
  expect(() => workspaceModule(state.installed)).toThrow("Unsafe workspace module directory");
});
