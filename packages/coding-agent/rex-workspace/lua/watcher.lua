-- Long-lived Rex event loop. init.lua only bootstraps this process.
local module = rex.args.module or (debug.getinfo(1, "S").source:gsub("^@", ""):match("^(.*)/lua/[^/]+$"))
assert(module, "Missing Rex Agent Workspace module path")
local Controller = dofile(module .. "/lua/controller.lua")
local handle, controller
local pending, discarding = "", false
local stopping = false
local limit = 1024 * 1024

local function log(message)
  rex.log("warn", "rex-agent-workspace: " .. tostring(message))
end

local function send(command)
  if not handle or not handle:running() then return false end
  local data, err = rex.json.encode(command)
  if not data then log(err); return false end
  local ok, write_err = handle:write(data .. "\n")
  if not ok then log(write_err) end
  return ok
end

local function stop()
  if stopping then return end
  stopping = true
  if controller then controller:shutdown() end
  send({ type = "stop" })
end

local function event(data)
  local value, err = rex.json.decode(data)
  if type(value) ~= "table" then log(err or "Invalid worker event"); return end
  if value.type == "ready" then
    if not controller then
      controller = Controller.new(rex, send, module, value.pid)
      if rex.args.rex_binary then controller.rex_binary = rex.args.rex_binary end
    end
  elseif value.type == "shutdown" then stop()
  elseif controller and not stopping then
    local ok, failure = pcall(controller.event, controller, value)
    if not ok then log(failure) end
  end
end

local function receive(chunk)
  local start = 1
  while start <= #chunk do
    local ending = chunk:find("\n", start, true)
    local piece = chunk:sub(start, ending and ending - 1 or #chunk)
    if not discarding then
      if #pending + #piece > limit then
        pending, discarding = "", true
        log("Skipped worker record larger than 1 MiB")
      else pending = pending .. piece end
    end
    if not ending then break end
    if not discarding and #pending > 0 then event(pending) end
    pending, discarding, start = "", false, ending + 1
  end
end

rex.on("block_closed", function(ctx, ev)
  if controller and not stopping then controller:block_closed(ctx.session_id, ev.block_id) end
end)
rex.on("session_destroyed", function(_, ev)
  if not controller or stopping then return end
  local roots = {}
  for root, state in pairs(controller.roots) do
    if state.source.session == ev.session_id then roots[#roots + 1] = root end
  end
  for _, root in ipairs(roots) do controller:gone(root) end
end)

local command = { "/usr/bin/python3", module .. "/worker.py", "watch", "--module", module }
if rex.args.scan_dir then
  command[#command + 1], command[#command + 2] = "--scan-dir", rex.args.scan_dir
end
if rex.args.state_dir then
  assert(rex.args.state_dir:sub(1, 1) == "/", "state_dir must be absolute")
  table.insert(command, 1, "REX_WORKSPACE_STATE_DIR=" .. rex.args.state_dir)
  table.insert(command, 1, "/usr/bin/env")
end
local worker, err = rex.exec.start(command, {
  detach = true, stdin = true, stdout = receive,
  stderr = function(data) io.stderr:write(data); io.stderr:flush() end,
})
assert(worker, err)
handle = worker

rex.every(0.25, function()
  if not handle:running() then
    local result = handle:wait(1)
    -- A duplicate singleton exits before ready; it owns nothing to clean up.
    if controller and not stopping then
      local ok, failure = pcall(controller.shutdown, controller)
      if not ok then log(failure) end
    end
    rex.stop(result and result.code == 0 and 0 or 1)
  end
  if controller and not stopping then
    local ok, failure = pcall(controller.tick, controller, os.clock())
    if not ok then log(failure) end
  end
end)
