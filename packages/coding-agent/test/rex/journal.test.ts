import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { SessionJournal } from "../../src/rex/readonly/journal";

const fixtures: string[] = [];
afterEach(() => { for (const directory of fixtures.splice(0)) fs.rmSync(directory, { recursive: true, force: true }); });
function fixture(text: string): string {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "rsv-journal-"));
  fixtures.push(directory);
  const file = path.join(directory, "session.jsonl");
  fs.writeFileSync(file, text, { mode: 0o600 });
  return file;
}
test("complete records and >4000-character payloads are never truncated", async () => {
  const payload = JSON.stringify({ text: "x".repeat(12000) + "END-MARKER" }) + "\n";
  const journal = new SessionJournal(fixture(payload));
  try {
    expect(await journal.poll(() => false)).toEqual({ reset: true, text: payload });
    expect(await journal.poll(() => false)).toBeUndefined();
  } finally { journal.close(); }
});
test("partial JSON and split UTF-8 append only once complete", async () => {
  const file = fixture("first\n");
  const journal = new SessionJournal(file);
  try {
    expect((await journal.poll(() => false))?.text).toBe("first\n");
    const bytes = Buffer.from('{"text":"中文"}\n');
    fs.appendFileSync(file, bytes.subarray(0, 10));
    expect((await journal.poll(() => false))?.text).toBe("");
    fs.appendFileSync(file, bytes.subarray(10));
    expect((await journal.poll(() => false))?.text).toBe(bytes.toString("utf8"));
    expect(await journal.poll(() => false)).toBeUndefined();
  } finally { journal.close(); }
});
test("same-inode title changes reload while inode substitution is rejected", async () => {
  const file = fixture("old-title\nbody\n");
  const journal = new SessionJournal(file);
  try {
    await journal.poll(() => false);
    const fd = fs.openSync(file, "r+");
    fs.writeSync(fd, Buffer.from("new-title"), 0, 9, 0);
    fs.closeSync(fd);
    expect(await journal.poll(() => false)).toEqual({ reset: true, text: "new-title\nbody\n" });
    fs.renameSync(file, `${file}.old`);
    fs.writeFileSync(file, "replacement\n", { mode: 0o600 });
    await expect(journal.poll(() => false)).rejects.toThrow("replaced");
  } finally { journal.close(); }
});
test("cancelled old selection cannot emit a buffered record", async () => {
  const file = fixture("a".repeat(600000) + "\n");
  const journal = new SessionJournal(file);
  try {
    let calls = 0;
    expect(await journal.poll(() => ++calls > 2)).toBeUndefined();
  } finally { journal.close(); }
});
test("symlinks and group-writable journals are rejected", () => {
  const file = fixture("text\n");
  fs.symlinkSync(file, `${file}.link`);
  expect(() => new SessionJournal(`${file}.link`)).toThrow();
  fs.chmodSync(file, 0o620);
  expect(() => new SessionJournal(file)).toThrow("non-writable-by-others");
});
