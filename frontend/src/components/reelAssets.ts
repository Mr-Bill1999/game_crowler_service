export type ReelCover = { src: string; release: () => void };

type Job = { src: string; priority: number; resolve: (image: HTMLImageElement | null) => void };

/** A bounded, shared image queue for the decorative background only. */
export class ReelAssets {
  private sources: string[] = [];
  private reserved = new Map<string, symbol>();
  private recent: string[] = [];
  private failed = new Set<string>();
  private cache = new Map<string, HTMLImageElement>();
  private queue: Job[] = [];
  private active = new Set<() => void>();
  private listeners = new Set<() => void>();
  private disposed = false;

  setSources(sources: string[]) {
    this.sources = [...new Set(sources)];
    this.listeners.forEach((listener) => listener());
  }

  subscribe(listener: () => void) {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  async acquire(exclude: string | null, priority: number): Promise<ReelCover | null> {
    if (this.disposed) return null;
    const available = this.sources.filter((src) => src !== exclude && !this.reserved.has(src) && !this.failed.has(src));
    const fresh = available.filter((src) => !this.recent.includes(src));
    const candidates = fresh.length ? fresh : available;
    if (!candidates.length) return null;
    const src = candidates[Math.floor(Math.random() * candidates.length)];
    const token = Symbol();
    this.reserved.set(src, token);
    this.recent.push(src);
    if (this.recent.length > Math.min(80, Math.floor(this.sources.length / 2))) this.recent.shift();
    const release = () => { if (this.reserved.get(src) === token) this.reserved.delete(src); };
    const image = await this.load(src, priority);
    if (!image || this.disposed) { release(); return null; }
    return { src, release };
  }

  private load(src: string, priority: number): Promise<HTMLImageElement | null> {
    const cached = this.cache.get(src);
    if (cached) {
      this.cache.delete(src);
      this.cache.set(src, cached);
      return Promise.resolve(cached);
    }
    return new Promise((resolve) => {
      this.queue.push({ src, priority, resolve });
      this.queue.sort((a, b) => a.priority - b.priority);
      this.drain();
    });
  }

  private drain() {
    while (!this.disposed && this.active.size < 6 && this.queue.length) {
      const job = this.queue.shift()!;
      const image = new Image();
      let finished = false;
      let timeout: ReturnType<typeof setTimeout>;
      const finish = (loaded: HTMLImageElement | null) => {
        if (finished) return;
        finished = true;
        clearTimeout(timeout);
        image.onload = null;
        image.onerror = null;
        this.active.delete(cancel);
        if (loaded && !this.disposed) {
          this.cache.set(job.src, loaded);
          if (this.cache.size > 48) this.cache.delete(this.cache.keys().next().value!);
        } else if (!this.disposed) {
          this.failed.add(job.src);
        }
        job.resolve(loaded);
        this.drain();
      };
      const cancel = () => { image.removeAttribute("src"); finish(null); };
      this.active.add(cancel);
      timeout = setTimeout(cancel, 10_000);
      image.decoding = "async";
      image.fetchPriority = job.priority === 0 ? "auto" : "low";
      image.onload = () => { image.decode().then(() => finish(image), () => finish(null)); };
      image.onerror = () => finish(null);
      image.src = job.src;
    }
  }

  dispose() {
    this.disposed = true;
    this.queue.splice(0).forEach((job) => job.resolve(null));
    [...this.active].forEach((cancel) => cancel());
    this.cache.clear();
    this.reserved.clear();
    this.listeners.clear();
  }
}
