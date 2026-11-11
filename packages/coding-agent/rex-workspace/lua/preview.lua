local dir = debug.getinfo(1, "S").source:match("^@?(.*/)") or "./"
local ui = dofile(dir .. "ui.lua")
local M = {}
function M.new(send)
    local self = {send = send, title = "Changes", text = "", scroll = 0, view_height = 22, rendered = {}, diff = true}
    function self:receive(snapshot)
        self.title = ui.field(snapshot, "title", "Changes")
        self.text = ui.sanitize(type(snapshot.text) == "string" and snapshot.text:sub(1, 1024 * 1024) or "")
        self.diff, self.cache, self.scroll = snapshot.diff == true, nil, 0
    end
    function self:render(width, height)
        self.view_height = math.max(0, height - 1)
        if self.cache ~= width then
            self.rendered = {}
            for _, line in ipairs(ui.wrap(self.text, width)) do
                local style = ""
                if self.diff then
                    if line:sub(1, 1) == "+" then style = "good"
                    elseif line:sub(1, 1) == "-" then style = "error"
                    elseif line:sub(1, 2) == "@@" then style = "accent" end
                end
                self.rendered[#self.rendered + 1] = {line, style}
            end
            self.cache = width
        end
        local maximum = math.max(0, #self.rendered - self.view_height)
        local start = self.scroll == nil and maximum or math.min(self.scroll, maximum)
        if self.scroll ~= nil then self.scroll = start end
        local rows = {{self.title, "heading"}}
        for i = start + 1, math.min(#self.rendered, start + self.view_height) do rows[#rows + 1] = self.rendered[i] end
        return rows
    end
    function self:event(event)
        if event == "quit" or event == "escape" or type(event) == "table" and event[1] == "text" and event[2] == "q" then self.send({type = "close-preview"}); return false end
        ui.scroll(self, event, #self.rendered)
        return true
    end
    return self
end
if not (rex.args and rex.args.library) then
    local root, module = assert(rex.args.root, "root is required"), assert(rex.args.module, "module is required")
    local preview = M.new(function(event) ui.request(module, root, "preview", event) end)
    ui.run(preview, {root = root, file = "preview.json", mouse = false})
end
if rex.args and rex.args.library then return M end
