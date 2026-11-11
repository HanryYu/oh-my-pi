-- Observable workspace transitions; no server calls or model requests.
local path = debug.getinfo(1, "S").source:gsub("^@", "")
local base = path:match("^(.*)/test/rex%-workspace/[^/]+$") or "."
local Controller = dofile(base .. "/packages/coding-agent/rex-workspace/lua/controller.lua")
local encode = rex.json.encode
local checks = 0
local function check(value, message)
  assert(value, message)
  checks = checks + 1
end
local function block(id) return { block_id = id } end
local function clone(value)
  if type(value) ~= "table" then return value end
  local out = {}; for k, v in pairs(value) do out[k] = clone(v) end; return out
end
local function has(node, id)
  return node and ((node.block_id == id) or
    (node.split and (has(node.split.before, id) or has(node.split.after, id))))
end
local function remove(node, id)
  if not node then return nil end
  if node.block_id then return node.block_id ~= id and node or nil end
  local s = node.split
  s.before, s.after = remove(s.before, id), remove(s.after, id)
  if not s.before then return s.after end
  if not s.after then return s.before end
  return node
end
local function insert(node, id, fresh, args)
  if node.block_id then
    if node.block_id ~= id then return node end
    return { split = { direction = args.direction, ratio = args.ratio,
      before = args.side == "before" and fresh or node,
      after = args.side == "before" and node or fresh } }
  end
  node.split.before = insert(node.split.before, id, fresh, args)
  node.split.after = insert(node.split.after, id, fresh, args)
  return node
