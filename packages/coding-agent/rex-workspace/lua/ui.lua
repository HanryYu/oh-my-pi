-- Terminal primitives shared by the three native rex do views.
local dir = debug.getinfo(1, "S").source:match("^@?(.*/)") or "./"
local unicode = dofile(dir .. "unicode.lua")
local M = {unicode = unicode, MAX_LINE = 1024 * 1024}
local colors = {muted = "\27[90m", accent = "\27[36m", selected = "\27[1;36m", error = "\27[31m", good = "\27[32m", heading = "\27[1m"}

-- Rex's Lua VM has a bounded value stack. table.concat otherwise pushes every
-- element at once, so a normal large diff can overflow that stack.
function M.concat(parts, first, last)
    first, last = first or 1, last or #parts
    if last - first < 256 then return table.concat(parts, "", first, last) end
    local chunks = {}
    for at = first, last, 256 do
        chunks[#chunks + 1] = table.concat(parts, "", at, math.min(at + 255, last))
    end
    return M.concat(chunks)
end

function M.sanitize(value, single)
    if type(value) ~= "string" then return "" end
    local out, at = {}, 1
    while at <= #value do
        local n, size, char = unicode.next(value, at, true)
        if n == 10 or n == 9 or n == 0x2028 or n == 0x2029 then
            out[#out + 1] = single and " " or n == 9 and "    " or "\n"
        elseif n < 32 or n >= 127 and n <= 159 or n >= 0x202a and n <= 0x202e
            or n >= 0x2060 and n <= 0x206f or n == 0x061c or n == 0x200e or n == 0x200f
            or n == 0xfeff or n == 0x00ad or n == 0x200b then
            out[#out + 1] = string.format(n < 256 and "\\x%02x" or "\\u%04x", n)
        else out[#out + 1] = char end
        at = at + size
    end
    return M.concat(out)
end
function M.width(text)
    local used, at = 0, 1
    while at <= #text do
        local n, size = unicode.next(text, at, true)
        used, at = used + unicode.width(n), at + size
    end
    return used
end
function M.clip(text, width)
    local out, used, at = {}, 0, 1
    while at <= #text do
        local n, size, char = unicode.next(text, at, true)
        local cells = unicode.width(n)
        if used + cells > math.max(0, width) then break end
        out[#out + 1], used, at = char, used + cells, at + size
    end
    return M.concat(out)
end
function M.wrap(text, width)
    width = math.max(1, width)
    text = M.sanitize(text)
    local result, part, used, at = {}, {}, 0, 1
    while at <= #text do
        local n, size, char = unicode.next(text, at, true)
        local cells = unicode.width(n)
        if n == 10 then
            result[#result + 1], part, used = M.concat(part), {}, 0
        else
            if used + cells > width and #part > 0 then
                result[#result + 1], part, used = M.concat(part), {}, 0
            end
            -- A wide glyph cannot fit a one-cell viewport; never force wrapping.
            if cells <= width then part[#part + 1], used = char, used + cells end
        end
        at = at + size
    end
    result[#result + 1] = M.concat(part)
    return result
end
function M.field(object, key, fallback, limit)
    local value = type(object) == "table" and object[key]
    if type(value) ~= "string" or value == "" then return fallback or "" end
    return M.sanitize(value:sub(1, limit or 256), true)
end

function M.framer(on_event, on_notice, limit)
    local self = {pending = "", discarding = false, warned = {}, limit = limit or M.MAX_LINE}
    function self:warn(key, text)
        if not self.warned[key] then self.warned[key] = true; on_notice(text) end
    end
    function self:feed(data)
        local at = 1
        while at <= #data do
            local stop = data:find("\n", at, true)
            local last = stop and stop - 1 or #data
            if not self.discarding then
                if #self.pending + last - at + 1 > self.limit then
                    self.pending, self.discarding = "", true
                    self:warn("size", "已跳过超长数据行（上限 1 MiB）")
                else self.pending = self.pending .. data:sub(at, last) end
            end
            if not stop then break end
            if not self.discarding and self.pending:match("%S") then
                local ok, event = pcall(rex.json.decode, self.pending)
                if not ok or type(event) ~= "table" then self:warn("invalid", "已跳过损坏的数据行")
                elseif event.v ~= 1 then self:warn("version", "已跳过不支持的数据记录")
                else on_event(event) end
            end
            self.pending, self.discarding, at = "", false, stop + 1
        end
    end
    function self:finish()
        if self.pending ~= "" or self.discarding then self:warn("partial", "末尾数据尚未写完") end
        self.pending, self.discarding = "", false
    end
    return self
end

local codes = {A = "up", B = "down", C = "right", D = "left", H = "home", F = "end", ["1~"] = "home", ["4~"] = "end", ["7~"] = "home", ["8~"] = "end", ["5~"] = "pageup", ["6~"] = "pagedown", ["3~"] = "delete", Z = "backtab"}
function M.keys()
    local self = {pending = "", paste = "", pasting = false}
    function self:feed(data, now)
        self.pending = self.pending .. (data or "")
        now = now or os.clock()
        local events = {}
        while #self.pending > 0 do
            local text = self.pending
            if self.pasting then
                local stop = text:find("\27[201~", 1, true)
                if not stop then
                    local count = math.max(0, #text - 5)
                    self.paste = (self.paste .. text:sub(1, count)):sub(1, 16384)
                    self.pending = text:sub(count + 1)
                    break
                end
                self.paste = (self.paste .. text:sub(1, stop - 1)):sub(1, 16384)
                events[#events + 1] = {"paste", self.paste}
                self.pending, self.pasting, self.paste = text:sub(stop + 6), false, ""
            elseif text:sub(1, 6) == "\27[200~" then
                self.pending, self.pasting, self.escape_at = text:sub(7), true, nil
            elseif text:sub(1, 1) == "\27" then
                local button, x, y, release, stop = text:match("^\27%[<(%d+);(%d+);(%d+)([Mm])()")
                local code, ending = text:match("^\27[%[O]([0-9;?]*[ -/]*[@-~])()")
                if button then
                    events[#events + 1] = {"mouse", tonumber(button), tonumber(x), tonumber(y), release == "m"}
                    self.pending, self.escape_at = text:sub(stop), nil
                elseif code and text:sub(1, 3) ~= "\27[<" then
                    if codes[code] then events[#events + 1] = codes[code] end
                    self.pending, self.escape_at = text:sub(ending), nil
                else
                    self.escape_at = self.escape_at or now
                    if now - self.escape_at < 0.06 and #text < 128 then break end
                    events[#events + 1] = "escape"
                    self.pending = (text:sub(1, 2) == "\27[" or text:sub(1, 2) == "\27O") and "" or text:sub(2)
                    self.escape_at = nil
                end
            else
                local n, size, char = unicode.next(text, 1, false)
                if not n then break end
                self.pending, self.escape_at = text:sub(size + 1), nil
                if n == 10 or n == 13 then events[#events + 1] = "enter"
                elseif n == 127 or n == 8 then events[#events + 1] = "backspace"
                elseif n == 3 or n == 4 then events[#events + 1] = "quit"
                elseif n == 21 then events[#events + 1] = "clear"
                elseif n == 9 then events[#events + 1] = "tab"
                elseif n >= 32 and not (n >= 127 and n <= 159) then events[#events + 1] = {"text", char} end
            end
        end
        return events
    end
    return self
end
function M.input()
    local self = {chars = {}, cursor = 0}
    function self:value() return M.concat(self.chars) end
    function self:event(event, limit)
        if type(event) == "table" and (event[1] == "text" or event[1] == "paste") then
            local text = event[2]:gsub("[\r\n\t]", " ")
            local chars = unicode.chars(M.sanitize(text, true))
            for _, char in ipairs(chars) do
                if #self.chars >= (limit or 4096) then break end
                table.insert(self.chars, self.cursor + 1, char)
                self.cursor = self.cursor + 1
            end
        elseif event == "backspace" and self.cursor > 0 then table.remove(self.chars, self.cursor); self.cursor = self.cursor - 1
        elseif event == "delete" then table.remove(self.chars, self.cursor + 1)
        elseif event == "left" then self.cursor = math.max(0, self.cursor - 1)
        elseif event == "right" then self.cursor = math.min(#self.chars, self.cursor + 1)
        elseif event == "home" then self.cursor = 0
        elseif event == "end" then self.cursor = #self.chars
        elseif event == "clear" then self.chars, self.cursor = {}, 0 end
    end
    function self:display(width)
        local before, after = {}, {}
        for i, char in ipairs(self.chars) do
            local target = i <= self.cursor and before or after
            target[#target + 1] = char
        end
        local used, start = M.width(M.concat(before)), 1
        while start <= #before and used >= math.max(1, width) do
            used = used - M.width(before[start]); start = start + 1
        end
        return M.clip(M.concat(before, start) .. "▏" .. M.concat(after), width)
    end
    return self
end
function M.scroll(self, event, count)
    local maximum = math.max(0, count - self.view_height)
    local current = self.scroll == nil and maximum or self.scroll
    local key = type(event) == "table" and event[1] == "text" and event[2] or event
    if key == "up" or key == "k" then self.scroll = math.max(0, current - 1)
    elseif key == "down" or key == "j" then
        if current + 1 >= maximum then self.scroll = nil else self.scroll = current + 1 end
    elseif key == "pageup" then self.scroll = math.max(0, current - math.max(1, self.view_height - 1))
    elseif key == "pagedown" then
        local target = current + math.max(1, self.view_height - 1)
        if target >= maximum then self.scroll = nil else self.scroll = target end
    elseif key == "home" then self.scroll = 0
    elseif key == "end" or key == "G" then self.scroll = nil end
end
function M.read_json(path, limit)
    local file = io.open(path, "rb")
    if not file then return nil end
    local text = file:read((limit or M.MAX_LINE) + 1)
    file:close()
    if not text or #text > (limit or M.MAX_LINE) then return nil end
    local ok, value = pcall(rex.json.decode, text)
    if ok and type(value) == "table" then return value, text end
end
function M.request(module, root, view, request)
    local result, err = rex.exec({"/usr/bin/python3", module .. "/worker.py", "request", "--root", root, "--view", view}, {stdin = rex.json.encode(request), timeout = 3})
    if not result or not result.ok then return nil, err or result and result.stderr or "请求失败" end
    return true
end

-- A tiny shell lifecycle guard restores the TTY even if the Lua host is killed.
-- Rendering, input parsing, selection and all semantic actions remain Lua-owned.
local restore = "\27[?1000l\27[?1002l\27[?1006l\27[?2004l\27[0m\27[?25h\27[?1049l"
local guard = 'saved=$1; device=$2; reader=; restore() { if [ -n "$reader" ]; then kill "$reader" 2>/dev/null; wait "$reader" 2>/dev/null; fi; stty "$saved" <"$device"; printf "\\033[?1000l\\033[?1002l\\033[?1006l\\033[?2004l\\033[0m\\033[?25h\\033[?1049l" >"$device"; }; trap restore EXIT; trap \'exit 0\' HUP INT TERM; /bin/cat "$device" & reader=$!; while IFS= read -r line; do :; done'
function M.terminal(mouse)
    local self = {mouse = mouse, width = 79, height = 24}
    function self:open(on_input, on_error)
        local saved = assert(rex.exec({"/bin/sh", "-c", '/bin/ps -p "$PPID" -o tty=; stty -g </dev/tty'}, {timeout = 2}))
        assert(saved.ok, saved.stderr)
        local device
        device, self.saved = saved.stdout:match("^%s*([^%s]+)%s+([^\r\n]+)")
        assert(device and device:match("^tty[%w]+$") and self.saved ~= "", "需要交互终端")
        self.device = "/dev/" .. device
        -- Detached commands have no controlling tty. Pass the actual PTY path,
        -- not /dev/tty, to the input/restoration helper.
        self.reader = assert(rex.exec.start({"/bin/sh", "-c", guard, "tty-input", self.saved, self.device}, {stdin = true, detach = true, stdout = on_input, stderr = function(data) on_error(M.sanitize(data, true)) end}))
        local raw = assert(rex.exec({"/bin/sh", "-c", "stty -icanon -echo -isig -ixon -icrnl min 1 time 0 </dev/tty"}, {timeout = 2}))
        assert(raw.ok, raw.stderr)
        self.active = true
        io.write("\27[?1049h\27[?25l\27[?2004h" .. (mouse and "\27[?1002h\27[?1006h" or ""))
        io.flush()
    end
    function self:size()
        local result = rex.exec({"/bin/sh", "-c", "stty size </dev/tty"}, {timeout = 1})
        local rows, cols
        if result then rows, cols = result.stdout:match("(%d+)%s+(%d+)") end
        if rows then self.width, self.height = math.max(1, tonumber(cols) - 1), math.max(1, tonumber(rows)) end
        return self.width, self.height
    end
    function self:draw(rows)
        local out = {"\27[H"}
        for index = 1, math.min(#rows, self.height) do
            if index > 1 then out[#out + 1] = "\r\n" end
            local row = rows[index]
            out[#out + 1] = "\27[0m" .. (colors[row[2]] or "") .. M.clip(M.sanitize(row[1], true), self.width) .. "\27[0m\27[K"
        end
        out[#out + 1] = "\27[J"
        local frame = M.concat(out)
        if frame ~= self.frame then io.write(frame); io.flush(); self.frame = frame end
    end
    function self:close()
        if self.reader then
            pcall(function()
                self.reader:close_stdin()
                self.reader:wait(0.5)
                if self.reader:running() then self.reader:kill(); self.reader:wait(0.3) end
            end)
            self.reader = nil
        end
        if self.saved then
            pcall(rex.exec, {"/bin/sh", "-c", 'stty "$1" </dev/tty', "tty-restore", self.saved}, {timeout = 1})
            self.saved = nil
        end
        if self.active then io.write(restore); io.flush(); self.active = false end
    end
    return self
end
function M.run(model, options)
    local screen, keys = M.terminal(options.mouse), M.keys()
    local stopped, ticks, last = false, 0, nil
    local timer
    local function close(code, err)
        if stopped then return end
        stopped = true
        if timer then timer:cancel() end
        if model.close then pcall(function() model:close() end) end
        screen:close()
        if err then io.stderr:write(M.sanitize(tostring(err)) .. "\n") end
        rex.stop(code or 0)
    end
    local function safe(fn)
        local ok, err = xpcall(fn, debug.traceback)
        if not ok then close(1, err) end
    end
    local function events(data)
        for _, event in ipairs(keys:feed(data)) do
            if model:event(event) == false then close(0); return end
        end
    end
    safe(function()
        screen:open(function(data) if not stopped then safe(function() events(data) end) end end,
            function(text) if not stopped then safe(function() error(text, 0) end) end end)
        screen:size()
        timer = rex.every(0.05, function()
            if stopped then return end
            safe(function()
                ticks = ticks + 1
                events("")
                if stopped then return end
                if not screen.reader:running() then close(0); return end
                if ticks % 4 == 1 then
                    local snapshot, bytes = M.read_json(options.root .. "/" .. (options.file or "view.json"))
                    if snapshot and bytes ~= last then
                        last = bytes
                        if snapshot.closed then close(0); return end
                        model:receive(snapshot)
                    end
                end
                if ticks % 10 == 1 then screen:size() end
                if model.tick then model:tick() end
                screen:draw(model:render(screen.width, screen.height))
            end)
        end)
    end)
end
return M
