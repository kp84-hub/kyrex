// Screen-only wake lock. Failure must never interrupt voice or repeat a task.
export class VoiceScreenLock {
  constructor({ wakeLock = globalThis.navigator?.wakeLock, visible = !globalThis.document?.hidden } = {}) {
    this.wakeLock = wakeLock; this.visible = visible; this.active = false; this.epoch = 0;
    this.lock = null; this.pending = null;
  }
  setActive(active) {
    if (this.active === active) return;
    this.active = active; this.sync();
  }
  setVisible(visible) { this.visible = visible; this.sync(); }
  release(lock) {
    try { Promise.resolve(lock?.release()).catch(() => {}); } catch {}
  }
  sync() {
    if (!this.active || !this.visible) {
      ++this.epoch; this.pending = null;
      const lock = this.lock; this.lock = null; this.release(lock);
      return;
    }
    if ((this.lock && !this.lock.released) || this.pending || !this.wakeLock?.request) return;
    const epoch = this.epoch;
    const pending = Promise.resolve().then(() => {
      if (epoch === this.epoch && this.active && this.visible) return this.wakeLock.request('screen');
      return null;
    }).then(lock => {
      if (!lock) return;
      // Stop, navigation or a new session may win while the browser prompts.
      if (epoch !== this.epoch || !this.active || !this.visible) { this.release(lock); return; }
      this.lock = lock;
      lock.addEventListener('release', () => { if (this.lock === lock) this.lock = null; }, { once: true });
    }).catch(() => {
      // Unsupported browsers and OS/battery policy remain usable for voice.
      // Retry only on a new session or visibility event, never in a tight loop.
    }).finally(() => { if (this.pending === pending) this.pending = null; });
    this.pending = pending;
  }
}