end
local function fixture(with_neighbor)
  local serial = 0
  local view = { active_window_id = "work", windows = {
    { window_id = "work", focused_block_id = "main", layers = {
      { layer_id = "tiled", layout = with_neighbor and { split = {
        direction = "horizontal", ratio = 0.5, before = block("main"), after = block("unrelated") } } or block("main") }
    } }
  } }
  local function next_id() serial = serial + 1; return "observer-" .. serial end
  local function locate(id)
    for _, w in ipairs(view.windows) do for _, l in ipairs(w.layers) do
      if has(l.layout, id) then return w, l end
    end end
  end
  local api = { json = { encode = encode }, layout = { block = function(args) return { block = args } end },
    log = function() end, block = {} }
  local process_pid = 101
  api.block.call = function() return { foreground = { pid = process_pid } } end
  api.call = function(method, args)
    if method == "session.attach" or method == "session.disown_block" then return {} end
    if method == "session.view" then return clone(view) end
    if method == "session.new_split" then
      local w, l = locate(args.anchor_id); assert(w, "missing anchor")
      local id = next_id(); l.layout = insert(l.layout, args.anchor_id, block(id), args)
      if args.focus then view.active_window_id, w.focused_block_id = w.window_id, id end
      return { block_ids = { id } }
    end
    if method == "session.new_window" then
      local id = next_id()
      local w = { window_id = "window-" .. id, focused_block_id = id,
        layers = { { layer_id = "tiled-" .. id, layout = block(id) } } }
      view.windows[#view.windows + 1] = w
      if args.focus then view.active_window_id = w.window_id end
      return { window_id = w.window_id, block_ids = { id } }
    end
    if method == "session.move_block" then
      local _, previous = locate(args.block_id); assert(previous)
      previous.layout = remove(previous.layout, args.block_id)
      local _, target = locate(args.anchor_block_id); assert(target)
      target.layout = insert(target.layout, args.anchor_block_id, block(args.block_id), args)
      return {}
    end
    if method == "block.close" then
      local _, l = locate(args.block_id)
      if l then l.layout = remove(l.layout, args.block_id) end
      return {}
    end
    if method == "session.focus_block" then
      local w = locate(args.block_id); assert(w)
      w.focused_block_id, view.active_window_id = args.block_id, w.window_id
      return {}
    end
    if method == "session.new_layer" then
      local target
      for _, w in ipairs(view.windows) do if w.window_id == args.window_id then target = w end end
      assert(target)
      local id = next_id(); local layer_id = "float-" .. id
      target.layers[#target.layers + 1] = { layer_id = layer_id, layout = block(id) }
      if args.focus then target.focused_block_id, view.active_window_id = id, target.window_id end
      return { block_ids = { id }, layer_id = layer_id }
    end
    if method == "session.close_layer" then
      for _, w in ipairs(view.windows) do for i = #w.layers, 1, -1 do
        if w.layers[i].layer_id == args.layer_id then table.remove(w.layers, i) end
      end end
      return {}
    end
    error("unexpected API " .. method)
  end
  local messages = {}
  local c = Controller.new(api, function(event) messages[#messages + 1] = clone(event) end, "/module", 202)
  local source = { v = 3, pid = 101, session = "session", anchor = "main", sessionId = "chat",
    taskId = "task-1", sessionFile = "/sessions/main.jsonl", ompExecutable = "/bin/omp",
    main = { state = "working", cwd = "/project" }, closed = false,
    display = { action = "show", seq = 1, width = 25 }, agents = {
      { key = "A:1", id = "A", generation = 1, status = "working" },
      { key = "B:1", id = "B", generation = 1, status = "working" } } }
  local function update(saved) c:event { type = "source", root = "/private/root", source = clone(source), layout = saved } end
  local function request(kind, key, selected_view)
    c:event { type = "request", root = "/private/root", view = selected_view or "panel", request = { type = kind, key = key } }
  end
  return { c = c, source = source, update = update, request = request, view = view,
    locate = locate, api = api, messages = messages, wrong_pid = function() process_pid = 999 end,
    state = function() return c.roots["/private/root"] end }
end

local f = fixture(false)
f.update()
local s = f.state()
check(s.panel ~= nil and not s.hidden, "show opens the panel")
check(f.view.windows[1].layers[1].layout.split.ratio == 0.75, "conversation keeps its proportional width")
check(f.view.windows[1].focused_block_id == "main", "opening observer preserves focus")
local panel = s.panel
f.update()
check(s.panel == panel, "duplicate snapshots do not create another panel")
f.request("select", "A:1")
local detail = s.detail
local native_command = f.c:layout("detail", s, "Detail").block.options.command
check(native_command[1] == s.source.ompExecutable and native_command[2] == "rex-view",
  "details launch the source runtime's native readonly command, not a separate viewer")
f.request("select", "B:1")
check(s.detail == detail and s.selected == "B:1", "switches content in the same detail pane")
f.source.agents[2].status = "parked"
f.update()
check(s.detail == detail, "parking does not destroy a view")
f.source.agents[2].status = "done"
f.update()
check(s.detail == detail, "completion does not destroy a view")
f.source.agents[2] = { key = "B:2", id = "B", generation = 2, status = "working" }
f.update()
check(s.detail == nil and s.selected == nil and not f.locate(detail), "replacement generation removes old selected identity")
f.request("select", "B:1")
check(s.detail == nil, "stale selection does not open a pane")
f.request("select", "B:2", "detail")
check(s.detail == nil, "detail view cannot select or issue project actions")
f.request("select", "B:2")
check(s.detail ~= nil, "current generation remains selectable")
f.api.call("block.close", { block_id = panel })
f.c:block_closed("session", panel)
check(s.hidden and not s.detail, "manual panel close closes only its own detail")
f.update()
check(not s.panel, "ordinary state updates respect manual dismissal")
f.source.display.seq = 2
f.update()
check(s.panel ~= nil and s.panel ~= panel, "new show request can reopen dismissed workspace")
f.request("select", "A:1")
f.source.taskId = "task-2"
f.update()
check(not s.detail and not s.selected, "new task closes the previous selection even if a registry key survives")
f.request("select", "A:1")
f.source.sessionId = "another-chat"
f.source.agents = {}
f.update()
check(not s.detail and not s.selected, "chat cutover drops previous selected history")

local neighbors = fixture(true)
neighbors.update()
local ns = neighbors.state()
local main_window = neighbors.locate("main")
local other_window = neighbors.locate("unrelated")
check(main_window.window_id ~= other_window.window_id, "moves only own conversation into a workspace tab")
check(other_window.window_id == "work", "unrelated pane remains in its original tab")
neighbors.c:shutdown()
check(neighbors.locate("unrelated") ~= nil and neighbors.locate("main") ~= nil, "shutdown never closes main or unrelated programs")

local denied = fixture(false)
denied.wrong_pid(); denied.update()
check(not denied.state().panel, "mismatched process identity cannot alter layout")

local restore = fixture(false)
restore.update(); restore.request("select", "A:1")
local before = restore.state()
local saved = { v = 2, controller = "lua", ompPid = 101, session = "session", anchor = "main",
  sessionId = "chat", panel = before.panel, detail = before.detail, selected = "A:1", requestSeq = 1, hidden = false }
saved.taskId = restore.source.taskId
local recovered = Controller.new(restore.api, function() end, "/module", 303)
recovered:event { type = "source", root = "/private/root", source = clone(restore.source), layout = saved }
local after = recovered.roots["/private/root"]
check(after.panel == saved.panel and after.detail == saved.detail and after.selected == "A:1", "watcher restart adopts exact live pane associations")
restore.c:event { type = "response", root = "/private/root", event = { type = "diff", text = "+one" } }
local preview = before.preview
restore.c:event { type = "response", root = "/private/root", event = { type = "diff", text = "+two" } }
check(before.preview == preview, "subsequent previews reuse one floating pane")
restore.request("close-preview", nil, "preview")
check(not before.preview and restore.view.windows[1].focused_block_id == before.panel, "closing preview returns focus to its panel")
local orphan = fixture(false)
orphan.update(); orphan.request("select", "A:1")
local osaved = { v = 2, controller = "lua", ompPid = 101, session = "session", anchor = "main",
  sessionId = "chat", panel = orphan.state().panel, detail = orphan.state().detail,
  selected = "A:1", requestSeq = 1, hidden = false }
orphan.api.call("block.close", { block_id = osaved.panel })
local recovery = Controller.new(orphan.api, function() end, "/module", 404)
recovery:event { type = "source", root = "/private/root", source = clone(orphan.source), layout = osaved }
check(not orphan.locate(osaved.detail) and orphan.locate("main"), "restart retires orphaned details without touching the conversation")
print("Lua controller behavior checks passed")
