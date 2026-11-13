-- Rex owns the workspace: the agent bridge never creates or manipulates panes.
local Controller = {}
Controller.__index = Controller
local terminal = "com.superlogical.terminal"
local dir = debug.getinfo(1, "S").source:match("^@?(.*/)") or "./"
local ui = dofile(dir .. "ui.lua")

local function blocks(node, out)
  out = out or {}
  if type(node) ~= "table" then return out end
  if node.block_id then out[#out + 1] = node.block_id end
  if node.split then blocks(node.split.before, out); blocks(node.split.after, out) end
  return out
end

local function window_of(view, id)
  for _, window in ipairs(view.windows or {}) do
    for _, layer in ipairs(window.layers or {}) do
      for _, block in ipairs(blocks(layer.layout)) do
        if block == id then return window end
      end
    end
  end
end

local function agent_of(state, key)
  if not key then return nil end
  for _, agent in ipairs(state.source.agents or {}) do
    if agent.key == key then return agent end
  end
end

local function window_label(state)
  local main = state.source.main
  local label = type(main.title) == "string" and main.title:match("^%s*(.-)%s*$") or ""
  if label == "" then label = main.cwd:match("([^/]+)/?$") or "OMP" end
  label = ui.clip(ui.sanitize(label:sub(1, 128), true), 64):match("^%s*(.-)%s*$")
  return label ~= "" and label or "OMP"
end

local function anchor_owner(window, anchor)
  for _, layer in ipairs(window.layers or {}) do
    for _, block in ipairs(layer.blocks or {}) do
      if block.block_id == anchor then return block.owner end
    end
  end
end

function Controller.new(api, send, module, worker_pid)
  return setmetatable({ api = api, send = send, module = module, worker_pid = worker_pid,
    roots = {}, stopping = false, rex_binary = os.getenv("REX_WORKSPACE_REX") or "rex" }, Controller)
end

function Controller:call(method, args)
  local value, err = self.api.call(method, args)
  if not value then error(tostring(err or method .. " returned no result"), 0) end
  return value
end

function Controller:view(state)
  local result = self:call("session.view", { session_id = state.source.session })
  return result.view or result
end

function Controller:sync_label(state)
  if not state.window or state.custom_label then return end
  local label = window_label(state)
  if label == state.window_label then return end
  local window = window_of(self:view(state), state.source.anchor)
  if not window or window.window_id ~= state.window then return end
  if state.window_label and window.label ~= state.window_label then
    state.custom_label = true
    return
  end
  self:call("session.set_window_label", { session_id = state.source.session,
    window_id = state.window, label = label })
  state.window_label = label
end

function Controller:foreground(state, owner)
  self:call("session.focus_block", { session_id = state.source.session, block_id = state.source.anchor })
  if not owner then return end
  local client, err = self.api.call("client.inspect", { client_id = owner })
  if client and client.client and client.client.info and client.client.info.kind == "app" then
    local _, switch_err = self.api.call("client.perform", { client_id = owner, action = "session.select",
      session_id = state.source.session, block_id = state.source.anchor, timeout_ms = 1500,
      args = { session_id = state.source.session, window_id = state.window } })
    err = switch_err
  end
  if err then self:notice(state, "Cannot switch to workspace: " .. tostring(err), true) end
end

function Controller:log(message)
  if self.api.log then self.api.log("warn", "rex-agent-workspace: " .. tostring(message)) end
end

function Controller:emit(state, kind, extra)
  local command = extra or {}
  command.type, command.root = kind, state.root
  self.send(command)
end

function Controller:write(state, name, data)
  local encoded = self.api.json.encode(data)
  state.serialized = state.serialized or {}
  if state.serialized[name] == encoded then return end
  state.serialized[name] = encoded
  self:emit(state, "publish", { name = name, data = data })
end

function Controller:publish(state)
  local source = state.source
  self:write(state, "workspace.json", { v = 2, controller = "lua", pid = self.worker_pid,
    ompPid = source.pid, session = source.session, anchor = source.anchor,
    sessionId = source.sessionId, panel = state.panel, detail = state.detail,
    taskId = source.taskId,
    preview = state.preview, previewLayer = state.preview_layer, window = state.window,
    windowLabel = state.window_label, customLabel = state.custom_label,
    selected = state.selected, requestSeq = state.request_seq, hidden = state.hidden,
    sidebarPercent = state.width })
  self:write(state, "view.json", { v = 1, root = state.root, main = source.main,
    sessionFile = source.sessionFile, ompExecutable = source.ompExecutable, taskId = source.taskId,
    agentDir = source.agentDir,
    diagnostics = source.diagnostics or {},
    project = state.project or {}, agents = source.agents or {}, selected = state.selected,
    notice = state.notice, noticeError = state.notice_error, closed = state.hidden or self.stopping })
end

function Controller:notice(state, text, is_error)
  state.notice, state.notice_error = tostring(text or ""), is_error == true
  self:publish(state)
end

function Controller:valid_anchor(state)
  self:call("session.attach", { session_id = state.source.session })
  local process, err = self.api.block.call(terminal, "process", {
    session_id = state.source.session, block_id = state.source.anchor })
  if not process then error(tostring(err or "The conversation pane is unavailable"), 0) end
  local candidates = { process.child or {}, process.foreground or {} }
  for _, item in ipairs(process.ancestors or {}) do candidates[#candidates + 1] = item end
  for _, item in ipairs(candidates) do
    if item.pid == state.source.pid then
      local view = self:view(state)
      local window = window_of(view, state.source.anchor)
      if not window then error("The conversation pane is not placed in a window", 0) end
      return view, window
    end
  end
  error("The anchor pane does not belong to this OMP process; no layout was changed", 0)
end

function Controller:layout(program, state, label)
  if program == "detail" then
    return self.api.layout.block { flavor = terminal .. ".shell", label = label,
      options = { shell = "none", cwd = state.source.main.cwd,
        command = { state.source.ompExecutable, "rex-view", "--root", state.root, "--module", self.module } } }
  end
  return self.api.layout.block { flavor = terminal .. ".shell", label = label,
    options = { shell = "none", cwd = state.source.main.cwd,
      command = { self.rex_binary, "--autostart=false", "do", self.module .. "/lua/" .. program .. ".lua",
        "root=" .. state.root, "module=" .. self.module } } }
end

function Controller:disown(state, id)
  -- Release only the new observer pane, never every block in the session.
  local _, err = self.api.call("session.disown_block", {
    session_id = state.source.session, block_ids = { id } })
  if err then self:log(err) end
end

function Controller:close_block(state, id)
  if not id then return end
  local _, err = self.api.call("block.close", {
    session_id = state.source.session, block_id = id })
  if err then self:log(err) end
end

function Controller:close_detail(state)
  local id = state.detail
  state.detail, state.selected = nil, nil
  self:close_block(state, id)
end

function Controller:close_preview(state, restore_focus)
  local id, layer = state.preview, state.preview_layer
  state.preview, state.preview_layer = nil, nil
  local active = false
  if restore_focus and id then
    local ok, view = pcall(self.view, self, state)
    active = ok and view.active_window_id == state.window
  end
  if layer then
    local _, err = self.api.call("session.close_layer", {
      session_id = state.source.session, layer_id = layer })
    if err then self:log(err) end
  else
    self:close_block(state, id)
  end
  if active and state.panel then
    self.api.call("session.focus_block", { session_id = state.source.session, block_id = state.panel })
  end
end

function Controller:hide(state)
  state.hidden = true
  self:close_preview(state, false)
  self:close_detail(state)
  local id = state.panel
  state.panel, state.window = nil, nil
  self:close_block(state, id)
  self:publish(state)
end

function Controller:observe(state)
  if not state.observed then
    state.observed = true
    self:emit(state, "observe")
  end
end

function Controller:adopt(state, saved)
  if type(saved) ~= "table" or saved.v ~= 2 or saved.controller ~= "lua" or
      saved.ompPid ~= state.source.pid or saved.anchor ~= state.source.anchor or
      saved.session ~= state.source.session or saved.sessionId ~= state.source.sessionId then return false end
  local view, owner = self:valid_anchor(state)
  state.validated = true
  state.request_seq = tonumber(saved.requestSeq) or -1
  state.window_label, state.custom_label = saved.windowLabel, saved.customLabel == true
  state.hidden = saved.hidden == true
  if saved.panel and window_of(view, saved.panel) == owner then
    state.panel, state.window = saved.panel, owner.window_id
    state.hidden = false
    if saved.detail and window_of(view, saved.detail) == owner then
      state.detail = saved.detail
      if saved.taskId == state.source.taskId and agent_of(state, saved.selected) then state.selected = saved.selected
      else self:close_detail(state) end
    end
    if saved.preview and window_of(view, saved.preview) == owner then
      state.preview, state.preview_layer = saved.preview, saved.previewLayer
    end
    self:observe(state)
  else
    -- The panel may have been closed while the watcher was down. Retire only
    -- the orphaned observer IDs tied to this validated conversation.
    if saved.detail and window_of(view, saved.detail) == owner then
      self:close_block(state, saved.detail)
    end
    if saved.preview and window_of(view, saved.preview) == owner then
      self:close_block(state, saved.preview)
    end
    -- A saved identity is not enough to recreate a manually closed panel.
    state.hidden = true
  end
  return true
end

function Controller:show(state)
  local view, window = self:valid_anchor(state)
  state.validated = true
  if state.panel and window_of(view, state.panel) then
    state.hidden = false
    self:observe(state)
    self:publish(state)
    return
  end
  local ratio = 1 - state.width / 100
  local neighbors = {}
  for _, layer in ipairs(window.layers or {}) do
    for _, id in ipairs(blocks(layer.layout)) do neighbors[#neighbors + 1] = id end
  end
  local source = state.source
  local moved, owner = false, anchor_owner(window, source.anchor)
  if #neighbors > 1 then
    local label = window_label(state)
    local created = self:call("session.new_window", { session_id = source.session,
      window_label = label, focus = false, layout = self:layout("panel", state, "Workspace") })
    state.panel, state.window = created.block_ids[1], created.window_id
    state.window_label, state.custom_label = label, false
    local ok, err = pcall(self.call, self, "session.move_block", { session_id = source.session,
      block_id = source.anchor, anchor_block_id = state.panel,
      direction = "horizontal", side = "before", ratio = ratio })
    if not ok then
      local panel = state.panel
      state.panel, state.window = nil, nil
      self:close_block(state, panel)
      error(err, 0)
    end
    moved = true
  else
    local created = self:call("session.new_split", { session_id = source.session,
      anchor_id = source.anchor, direction = "horizontal", side = "after", ratio = ratio,
      focus = false, layout = self:layout("panel", state, "Workspace") })
    state.panel, state.window = created.block_ids[1], window.window_id
  end
  state.hidden = false
  self:disown(state, state.panel)
  self:observe(state)
  self:sync_label(state)
  if moved then self:foreground(state, owner) end
  self:publish(state)
end

function Controller:select(state, key)
  if type(key) ~= "string" or not agent_of(state, key) then error("The subagent was removed", 0) end
  if not state.panel or state.hidden then error("The workspace panel is closed", 0) end
  local view = self:view(state)
  if not window_of(view, state.panel) then error("The workspace panel is closed", 0) end
  if state.detail and not window_of(view, state.detail) then state.detail = nil end
  if not state.detail then
    local created = self:call("session.new_split", { session_id = state.source.session,
      anchor_id = state.panel, direction = "vertical", side = "after", ratio = 0.55,
      focus = false, layout = self:layout("detail", state, "Subagent") })
    state.detail = created.block_ids[1]
    self:disown(state, state.detail)
  end
  state.selected, state.notice, state.notice_error = key, nil, nil
  self:publish(state)
end

function Controller:preview(state, event)
  if not state.panel or state.hidden then return end
  local view = self:view(state)
  if not window_of(view, state.panel) then return end
  if state.preview and not window_of(view, state.preview) then
    state.preview, state.preview_layer = nil, nil
  end
  self:write(state, "preview.json", { v = 1, title = "Changes", text = event.text or "", diff = true })
  if state.preview then
    self:call("session.focus_block", { session_id = state.source.session, block_id = state.preview })
    return
  end
  local created = self:call("session.new_layer", { session_id = state.source.session,
    window_id = state.window, bounds = { 0.08, 0.07, 0.84, 0.86 }, focus = true,
    layout = self:layout("preview", state, "Changes") })
  state.preview = created.block_ids[1]
  state.preview_layer = created.layer_id
  self:disown(state, state.preview)
  self:publish(state)
end

function Controller:source(event)
  local source = event.source
  if type(source) ~= "table" or source.v ~= 3 then return end
  local state = self.roots[event.root]
  if not state then
    state = { root = event.root, source = source, hidden = true, request_seq = -1,
      width = 25, project = {}, retry_at = 0 }
    self.roots[event.root] = state
    if event.layout then
      local ok, err = pcall(self.adopt, self, state, event.layout)
      if not ok then self:log(err) end
    end
  else
    local previous = state.source
    if source.pid ~= previous.pid or source.anchor ~= previous.anchor or source.session ~= previous.session then
      self:hide(state)
      self:emit(state, "forget")
      self.roots[event.root] = nil
      return self:source(event)
    end
    if source.sessionId ~= previous.sessionId or source.main.cwd ~= previous.main.cwd then
      self:close_preview(state, false)
      self:close_detail(state)
      state.observed, state.project = false, {}
    end
    if source.taskId ~= previous.taskId then self:close_detail(state) end
    state.source = source
  end
  local display = source.display or {}
  local width = tonumber(display.width)
  if not width or width % 1 ~= 0 or width < 15 or width > 35 then
    if state.panel then self:hide(state) end
    self:notice(state, "Sidebar width must be an integer from 15 to 35", true)
    return
  end
  state.width = width
  if source.closed then self:gone(event.root); return end
  if state.selected and not agent_of(state, state.selected) then self:close_detail(state) end
  local sequence = tonumber(display.seq) or 0
  if sequence ~= state.request_seq then
    state.request_seq = sequence
    if display.action == "show" then
      state.hidden = false
      local ok, err = pcall(self.show, self, state)
      if not ok then state.last_error = tostring(err); self:log(err) end
    else self:hide(state) end
  elseif not state.hidden and not state.observed then
    self:observe(state)
  end
  if state.panel and not state.hidden then
    local ok, err = pcall(self.sync_label, self, state)
    if not ok then self:notice(state, err, true) end
  end
  self:publish(state)
end

function Controller:request(event)
  local state = self.roots[event.root]
  if not state then return end
  local request, view = event.request or {}, event.view
  local kind = request.type
  local ok, err = pcall(function()
    if view == "detail" and kind == "close-detail" then
      self:close_detail(state); self:publish(state)
    elseif view == "preview" and kind == "close-preview" then
      self:close_preview(state, true); self:publish(state)
    elseif view ~= "panel" then error("This view is read-only", 0)
    elseif kind == "show" then self:show(state)
    elseif kind == "hide" then self:hide(state)
    elseif kind == "select" then self:select(state, request.key)
    elseif kind == "diff" or kind == "commit" or kind == "run-script" or kind == "add-script" or
        kind == "open-editor" or kind == "open-url" then
      if state.hidden or not state.panel then error("The workspace panel is closed", 0) end
      self:emit(state, "project", { request = request })
    else error("Unknown workspace action", 0) end
  end)
  if not ok then self:notice(state, err, true) end
end

function Controller:gone(root)
  local state = self.roots[root]
  if not state then return end
  self:hide(state)
  self.roots[root] = nil
  self:emit(state, "forget")
end

function Controller:event(event)
  if self.stopping then return end
  if event.type == "source" then self:source(event)
  elseif event.type == "gone" then self:gone(event.root)
  elseif event.type == "request" then self:request(event)
  elseif event.type == "project" then
    local state = self.roots[event.root]
    if state then state.project = event.project; self:publish(state) end
  elseif event.type == "response" then
    local state = self.roots[event.root]
    if not state then return end
    local response = event.event or {}
    if response.type == "diff" then self:preview(state, response)
    elseif response.type == "notice" then self:notice(state, response.text, response.error) end
  end
end

function Controller:block_closed(session, id)
  for root, state in pairs(self.roots) do
    if state.source.session == session then
      if id == state.source.anchor then self:gone(root)
      elseif id == state.panel then
        -- Keep hidden until a newer explicit bridge show request arrives.
        state.panel = nil; self:hide(state)
      elseif id == state.detail then state.detail, state.selected = nil, nil; self:publish(state)
      elseif id == state.preview then
        state.preview, state.preview_layer = nil, nil
        self:publish(state)
      end
    end
  end
end

function Controller:tick(now)
  for _, state in pairs(self.roots) do
    if not state.hidden and not state.panel and now >= state.retry_at then
      state.retry_at = now + 2
      local ok, err = pcall(self.show, self, state)
      if not ok and tostring(err) ~= state.last_error then state.last_error = tostring(err); self:log(err) end
    end
  end
end

function Controller:shutdown()
  self.stopping = true
  for _, state in pairs(self.roots) do
    local hidden, sequence = state.hidden, state.request_seq
    self:hide(state)
    -- An observer restart is not a user's dismissal. Reopen visible workspaces
    -- on the next watcher, but continue honoring manually hidden ones.
    state.hidden, state.request_seq = hidden, hidden and sequence or -1
    self:publish(state)
  end
  self.roots = {}
end

return Controller
