-- Run with: rex do test/rex-workspace/test_ui.lua. No panes, workers or model calls.
local filename = debug.getinfo(1, "S").source:gsub("^@", "")
local base = filename:match("^(.*)/test/rex%-workspace/[^/]+$") or "."
local dir = base .. "/packages/coding-agent/rex-workspace/lua/"
local old_library = rex.args.library
rex.args.library = true
local ui = dofile(dir .. "ui.lua")
local Panel = dofile(dir .. "panel.lua")
local Preview = dofile(dir .. "preview.lua")
rex.args.library = old_library
local passed = 0
local function eq(actual, expected)
    assert(actual == expected, "expected " .. tostring(expected) .. ", got " .. tostring(actual))
end
local function test(name, fn)
    local ok, err = pcall(fn)
    assert(ok, name .. ": " .. tostring(err))
    passed = passed + 1
end
local function text(rows)
    local lines = {}
    for _, row in ipairs(rows) do lines[#lines + 1] = row[1] end
    return table.concat(lines, "\n")
end
local function contains(value, needle) assert(value:find(needle, 1, true), "missing " .. needle) end
local function absent(value, needle) assert(not value:find(needle, 1, true), "unexpected " .. needle) end
local function snapshot(git)
    return {v = 1, main = {state = "working", intent = "实现工作区"}, project = {host = "mac", cwd = "/project", editor = {label = "Cursor", available = true}, git = git ~= false, branch = "main", changes = {added = 4, removed = 2, files = 1}, scripts = {}, servers = {}}, agents = {}}
end
local function panel()
    local sent = {}
    local p = Panel.new(function(event) sent[#sent + 1] = event end)
    p:receive(snapshot()); p:render(36, 30)
    return p, sent
end
local function action_y(p, kind, id)
    for y, action in pairs(p.hits) do if action[1] == kind and action[2] == id then return y end end
end
local function click(p, kind, id, release_x)
    p:render(p.width, p.height)
    local y = assert(action_y(p, kind, id), "missing action " .. kind)
    p:event({"mouse", 0, 3, y, false})
    p:event({"mouse", 0, release_x or 3, y, true})
end
local function encoded(event) event.v = 1; return rex.json.encode(event) .. "\n" end

test("Unicode cells and hostile terminal text", function()
    eq(ui.width("中é"), 3)
    eq(ui.clip("中é文", 3), "中é")
    eq(table.concat(ui.wrap("中文é", 2), "|"), "中|文|é")
    eq(ui.width(ui.clip("🧑‍💻中", 3)) <= 3, true)
    local safe = ui.sanitize("正文\27]52;c;AAAA\7\r\8\194\155‮")
    for _, control in ipairs({"\27", "\7", "\r", "\8", "\194\155", "‮"}) do absent(safe, control) end
    eq(ui.clip("中", 1), "")
    for _, line in ipairs(ui.wrap("中文", 1)) do assert(ui.width(line) <= 1) end
end)
test("Fragmented UTF-8, matching mouse and Escape timeout", function()
    local keys, events = ui.keys(), {}
    local bytes = "\27[<0;4;8M中\27[<0;4;8m"
    for i = 1, #bytes do for _, event in ipairs(keys:feed(bytes:sub(i, i), 1)) do events[#events + 1] = event end end
    eq(#events, 3); eq(events[1][1], "mouse"); eq(events[1][5], false)
    eq(events[2][2], "中"); eq(events[3][5], true)
    eq(#keys:feed("\27", 2), 0); eq(keys:feed("", 2.1)[1], "escape")
    eq(keys:feed("\27[6~", 3)[1], "pagedown")
end)
test("Paste is one event and cannot submit", function()
    local keys, events = ui.keys(), {}
    for _, chunk in ipairs({"\27[20", "0~q\n中文\27[20", "1~"}) do
        for _, event in ipairs(keys:feed(chunk, 1)) do events[#events + 1] = event end
    end
    eq(#events, 1); eq(events[1][1], "paste"); eq(events[1][2], "q\n中文")
    local field = ui.input(); field:event(events[1]); eq(field:value(), "q 中文")
    field:event("home"); field:event({"text", "新"}); field:event("delete"); eq(field:value(), "新 中文")
    field:event("end"); field:event("backspace"); eq(field:value(), "新 中")
    assert(ui.width(field:display(4)) <= 4)
end)
test("Bounded JSONL framing waits for complete records", function()
    local events, warnings = {}, {}
    local framing = ui.framer(function(event) events[#events + 1] = event end, function(message) warnings[#warnings + 1] = message end, 128)
    local bytes = encoded({kind = "text", text = "正文🧑‍💻"})
    for i = 1, #bytes - 1 do framing:feed(bytes:sub(i, i)) end
    eq(#events, 0); framing:feed("\n"); eq(events[1].text, "正文🧑‍💻")
    for _ = 1, 10 do framing:feed(string.rep("x", 100)); assert(#framing.pending <= 128) end
    framing:feed('\n{broken}\n{"v":true}\n' .. encoded({kind = "text", text = "继续"}))
    eq(#events, 2); eq(events[2].text, "继续"); eq(#warnings, 3)
    framing:feed('{"v":1}'); framing:finish(); eq(#events, 2); eq(#warnings, 4)
end)
test("Empty groups and non-git controls stay absent, project actions remain reachable", function()
    local p, sent = panel(); local s = snapshot(false); s.project.editor = nil; p:receive(s); p:render(36, 30)
    for _, kind in ipairs({"diff", "commit", "select", "open-url"}) do eq(action_y(p, kind), nil) end
    eq(action_y(p, "toggle", "servers"), nil); eq(action_y(p, "toggle", "agents"), nil)
    eq(action_y(p, "open-editor"), nil)
    click(p, "open-terminal"); eq(sent[1].type, "open-terminal")
    click(p, "add-script"); eq(p.input_kind, "script-name")
end)
test("Activation requires same press and release, drag cancels", function()
    local p, sent = panel()
    click(p, "open-editor", nil, 4); eq(#sent, 0)
    click(p, "open-editor"); eq(#sent, 1); eq(sent[1].type, "open-editor")
    local y = assert(action_y(p, "diff"))
    p:event({"mouse", 0, 3, y, false}); p:event({"mouse", 32, 4, y, false}); p:event({"mouse", 0, 3, y, true})
    eq(#sent, 1)
    p:event({"mouse", 0, 3, y, false}); p:receive(snapshot(false)); p:render(36, 30); p:event({"mouse", 0, 3, y, true})
    eq(#sent, 1)
end)
test("Held clicks survive status refreshes and footer notices without shifting targets", function()
    local p, sent = panel(); local s = snapshot()
    s.agents = {{key = "Read:2", name = "ReadNativeView", status = "running"}}
    p:receive(s); p:render(36, 30)
    local y = assert(action_y(p, "select", "Read:2"))
    p:event({"mouse", 0, 3, y, false})
    s.agents[1].status, s.notice, s.noticeError = "parked", "Connection refused", true
    p:receive(s); local out = p:render(36, 30)
    eq(action_y(p, "select", "Read:2"), y)
    contains(text(out), s.notice)
    p:event({"mouse", 0, 3, y, true}); eq(#sent, 1); eq(sent[1].key, "Read:2")
end)
test("Refreshes cannot substitute another agent or activate a moved target", function()
    local p, sent = panel(); local s = snapshot()
    s.agents = {{key = "A:1", name = "First", status = "running"}}
    p:receive(s); p:render(36, 30)
    local y = assert(action_y(p, "select", "A:1"))
    p:event({"mouse", 0, 3, y, false})
    s.agents[1] = {key = "A:2", name = "First", status = "running"}
    p:receive(s); p:render(36, 30); p:event({"mouse", 0, 3, y, true}); eq(#sent, 0)
    y = assert(action_y(p, "select", "A:2"))
    p:event({"mouse", 0, 3, y, false})
    table.insert(s.agents, 1, {key = "B:1", name = "Second", status = "running"})
    p:receive(s); p:render(36, 30); p:event({"mouse", 0, 3, y, true}); eq(#sent, 0)
    click(p, "select", "A:2"); eq(sent[1].key, "A:2")
end)
test("Collapsed groups support keyboard expansion, activation and return to parent", function()
    local p, sent = panel()
    click(p, "toggle", "scripts")
    p:event("left"); p:render(36, 30)
    eq(p.focus[2], "scripts"); eq(action_y(p, "add-script"), nil)
    p:event("right"); p:event("down"); p:event("enter"); eq(p.input_kind, "script-name")
    p:event("escape"); p:event("left"); p:render(36, 30); eq(p.focus[2], "scripts")
    eq(action_y(p, "add-script"), nil)
    p:event("enter"); p:render(36, 30); assert(action_y(p, "add-script"))
end)
test("Forms send only complete explicit submissions", function()
    local p, sent = panel()
    click(p, "add-script"); p:event({"text", "dev"}); p:event("enter"); eq(#sent, 0)
    p:event({"paste", "bun run dev\n"}); eq(#sent, 0)
    p:event("escape"); eq(#sent, 0)
    click(p, "add-script"); p:event({"paste", "开发"}); p:event("enter"); p:event({"paste", "bun run dev\n"}); p:event("enter")
    eq(sent[1].type, "add-script"); eq(sent[1].name, "开发"); eq(sent[1].command, "bun run dev")
    click(p, "commit"); p:event("enter"); eq(#sent, 1); eq(p.notice_error, true)
    p:event({"paste", "Implement workspace"}); p:event("enter"); eq(sent[2].message, "Implement workspace")
    eq(p.input_kind, nil)
end)
test("Real project ids and registry generation are routed", function()
    local p, sent = panel(); local s = snapshot()
    s.project.scripts = {{id = "package:dev", label = "dev", command = "bun run dev"}}
    s.project.servers = {{id = "123:3000", label = "app", port = 3000}}
    s.agents = {{key = "A:2", name = "Worker", status = "parked"}}
    p:receive(s)
    click(p, "run-script", "package:dev"); eq(#sent, 0); p:event("enter")
    click(p, "open-url", "123:3000"); click(p, "select", "A:2"); click(p, "diff")
    eq(sent[1].id, "package:dev"); eq(sent[2].id, "123:3000"); eq(sent[3].key, "A:2"); eq(sent[4].type, "diff")
end)
test("Long agent list is keyboard reachable and q hides only UI", function()
    local p, sent = panel(); local s = snapshot()
    for i = 1, 40 do s.agents[i] = {key = "worker:" .. i, name = "Worker", status = "running"} end
    p:receive(s); p:render(30, 8)
    for _ = 1, #p.rows do if p.focus[2] ~= "worker:40" then p:event("down") end end
    p:render(30, 8); assert(p.offset > 0)
    p:event("enter"); eq(sent[1].key, "worker:40")
    eq(p:event({"text", "q"}), false); eq(sent[2].type, "hide")
end)
test("Keyboard focus and scrolled targets keep their identities across list updates", function()
    local p = panel(); local s = snapshot()
    for i = 1, 40 do s.agents[i] = {key = "worker:" .. i, name = "Worker" .. i, status = "running"} end
    p:receive(s); p:render(30, 8)
    for _ = 1, #p.rows do if p.focus[2] ~= "worker:39" then p:event("down") end end
    p:render(30, 8)
    local key, y = p.focus[2], assert(action_y(p, "select", p.focus[2]))
    table.insert(s.agents, 1, {key = "new:1", name = "New", status = "running"})
    s.agents[20].status = "parked"
    p:receive(s); p:render(30, 8)
    eq(p.focus[2], key); eq(action_y(p, "select", key), y)
    p:event("left"); p:render(30, 8)
    eq(p.focus[2], "agents"); assert(action_y(p, "toggle", "agents"))
    eq(action_y(p, "select", key), nil)
end)
test("Commands require confirmation and stale previews cannot execute", function()
    local p, sent = panel(); local s = snapshot()
    s.project.scripts = {{id = "dev", label = "dev", command = "bun run dev", cwd = "/project"}}
    p:receive(s); click(p, "run-script", "dev")
    eq(#sent, 0); contains(text(p:render(36, 30)), "bun run dev")
    p:event("escape"); eq(#sent, 0); eq(p.focus[2], "dev")
    click(p, "run-script", "dev")
    s.project.scripts[1].command = "bun run different"; p:receive(s); p:event("enter")
    eq(#sent, 0); eq(p.notice_error, true)
    click(p, "run-script", "dev"); p:event("enter"); eq(sent[1].id, "dev")
end)
test("Running commands expose actual state but cannot be launched twice", function()
    local p, sent = panel(); local s = snapshot()
    s.project.scripts = {{id = "dev", label = "dev", command = "bun run dev", state = "running", pid = 42, logFile = "/logs/dev.log"}}
    p:receive(s); click(p, "run-script", "dev"); p:event("enter")
    eq(#sent, 0); contains(text(p:render(36, 30)), "PID 42"); contains(text(p:render(36, 30)), "/logs/dev.log")
    p:event("escape"); s.project.scripts[1].state = "done"; p:receive(s)
    click(p, "run-script", "dev"); p:event("enter"); eq(sent[1].id, "dev")
end)
test("Preferred workflows stay visible and other scripts remain reachable", function()
    local p, sent = panel(); local s = snapshot()
    for i = 1, 12 do s.project.scripts[#s.project.scripts + 1] = {id = "task-" .. i, label = "task-" .. i, kind = "task", command = "echo task"} end
    for _, kind in ipairs({"dev", "build", "test", "check"}) do s.project.scripts[#s.project.scripts + 1] = {id = kind, label = kind, kind = kind, command = "echo " .. kind} end
    p:receive(s); p:render(36, 40)
    for _, kind in ipairs({"dev", "build", "test", "check"}) do assert(action_y(p, "run-script", kind)) end
    eq(action_y(p, "run-script", "task-12"), nil)
    click(p, "toggle", "more-scripts"); click(p, "run-script", "task-12")
    eq(#sent, 0); p:event("enter"); eq(sent[1].id, "task-12")
end)
test("Long command previews are scrollable in a narrow sidebar", function()
    local p, sent = panel(); local s = snapshot()
    s.project.scripts = {{id = "long", label = "long", command = string.rep("a", 200) .. "LAST_ARGUMENT"}}
    p:receive(s); p:render(18, 8); p.focus = {"run-script", "long"}; p:move(0)
    p:activate({"run-script", "long"}); p:render(18, 8)
    for _ = 1, 5 do p:event("pagedown"); p:render(18, 8) end
    contains(text(p:render(18, 8)), "LAST_ARGUMENT"); eq(#sent, 0)
    p:event("escape"); eq(#sent, 0)
end)
test("Diff preview scrolls safely and requests its own close", function()
    local sent = {}; local preview = Preview.new(function(event) sent[#sent + 1] = event end)
    preview:receive({title = "Changes", text = string.rep("+中文\n", 30) .. "\27]52;c;AAAA\7", diff = true})
    local before = text(preview:render(8, 5)); preview:event("pagedown"); assert(preview.scroll > 0)
    preview:event("end"); local after = text(preview:render(8, 5)); assert(before ~= after); absent(after, "\27")
    eq(preview:event({"text", "q"}), false); eq(sent[1].type, "close-preview")
end)
test("Large diff text preserves content without overflowing the Lua stack", function()
    local long = string.rep("a", 20000) .. "\27]52;c;bad\7\n尾部"
    local safe = ui.sanitize(long)
    contains(safe, string.rep("a", 20000))
    contains(safe, "尾部")
    absent(safe, "\27")
    local preview = Preview.new(function() end)
    preview:receive({title = "Changes", text = long, diff = true})
    preview:render(79, 24)
    preview:event("end")
    contains(text(preview:render(79, 24)), "尾部")
end)
test("TTY lifecycle preserves saved state and read-only terminals never enable mouse", function()
    local old_exec, old_write, old_flush = rex.exec, io.write, io.flush
    local captured, calls, handle = {}, {}, {live = true}
    local exec = setmetatable({}, {__call = function(_, argv)
        calls[#calls + 1] = argv
        return {ok = true, stdout = argv[3]:find("tty=", 1, true) and "ttys123 \nsaved-state\n" or argv[3] == "stty size </dev/tty" and "24 80\n" or ""}
    end})
    function exec.start(_, options)
        return handle
    end
    function handle:close_stdin() self.closed = true; self.live = false end
    function handle:wait() return {ok = true} end
    function handle:running() return self.live end
    function handle:kill() self.live = false end
    rex.exec, io.write, io.flush = exec, function(value) captured[#captured + 1] = value end, function() end
    local screen = ui.terminal(false)
    local ok, err = pcall(function()
        screen:open(function() end, function() end)
        local width, height = screen:size(); eq(width, 79); eq(height, 24)
        screen:draw({{"unsafe\27]52;c;AAAA\7", "accent"}})
        screen:close(); screen:close()
    end)
    rex.exec, io.write, io.flush = old_exec, old_write, old_flush
    assert(ok, err)
    local out = table.concat(captured)
    absent(out, "\27[?1000h"); absent(out, "\27[?1002h"); absent(out, "\27[?1006h"); absent(out, "\27]52")
    contains(out, "\27[?25h"); contains(out, "\27[?1049l"); eq(handle.closed, true)
end)
return {passed = passed}
