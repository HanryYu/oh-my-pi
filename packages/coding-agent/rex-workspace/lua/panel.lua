local dir = debug.getinfo(1, "S").source:match("^@?(.*/)") or "./"
local ui = dofile(dir .. "ui.lua")
local M = {}
local function equal(a, b) return a and b and a[1] == b[1] and a[2] == b[2] end
local function row(text, style, action, key)
    return {text or "", style or "", action, key}
end
local function fit(text, width)
    text = ui.sanitize(text, true)
    if ui.width(text) <= width then return text end
    return width > 0 and ui.clip(text, width - 1) .. "…" or ""
end
local statuses = {working = "running", idle = "done", completed = "done", failed = "failed", awaiting_input = "waiting"}
function M.new(send)
    local self = {
        send = send, snapshot = {}, offset = 0, rows = {}, hits = {}, height = 24, width = 79,
        notice = "", input = ui.input(), collapsed = {["more-scripts"] = true}, keyboard = false,
    }
    function self:receive(snapshot)
        self.snapshot = snapshot
        local notice, is_error = ui.field(snapshot, "notice", "", 1000), snapshot.noticeError == true
        if notice ~= self.remote_notice or is_error ~= self.remote_error then
            if notice ~= "" or not self.input_kind then self.notice, self.notice_error = notice, is_error end
            self.remote_notice, self.remote_error = notice, is_error
        end
        -- Keep a held click across status-only updates. Render validates its
        -- identity at the original screen coordinates before it can activate.
    end
    function self:script(id)
        for _, script in ipairs((self.snapshot.project or {}).scripts or {}) do
            if script.id == id then return script end
        end
    end
    function self:project_rows()
        local s, rows = self.snapshot, {}
        local project, main = s.project or {}, s.main or {}
        local function add(text, style, action, key, detail)
            local item = row(text, style, action, key)
            item.detail = detail
            rows[#rows + 1] = item
            return item
        end
        local function group(id, label, count)
            if #rows > 0 then add("", "", nil, "gap:" .. id) end
            local item = add(label .. (count and " · " .. count or ""), "heading", {"toggle", id}, "group:" .. id)
            item.group = id
            return not self.collapsed[id]
        end
        if self.input_kind == "run-script" and self.pending_script then
            local pending = self.pending_script
            add(ui.field(pending, "label", pending.id), "heading", nil, "run:title")
            local current = self:script(pending.id)
            if current and current.state then
                add(ui.field(current, "state", "") .. (current.pid and " · PID " .. tostring(current.pid) or ""),
                    current.state == "failed" and "error" or "muted", nil, "run:state")
            end
            for _, field in ipairs({"cwd", "source", "command"}) do
                local value = ui.field(pending, field, "", 1048576)
                if value ~= "" then
                    add("", "", nil, "run:gap:" .. field)
                    for i, line in ipairs(ui.wrap(value, math.max(1, self.width))) do
                        add(line, field == "command" and "" or "muted", nil, "run:" .. field .. ":" .. i)
                    end
                end
            end
            if current and current.logFile then
                add("", "", nil, "run:gap:log")
                for i, line in ipairs(ui.wrap(ui.field(current, "logFile", "", 4096), math.max(1, self.width))) do
                    add(line, "muted", nil, "run:log:" .. i)
                end
            end
            return rows
        end
        if not next(s) then add("连接工作区…", "muted", nil, "connecting"); return rows end
        local cwd = ui.field(project, "cwd", ui.field(main, "cwd", ""), 4096):gsub("/+$", "")
        add(cwd:match("([^/]+)$") or (cwd == "" and "/" or cwd), "heading", nil, "project")
        local editor = project.editor
        if type(editor) == "table" and editor.available == true then
            add("Open in " .. ui.field(editor, "label", "editor"), "accent", {"open-editor"}, "editor")
        end
        add("Terminal", "accent", {"open-terminal"}, "terminal")
        if project.git == true then
            add("", "", nil, "gap:git")
            local changes = project.changes or {}
            add("⎇ " .. ui.field(project, "branch", ui.field(project, "head", "No commits")), "muted", nil, "branch")
            local files = tonumber(changes.files) or 0
            local summary = "Changes · " .. files .. (files == 1 and " file" or " files")
            local detail = string.format("+%s -%s", changes.added or 0, changes.removed or 0)
            if changes.incomplete then summary, detail = "Changes", "incomplete"
            elseif changes.headExists == false then summary = "Changes · empty tree" end
            add(summary, "", {"diff"}, "changes", detail)
            add("Commit staged", "accent", {"commit"}, "commit", changes.staged and tostring(changes.staged) or nil)
        end
        local agents = {}
        for _, agent in ipairs(s.agents or {}) do
            if type(agent) == "table" and type(agent.key) == "string" then agents[#agents + 1] = agent end
        end
        if #agents > 0 and group("agents", "Subagents", #agents) then
            for _, agent in ipairs(agents) do
                local status = ui.field(agent, "status", "unknown", 64)
                local item = add(ui.field(agent, "name", ui.field(agent, "id", agent.key)),
                    (status == "failed" or status == "error") and "error" or "", {"select", agent.key}, "agent:" .. agent.key, statuses[status] or status)
                item.selected = agent.key == s.selected
                if item.selected then item[2] = "accent" end
            end
        end
        local scripts, preferred, used = {}, {}, {}
        for _, script in ipairs(project.scripts or {}) do
            if type(script) == "table" and type(script.id) == "string" then scripts[#scripts + 1] = script end
        end
        local function take(script)
            if #preferred < 4 and not used[script.id] then preferred[#preferred + 1], used[script.id] = script, true end
        end
        for _, script in ipairs(scripts) do if script.state == "running" then take(script) end end
        for _, script in ipairs(scripts) do if script.id:match("^user%-") then take(script) end end
        for _, kind in ipairs({"dev", "build", "test", "check"}) do
            for _, script in ipairs(scripts) do if script.kind == kind and not used[script.id] then take(script); break end end
        end
        for _, script in ipairs(scripts) do take(script) end
        local function script_row(script)
            local label = ui.field(script, "label", script.id)
            add((script.state == "running" and "" or "Run ") .. label,
                script.state == "failed" and "error" or "", {"run-script", script.id}, "script:" .. script.id,
                script.state and ui.field(script, "state", "") or nil)
        end
        if group("scripts", "Scripts", #scripts > 0 and #scripts or nil) then
            for _, script in ipairs(preferred) do script_row(script) end
            if #scripts > #preferred and group("more-scripts", "More scripts", #scripts - #preferred) then
                for _, script in ipairs(scripts) do if not used[script.id] then script_row(script) end end
            end
            add("Add script", "accent", {"add-script"}, "add-script")
        end
        local servers = {}
        for _, server in ipairs(project.servers or {}) do
            if type(server) == "table" and type(server.id) == "string" then servers[#servers + 1] = server end
        end
        if #servers > 0 and group("servers", "Servers", #servers) then
            for _, server in ipairs(servers) do
                add(ui.field(server, "label", ui.field(server, "url", server.id)), "accent",
                    {"open-url", server.id}, "server:" .. server.id, server.port and ":" .. tostring(server.port) or nil)
            end
        end
        if #(s.diagnostics or {}) > 0 then
            add("Unlinked agents", "error", {"diagnostics"}, "diagnostics", tostring(#s.diagnostics))
        end
        if type(project.error) == "string" and project.error ~= "" then
            add("", "", nil, "gap:error")
            for i, line in ipairs(ui.wrap("! " .. ui.sanitize(project.error, true), math.max(1, self.width - 2))) do
                add("  " .. line, "error", nil, "error:" .. i)
            end
        end
        return rows
    end
    function self:render(width, height)
        local anchor = self.rows[self.offset + 1]
        local old_focus_index
        for i, item in ipairs(self.rows) do if equal(item[3], self.focus) then old_focus_index = i; break end end
        self.width, self.height, self.hits = math.max(1, width), math.max(1, height), {}
        self.rows = self:project_rows()
        local valid, nearest, distance = false, nil, math.huge
        for i, item in ipairs(self.rows) do
            if anchor and item[4] == anchor[4] then self.offset = i - 1 end
            if item[3] then
                if equal(item[3], self.focus) then valid = true end
                local delta = math.abs(i - (old_focus_index or 1))
                if delta < distance then nearest, distance = item[3], delta end
            end
        end
        if not valid then self.focus = nearest end
        -- A permanent footer slot prevents asynchronous notices from moving hits.
        local available = math.max(0, self.height - 1 - (self.input_kind and 1 or 0))
        self.available = available
        self.offset = math.max(0, math.min(self.offset, #self.rows - available))
        local out = {}
        for i = self.offset + 1, math.min(#self.rows, self.offset + available) do
            local item = self.rows[i]
            local text, style, action = item[1], item[2], item[3]
            if action then
                local focused = self.keyboard and equal(action, self.focus) and not self.input_kind
                local held = self.press and equal(action, self.press.action) and self.press.y == #out + 1
                local marker = item.group and (self.collapsed[item.group] and "▸ " or "▾ ")
                    or focused and "› " or item.selected and "● " or "  "
                local room = math.max(0, self.width - ui.width(marker))
                if item.detail then
                    local detail = fit(item.detail, math.max(0, room - 2))
                    text = fit(text, math.max(0, room - ui.width(detail) - 1))
                    text = text .. string.rep(" ", math.max(1, room - ui.width(text) - ui.width(detail))) .. detail
                else text = fit(text, room) end
                text = marker .. text
                self.hits[#out + 1] = action
                if focused or held then style = "selected" end
            end
            out[#out + 1] = row(fit(text, self.width), style)
        end
        if self.press and not equal(self.hits[self.press.y], self.press.action) then self.press = nil end
        while #out < self.height - (self.input_kind and 1 or 0) - 1 do out[#out + 1] = row("") end
        local footer = self.notice
        if footer == "" and not self.input_kind then
            local script = self.focus and self.focus[1] == "run-script" and self:script(self.focus[2])
            local main = self.snapshot.main or {}
            footer = script and ui.field(script, "command", "", 4096)
                or ui.field(main, "intent", ui.field(main, "state", ""), 512)
        end
        out[#out + 1] = row(fit((self.notice_error and footer ~= "" and "! " or "") .. footer, self.width), self.notice_error and "error" or "muted")
        if self.input_kind == "run-script" and #out < self.height then
            local current = self.pending_script and self:script(self.pending_script.id)
            out[#out + 1] = row(fit(current and current.state == "running" and "Esc Back" or "Enter Run · Esc Cancel", self.width), "accent")
        elseif self.input_kind and #out < self.height then
            local labels = {["script-name"] = "Name", ["script-command"] = "Command", commit = "Commit staged"}
            local label = fit(labels[self.input_kind], math.max(0, self.width - 4)) .. " › "
            out[#out + 1] = row(fit(label .. self.input:display(math.max(1, self.width - ui.width(label))), self.width), "accent")
        end
        return out
    end
    function self:activate(action)
        if not action then return end
        local exists = false
        for _, item in ipairs(self:project_rows()) do if equal(item[3], action) then exists = true; break end end
        if not exists then return end
        self.notice, self.notice_error, self.press = "", false, nil
        local kind = action[1]
        if kind == "toggle" then
            self.collapsed[action[2]] = not self.collapsed[action[2]]
            self.focus = action
            self:move(0)
        elseif kind == "diagnostics" then
            self.notice, self.notice_error = "Use rex-omp to include eval agents", true
        elseif kind == "run-script" then
            local script = self:script(action[2])
            if not script then return end
            self.pending_script = {id = script.id, label = script.label, command = script.command,
                cwd = script.cwd, source = script.source, offset = self.offset}
            self.input_kind, self.offset, self.focus, self.press = "run-script", 0, nil, nil
        elseif kind == "add-script" or kind == "commit" then
            self.input_kind = kind == "add-script" and "script-name" or "commit"
            self.input, self.script_name = ui.input(), ""
        else
            local event = {type = kind}
            if action[2] then event[kind == "select" and "key" or "id"] = action[2] end
            self.send(event)
        end
    end
    function self:submit()
        if self.input_kind == "run-script" then
            local pending = self.pending_script
            local current = pending and self:script(pending.id)
            if current and current.state == "running" then return end
            self.input_kind, self.pending_script, self.offset = nil, nil, 0
            self.focus = pending and {"run-script", pending.id} or nil
            self.offset = pending and pending.offset or 0
            if not current or current.command ~= pending.command then
                self.notice, self.notice_error = "Command changed; select it again", true
                return
            end
            self.send({type = "run-script", id = pending.id})
            return
        end
        local value = self.input:value():match("^%s*(.-)%s*$")
        if value == "" then
            self.notice = self.input_kind == "script-name" and "输入名称" or self.input_kind == "script-command" and "输入命令" or "输入提交信息"
            self.notice_error = true
            return
        end
        if self.input_kind == "script-name" then
            self.script_name, self.input_kind, self.input, self.notice, self.notice_error = value, "script-command", ui.input(), "", false
            return
        end
        local event = self.input_kind == "script-command" and {type = "add-script", name = self.script_name, command = value} or {type = "commit", message = value}
        self.input_kind, self.input, self.script_name, self.notice, self.notice_error = nil, ui.input(), "", "", false
        self.send(event)
    end
    function self:move(amount)
        local actions, current = {}, 1
        for i, item in ipairs(self.rows) do
            if item[3] then actions[#actions + 1] = i; if equal(item[3], self.focus) then current = #actions end end
        end
        if #actions == 0 then return end
        local index = actions[math.max(1, math.min(#actions, current + amount))]
        self.focus = self.rows[index][3]
        local available = math.max(1, self.available or self.height)
        if index - 1 < self.offset then self.offset = index - 1
        elseif index > self.offset + available then self.offset = index - available end
    end
    function self:event(event)
        if event == "quit" then self.send({type = "hide"}); return false end
        if self.input_kind then
            if event == "escape" then
                local pending = self.pending_script
                self.input_kind, self.pending_script, self.input, self.script_name, self.notice, self.notice_error, self.press = nil, nil, ui.input(), "", "", false, nil
                if pending then self.offset, self.focus = pending.offset or 0, {"run-script", pending.id} end
            elseif event == "enter" then self:submit()
            elseif self.input_kind == "run-script" then
                if event == "up" then self.offset = math.max(0, self.offset - 1)
                elseif event == "down" then self.offset = self.offset + 1
                elseif event == "pageup" then self.offset = math.max(0, self.offset - math.max(1, self.available or 1))
                elseif event == "pagedown" then self.offset = self.offset + math.max(1, self.available or 1)
                elseif type(event) == "table" and event[1] == "mouse" and event[2] >= 64 and event[2] < 128 then
                    self.offset = math.max(0, self.offset + (event[2] % 2 == 1 and 3 or -3))
                end
            else self.input:event(event, self.input_kind == "script-name" and 160 or 4096) end
            return true
        end
        local key = type(event) == "table" and event[1] == "text" and event[2] or event
        if type(key) == "string" then
            self.keyboard, self.press = true, nil
            self:render(self.width, self.height)
        end
        if key == "q" then self.send({type = "hide"}); return false
        elseif key == "up" or key == "k" or key == "backtab" then self:move(-1)
        elseif key == "down" or key == "j" or key == "tab" then self:move(1)
        elseif key == "pageup" then self:move(-math.max(1, self.available or self.height - 2))
        elseif key == "pagedown" then self:move(math.max(1, self.available or self.height - 2))
        elseif key == "home" then self:move(-#self.rows)
        elseif key == "end" then self:move(#self.rows)
        elseif key == "enter" or key == " " then self:activate(self.focus)
        elseif key == "left" or key == "right" then
            if self.focus and self.focus[1] == "toggle" then
                self.collapsed[self.focus[2]] = key == "left"
            elseif key == "left" then
                for i = #self.rows, 1, -1 do
                    local item = self.rows[i]
                    if equal(item[3], self.focus) then
                        for j = i - 1, 1, -1 do
                            if self.rows[j].group then self.focus = self.rows[j][3]; self.collapsed[self.focus[2]] = true; break end
                        end
                        break
                    end
                end
            end
            self:move(0)
        elseif type(event) == "table" and event[1] == "mouse" then
            local button, x, y, released = event[2], event[3], event[4], event[5]
            self.keyboard = false
            if button >= 64 and button < 128 then
                self.press = nil
                self.offset = math.max(0, self.offset + (button % 2 == 1 and 3 or -3))
            elseif math.floor(button / 32) % 2 == 1 then self.press = nil
            elseif button % 4 == 0 then
                local action = x >= 1 and x <= self.width and self.hits[y] or nil
                if released then
                    local press = self.press; self.press = nil
                    if press and press.x == x and press.y == y and equal(press.action, action) then self.focus = action; self:activate(action) end
                else self.press = action and {x = x, y = y, action = action} or nil end
            else self.press = nil end
        end
        return true
    end
    return self
end
if not (rex.args and rex.args.library) then
    local root, module = assert(rex.args.root, "root is required"), assert(rex.args.module, "module is required")
    local panel
    panel = M.new(function(event)
        local ok, err = ui.request(module, root, "panel", event)
        if not ok then panel.notice, panel.notice_error = ui.sanitize(err, true), true end
    end)
    ui.run(panel, {root = root, mouse = true})
end
if rex.args and rex.args.library then return M end
