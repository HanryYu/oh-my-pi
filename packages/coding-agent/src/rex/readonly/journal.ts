import * as fs from "node:fs";

/** An already-open read-only descriptor: no writer, lock, migration, or repair. */
export class SessionJournal {
  readonly file: string;
  readonly #fd: number;
  readonly #device: number;
  readonly #inode: number;
  #offset = 0;
  #pending = Buffer.alloc(0);
  #prefix = Buffer.alloc(0);
  #mtime = -1;
  #closed = false;
  constructor(file: string) {
    this.file = file;
    this.#fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    const info = fs.fstatSync(this.#fd);
    if (!info.isFile() || info.uid !== process.getuid!() || info.nlink !== 1 || (info.mode & 0o022)) {
      fs.closeSync(this.#fd);
      throw new Error("Transcript must be an owned, non-writable-by-others regular file");
    }
    this.#device = info.dev;
    this.#inode = info.ino;
  }
  async poll(cancelled: () => boolean): Promise<{ reset: boolean; text: string } | undefined> {
    if (this.#closed || cancelled()) return undefined;
    const info = fs.lstatSync(this.file);
    if (!info.isFile() || info.dev !== this.#device || info.ino !== this.#inode || info.uid !== process.getuid!() || info.nlink !== 1 || (info.mode & 0o022)) throw new Error("Transcript was replaced; reselect the agent to reopen it");
    let reset = this.#offset === 0;
    if (this.#prefix.length) {
      const prefix = Buffer.alloc(this.#prefix.length);
      const count = fs.readSync(this.#fd, prefix, 0, prefix.length, 0);
      if (count !== prefix.length || !prefix.equals(this.#prefix)) reset = true;
    }
    // Native title slots can rewrite the prefix without changing the inode.
    if (info.size < this.#offset || (info.size === this.#offset && info.mtimeMs !== this.#mtime)) reset = true;
    if (reset) { this.#offset = 0; this.#pending = Buffer.alloc(0); }
    if (info.size === this.#offset && !reset) return undefined;
    const chunks: Buffer[] = this.#pending.length ? [this.#pending] : [];
    while (this.#offset < info.size && !cancelled() && !this.#closed) {
      const chunk = Buffer.allocUnsafe(Math.min(256 * 1024, info.size - this.#offset));
      const count = fs.readSync(this.#fd, chunk, 0, chunk.length, this.#offset);
      if (!count) break;
      this.#offset += count;
      chunks.push(chunk.subarray(0, count));
      // Let selection, input and resize handlers run while a large file loads.
      const turn = Promise.withResolvers<void>();
      setImmediate(turn.resolve);
      await turn.promise;
    }
    if (cancelled() || this.#closed) return undefined;
    this.#mtime = info.mtimeMs;
    const bytes = Buffer.concat(chunks);
    const end = bytes.lastIndexOf(0x0a) + 1;
    // An incomplete JSONL record (including split UTF-8) is retained verbatim.
    // There is deliberately no 4000-char, record-size or transcript-size limit.
    this.#pending = bytes.subarray(end);
    this.#prefix = Buffer.alloc(Math.min(this.#offset, 4096));
    fs.readSync(this.#fd, this.#prefix, 0, this.#prefix.length, 0);
    return { reset, text: bytes.subarray(0, end).toString("utf8") };
  }
  close(): void {
    if (this.#closed) return;
    this.#closed = true;
    fs.closeSync(this.#fd);
  }
}
