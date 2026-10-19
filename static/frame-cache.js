// One channel's coverage frames. Selected frames take priority over adjacent-hour prefetches.
class CoverageFrames {
  constructor({ base, coarseBytes, detailBytes, onLoad, onError, fetcher = (...args) => fetch(...args), delay = 250 }) {
    Object.assign(this, { base, coarseBytes, detailBytes, onLoad, onError, fetcher, delay });
    this.cache = new Map();
    this.pending = new Map();
    this.selection = null;
    this.closed = false;
  }
  get(hour, detail = false) {
    const key = `${hour}:${detail}`, value = this.cache.get(key);
    if (value) { this.cache.delete(key); this.cache.set(key, value); }
    return value;
  }
  async request(hour, detail = false) {
    if (this.closed) return;
    const key = `${hour}:${detail}`;
    if (this.cache.has(key)) return this.get(hour, detail);
    if (this.pending.has(key)) return this.pending.get(key).promise;
    const controller = new AbortController();
    const entry = { controller };
    this.pending.set(key, entry);
    entry.promise = (async () => {
      try {
        const response = await this.fetcher(`${this.base}/${hour}${detail ? '.detail' : ''}.bin.gz`, { signal: controller.signal });
        if (!response.ok) throw Object.assign(new Error(`HTTP ${response.status}`), { status: response.status });
        const buffer = await response.arrayBuffer();
        if (buffer.byteLength !== (detail ? this.detailBytes : this.coarseBytes)) throw new Error('Incomplete coverage frame');
        if (this.closed || controller.signal.aborted) return;
        const value = new DataView(buffer);
        this.cache.set(key, value);
        // Keep a small recently-used window, including the selected coarse and fine frames.
        while (this.cache.size > 12) {
          const oldest = [...this.cache.keys()].find(k => !this.wanted?.has(k));
          if (!oldest) break;
          this.cache.delete(oldest);
        }
        if (this.wanted?.has(key)) this.onLoad();
        return value;
      } catch (error) {
        if (!this.closed && !controller.signal.aborted && this.wanted?.has(key)) this.onError(error);
      } finally {
        if (this.pending.get(key) === entry) this.pending.delete(key);
      }
    })();
    return entry.promise;
  }
  select(hour, detail, neighbors = []) {
    if (this.closed) return;
    const selection = `${hour}:${detail}`;
    if (selection === this.selection) return;
    this.selection = selection;
    clearTimeout(this.timer);
    this.wanted = new Set([`${hour}:false`, ...(detail ? [`${hour}:true`] : [])]);
    for (const [key, entry] of this.pending) {
      if (!this.wanted.has(key)) { entry.controller.abort(); this.pending.delete(key); }
    }
    Promise.all([this.request(hour), ...(detail ? [this.request(hour, true)] : [])]).then(values => {
      if (this.closed || this.selection !== selection || values.some(v => !v)) return;
      this.timer = setTimeout(async () => {
        for (const next of [...new Set(neighbors)].filter(h => h && h !== hour)) {
          for (const local of detail ? [false, true] : [false]) {
            if (this.closed || this.selection !== selection) return;
            await this.request(next, local);
          }
        }
      }, this.delay);
    });
  }
  close() {
    this.closed = true;
    clearTimeout(this.timer);
    for (const entry of this.pending.values()) entry.controller.abort();
    this.pending.clear();
    this.cache.clear();
  }
}
