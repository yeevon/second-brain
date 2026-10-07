export class RefreshCoordinator<T> {
  private revision = 0;
  private active: { revision: number; promise: Promise<void> } | null = null;
  private queued: {
    requestedRevision: number;
    load: () => Promise<T>;
    apply: (value: T) => void;
    reject: (error: unknown) => void;
    promise: Promise<void>;
    resolve: () => void;
    rejectPromise: (error: unknown) => void;
  } | null = null;

  invalidate(): number {
    return ++this.revision;
  }

  isCurrent(revision: number): boolean {
    return revision === this.revision;
  }

  isBusy(): boolean {
    return this.active !== null || this.queued !== null;
  }

  run(load: () => Promise<T>, apply: (value: T) => void, reject: (error: unknown) => void): Promise<void> {
    if (this.active) {
      if (this.active.revision === this.revision) return this.active.promise;
      if (this.queued?.requestedRevision === this.revision) return this.queued.promise;
      if (this.queued) this.queued.resolve();
      let resolve!: () => void;
      let rejectPromise!: (error: unknown) => void;
      const promise = new Promise<void>((resolvePromise, rejectQueued) => {
        resolve = resolvePromise;
        rejectPromise = rejectQueued;
      });
      this.queued = { requestedRevision: this.revision, load, apply, reject, promise, resolve, rejectPromise };
      return promise;
    }
    return this.start(load, apply, reject);
  }

  private start(load: () => Promise<T>, apply: (value: T) => void, reject: (error: unknown) => void): Promise<void> {
    const revision = ++this.revision;
    let loaded: Promise<T>;
    try {
      loaded = load();
    } catch (error) {
      loaded = Promise.reject(error);
    }
    const work = loaded
      .then((value) => {
        if (revision === this.revision) apply(value);
      })
      .catch((error: unknown) => {
        if (revision === this.revision) reject(error);
      })
      .finally(() => {
        if (this.active?.promise !== work) return;
        this.active = null;
        this.startQueued();
      });
    this.active = { revision, promise: work };
    return work;
  }

  private startQueued(): void {
    const queued = this.queued;
    if (!queued) return;
    this.queued = null;
    if (queued.requestedRevision !== this.revision) {
      queued.resolve();
      return;
    }
    void this.start(queued.load, queued.apply, queued.reject).then(queued.resolve, queued.rejectPromise);
  }
}
