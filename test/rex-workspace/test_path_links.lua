-- Pure routing fixtures: no terminals, network connections, or model requests.
local path = debug.getinfo(1, "S").source:gsub("^@", "")
local base = path:match("^(.*)/test/rex%-workspace/[^/]+$") or "."
local script = base .. "/packages/coding-agent/rex-workspace/lua/open-path.lua"
local native = rex
local session = "session:01a18bb0-ff4f-7829-aa39-eb90131cb46f"
local anchor = "block:01a18bb1-ffff-7000-aaaa-000000000001"
local checks = 0
local function check(value, message) assert(value, message); checks = checks + 1 end
local function clone(value)
    if type(value) ~= "table" then return value end
    local result = {}; for key, item in pairs(value) do result[key] = clone(item) end; return result
end
local function host(instance, owns)
    local state = {calls = {}, views = 0, statuses = 0, attaches = 0, created = 0, kind = "app"}
    state.view = {session_id = session, windows = {{window_id = "source-window", layers = {{
        layout = {split = {direction = "horizontal", ratio = 0.8,
            before = {block_id = owns and anchor or "other"}, after = {block_id = "sidebar"}}},
        blocks = {{block_id = anchor, owner = "source-app"}, {block_id = "sidebar", owner = "observer"}}
    }}}}}
    local api = {}
    api.call = function(method, args)
        state.calls[#state.calls + 1] = {method = method, args = clone(args)}
        if method == "server.status" then
            state.statuses = state.statuses + 1
            if state.offline then return nil, "Host is unreachable" end
            return {instance_id = state.restart and state.statuses > 2 and "replacement" or instance}
        elseif method == "session.attach" then
            check(args.session_id == session, "attach only the URL's validated source session")
            if state.attach_refused then return nil, "Cannot attach to source session" end
            state.attaches, state.attached = state.attaches + 1, true
            return {}
        elseif method == "session.view" then
            check(state.attached, "every raw session.view requires an explicit prior attach")
            state.attached = false
            state.views = state.views + 1
            if state.missing_session then return nil, "Session not found" end
            if state.disappear and state.views > 1 then return {session_id = session, windows = {}} end
            return clone(state.view)
        elseif method == "session.new_window" then
            state.created = state.created + 1; state.open = args
            return {window_id = "new-window-" .. state.created, block_ids = {"new-block-" .. state.created}}
        elseif method == "session.disown_block" or method == "session.focus_block" then return {}
        elseif method == "client.inspect" then
            check(args.client_id == "source-app", "inspect only the anchor owner")
            return {client = {info = {kind = state.kind}}}
        elseif method == "client.perform" then
            check(args.client_id == "source-app" and args.action == "session.select", "never broadcast or activate the app")
            state.selection = args
            if state.refuse then return nil, "Remote Control is disabled" end
            return {performed_by = args.client_id}
        end
        error("Unexpected mutating API: " .. method)
    end
    state.api = api
    return state
end
local function fixture()
    local local_host, remote = host("local-instance", false), host("home-instance", true)
    local down = host("down-instance", false); down.offline = true
    local registry = {Home = remote.api, Alias = remote.api, Down = down.api}
    -- Server-scoped API proxies do not expose the local layout builder.
    local_host.api.layout = {block = function(spec) return {block = spec} end}
    local_host.api.servers = function() return {"Home", "Alias", "Down"} end
    local_host.api.server = function(label)
        check(registry[label] ~= nil, "only registered host labels are resolved")
        return registry[label]
    end
    local_host.api.args = {session_id = session, anchor = anchor, home_this_machine = true, home_label = "",
        path = "/tmp/中文 a;$(touch no)/`whoami`.md", line = 12, col = 3}
    return local_host, remote, down
end
local function run(f)
    rex = f.api
    local ok, result = pcall(dofile, script)
    rex = native
    return ok, result
end
local function failure(configure, message)
    local local_host, remote, down = fixture()
    configure(local_host, remote, down)
    local ok, err = run(local_host)
    check(not ok and tostring(err):find(message, 1, true), "failure is visible: " .. message)
    check(local_host.created == 0 and remote.created == 0, "failure never opens a local substitute")
end

local f, remote, down = fixture()
local original = native.json.encode(remote.view)
local ok, result = run(f)
check(ok and result.server == "Home", "the pane's actual registered host is selected")
check(result.local_path == nil, "remote routing never authorizes a local filesystem open")
check(f.created == 0 and remote.created == 1 and down.created == 0, "one new tab is created only on the origin")
check(remote.views == 2, "aliases are deduplicated by server instance before source revalidation")
check(remote.attaches == 2, "source discovery and revalidation both attach explicitly")
check(native.json.encode(remote.view) == original, "source layout, sidebar, and 80/20 ratio stay unchanged")
local options = remote.open.layout.block.options
check(options.shell == "none" and options.cwd == "/" and not remote.open.focus, "direct argv has a neutral cwd and no implicit shell")
check(options.command[1] == "/usr/bin/python3" and options.command[2] == "-B" and options.command[3] == "-c", "trusted opener code travels as argv")
check(options.command[5] == f.api.args.path and options.command[6] == "12" and options.command[7] == "3", "path and position are literal arguments")
check(remote.selection.args.window_id == "new-window-1" and remote.selection.session_id == session, "only the source app selects the new tab")
local ok_second = run(f)
check(ok_second and remote.created == 2, "each click creates a unique new tab")

local system, system_remote = fixture()
system.view.windows[1].layers[1].layout.split.before.block_id = anchor
system_remote.view.windows[1].layers[1].layout.split.before.block_id = "other-remote-pane"
local system_layout = native.json.encode(system.view)
local system_ok, system_result = run(system)
check(system_ok and system_result.server == "local" and system_result.local_path == system.api.args.path,
    "a confirmed local source returns an explicit system-open result")
check(system_result.line == 12 and system_result.col == 3, "the local result preserves position data")
check(system.created == 0 and system_remote.created == 0, "local paths never create Rex windows")
check(system.selection == nil and native.json.encode(system.view) == system_layout,
    "system opening does not select a Rex tab or change the existing layout")

system.api.args.home_this_machine, system.api.args.home_label = false, "Remote home"
local away_ok, away_result = run(system)
check(away_ok and away_result.server == "Remote home" and away_result.local_path == nil,
    "a remote default home never authorizes a local system open")
check(system.created == 1 and system_remote.created == 0,
    "a remote default home opens its matching source in a new terminal on that root server")

failure(function(_, r) r.offline = true end, "unavailable")
failure(function(_, r) r.attach_refused = true end, "unavailable")
failure(function(_, r) r.missing_session = true end, "unavailable")
failure(function(_, r) r.view.windows[1].layers[1].layout.split.before.block_id = "wrong-pane" end, "unavailable")
failure(function(_, r) r.view.session_id = "session:other" end, "unavailable")
failure(function(_, r) r.disappear = true end, "disappeared")
failure(function(_, r) r.restart = true end, "changed")
failure(function(l) l.view.windows[1].layers[1].layout.split.before.block_id = anchor end, "more than one")
failure(function(l) l.api.args.anchor = "block:invalid" end, "source session and pane")
failure(function(l) l.api.args.path = "/tmp/a\0bad" end, "Invalid absolute path")
failure(function(l) l.api.args.line = 0 end, "Invalid line or column")
failure(function(l) l.api.args.line = nil end, "column requires a line")
failure(function(l) l.api.args.home_this_machine = nil end, "reliably identify")
failure(function(l) l.api.args.home_this_machine = "true" end, "reliably identify")

local refused, refused_remote = fixture()
refused_remote.refuse = true
local accepted, err = run(refused)
check(not accepted and tostring(err):find("Remote Control is disabled", 1, true), "shared tab selection propagates refusal without changing settings")
check(refused_remote.created == 1 and refused.created == 0, "selection refusal does not retry or open locally")
local cli, cli_remote = fixture()
cli_remote.kind = "cli"
check(run(cli) and cli_remote.selection == nil, "non-app owners do not receive app actions")

-- The controller uses exactly the same helper, including rejection behavior.
local ui = dofile(base .. "/packages/coding-agent/rex-workspace/lua/ui.lua")
local selected, focus_error = ui.select_window(refused_remote.api, session, "source-app", "new-window", "new-block")
check(not selected and focus_error == "Remote Control is disabled", "shared helper preserves the actual refusal")
print("path routing checks: " .. checks)
