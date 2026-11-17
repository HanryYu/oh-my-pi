-- The URL carries source IDs, never a server address or an executable.
local dir = debug.getinfo(1, "S").source:match("^@?(.*/)") or "./"
local ui = dofile(dir .. "ui.lua")
local maximum = 2147483647

local function uuid(value, prefix)
    if type(value) ~= "string" or value:sub(1, #prefix) ~= prefix then return false end
    local id = value:sub(#prefix + 1)
    return #id == 36 and id:match("^%x%x%x%x%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%-%x%x%x%x%x%x%x%x%x%x%x%x$") ~= nil
end
local function validate(args)
    if type(args) ~= "table" or not uuid(args.session_id, "session:") or not uuid(args.anchor, "block:") then
        error("A path link must identify its source session and pane", 0)
    end
    if type(args.home_this_machine) ~= "boolean" or type(args.home_label) ~= "string" or
        #args.home_label > 256 or args.home_label:find("[%z\1-\31\127]") then
        error("This Rex version does not reliably identify the home server", 0)
    end
    if type(args.path) ~= "string" or args.path:sub(1, 1) ~= "/" or args.path:sub(1, 2) == "//" or
        #args.path > 16384 or args.path:find("[%z\1-\31\127]") then
        error("Invalid absolute path", 0)
    end
    for _, key in ipairs({"line", "col"}) do
        local n = args[key]
        if n ~= nil and (type(n) ~= "number" or n < 1 or n > maximum or n ~= math.floor(n)) then
            error("Invalid line or column", 0)
        end
    end
    if args.col and not args.line then error("A column requires a line", 0) end
end
local function has(node, anchor)
    if type(node) ~= "table" then return false end
    if node.block_id == anchor then return true end
    return node.split and (has(node.split.before, anchor) or has(node.split.after, anchor)) or false
end
local function source_window(view, anchor, session)
    view = view.view or view
    if view.session_id and view.session_id ~= session then return nil end
    for _, window in ipairs(view.windows or {}) do
        for _, layer in ipairs(window.layers or {}) do
            if has(layer.layout, anchor) then return window end
        end
    end
end
local function call(api, method, args)
    local ok, value, err = pcall(api.call, method, args)
    if not ok then return nil, value end
    return value, err
end
local function require_call(api, method, args)
    local value, err = call(api, method, args)
    if not value then error(tostring(err or method .. " returned no result"), 0) end
    return value
end

local function open(api, args)
    validate(args)
    local home_label = args.home_label ~= "" and args.home_label or (args.home_this_machine and "local" or "home")
    local candidates = {{api = api, label = home_label, local_source = args.home_this_machine == true}}
    local labels, list_err = api.servers()
    if not labels then error(tostring(list_err or "Cannot list registered Rex hosts"), 0) end
    for _, label in ipairs(labels) do
        -- rex.server resolves registered labels, not URL-supplied network addresses.
        if type(label) == "string" then
            local ok, remote = pcall(api.server, label)
            if ok and remote then candidates[#candidates + 1] = {api = remote, label = label} end
        end
    end
    local seen, selected, failures = {}, nil, {}
    for _, candidate in ipairs(candidates) do
        local status, status_err = call(candidate.api, "server.status", {})
        if status and type(status.instance_id) == "string" and status.instance_id ~= "" then
            if not seen[status.instance_id] then
                seen[status.instance_id] = true
                local attached, view_err = call(candidate.api, "session.attach", {session_id = args.session_id})
                local view
                if attached then
                    view, view_err = call(candidate.api, "session.view", {session_id = args.session_id})
                end
                if view then
                    local window = source_window(view, args.anchor, args.session_id)
                    if window then
                        if selected then error("The source exists on more than one Rex server; no path was opened", 0) end
                        candidate.instance = status.instance_id
                        selected = candidate
                    end
                elseif view_err then failures[#failures + 1] = candidate.label .. ": " .. tostring(view_err) end
            end
        else
            failures[#failures + 1] = candidate.label .. ": " .. tostring(status_err or "missing server identity")
        end
    end
    if not selected then
        error("The source session and pane are unavailable; no path was opened" ..
            (#failures > 0 and " (" .. ui.sanitize(table.concat(failures, "; "), true) .. ")" or ""), 0)
    end
    local target = selected.api
    -- Recheck after discovery: never open on a restarted/replaced server or a stale pane.
    local status = require_call(target, "server.status", {})
    if status.instance_id ~= selected.instance then error("The source Rex server changed; no path was opened", 0) end
    require_call(target, "session.attach", {session_id = args.session_id})
    local window = source_window(require_call(target, "session.view", {session_id = args.session_id}), args.anchor, args.session_id)
    if not window then error("The source pane disappeared; no path was opened", 0) end
    if selected.local_source then
        return {server = selected.label, session_id = args.session_id, local_path = args.path,
            line = args.line, col = args.col}
    end
    local file, read_err = io.open(dir .. "../path_opener.py", "rb")
    if not file then error(tostring(read_err), 0) end
    local code = file:read(128 * 1024 + 1)
    file:close()
    if not code or #code > 128 * 1024 then error("The installed path opener is invalid", 0) end
    local created = require_call(target, "session.new_window", {session_id = args.session_id,
        window_label = ui.clip(args.path:match("([^/]+)/?$") or "/", 60), focus = false,
        layout = api.layout.block {flavor = "com.superlogical.terminal.shell", label = "Path",
            options = {shell = "none", cwd = "/", exit = {on_completion = false},
                command = {"/usr/bin/python3", "-B", "-c", code, args.path,
                    args.line and tostring(args.line) or "", args.col and tostring(args.col) or ""}}}})
    local block = created.block_ids and created.block_ids[1]
    if not block or not created.window_id then error("Rex did not return the new path terminal", 0) end
    require_call(target, "session.disown_block", {session_id = args.session_id, block_ids = {block}})
    require_call(target, "session.focus_block", {session_id = args.session_id, block_id = block})
    local _, focus_err = ui.select_window(target, args.session_id, ui.anchor_owner(window, args.anchor), created.window_id, block)
    if focus_err then error("Path terminal opened, but the source app refused tab selection: " .. tostring(focus_err), 0) end
    return {server = selected.label, session_id = args.session_id, window_id = created.window_id, block_id = block}
end

return open(rex, rex.args)
