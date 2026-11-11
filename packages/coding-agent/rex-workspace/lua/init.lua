-- Loaded by the installer-managed dofile block in the user's Rex init.lua.
-- Events and layout belong to watcher.lua, never to config-load callbacks.
return function(options)
    options = options or {}
    local filename = debug.getinfo(1, "S").source:gsub("^@", "")
    local module = options.module or filename:match("^(.*)/lua/init%.lua$")
    if not module or module:sub(1, 1) ~= "/" then
        error("Rex Agent Workspace requires an absolute module path", 0)
    end
    local python = options.python or "/usr/bin/python3"
    local rex_binary = os.getenv("REX_WORKSPACE_REX") or options.rex or "rex"
    local worker = module .. "/worker.py"

    local function request(ctx, action)
        if not ctx.session_id or not ctx.block_id then
            error("No focused workspace pane", 0)
        end
        local result, err = rex.exec({
            python, worker, "request", "--session", ctx.session_id,
            "--anchor", ctx.block_id, "--view", "panel",
        }, {stdin = rex.json.encode({type = action}), timeout = 3})
        if not result or not result.ok then
            error(err or (result and result.stderr) or "Workspace request failed", 0)
        end
        return {action = action}
    end

    rex.action({
        name = "workspace_show",
        title = "Show Workspace",
        category = "OMP",
        run = function(ctx) return request(ctx, "show") end,
    })
    rex.action({
        name = "workspace_hide",
        title = "Hide Workspace",
        category = "OMP",
        run = function(ctx) return request(ctx, "hide") end,
    })

    -- The worker launches the all-session watcher under its singleton lock.
    -- A detached launcher survives the return from this configuration load.
    local handle, err = rex.exec.start({
        python, worker, "start", "--module", module, "--rex", rex_binary,
    }, {detach = true, stdout = false, stderr = false})
    if not handle then
        rex.log("warn", "Rex Agent Workspace could not start: " .. tostring(err))
    end
end
