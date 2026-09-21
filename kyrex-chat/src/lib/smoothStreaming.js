// Turn irregular provider chunks into a steady visual text stream.
// The server's final text remains authoritative; this only controls pacing.
export function createTextStreamSmoother(onText, options = {}) {
  const raf = options.raf || requestAnimationFrame;
  const caf = options.caf || cancelAnimationFrame;
  const baseChars = options.baseChars || 3;
  let shown = '';
  let target = '';
  let frame = null;
  let stopped = false;
  let terminal = null;
  let waiters = [];

  const settle = () => {
    const pending = waiters;
    waiters = [];
    pending.forEach((resolve) => resolve());
  };

  const tick = () => {
    frame = null;
    if (stopped) return;
    const remaining = target.length - shown.length;
    if (remaining > 0) {
      const count = Math.min(remaining,
        remaining > 600 ? 24 : remaining > 240 ? 12 : remaining > 80 ? 6 : baseChars);
      shown = target.slice(0, shown.length + count);
      onText(shown);
    }
    if (shown.length < target.length) {
      frame = raf(tick);
      return;
    }
    if (terminal) {
      const done = terminal;
      terminal = null;
      done(shown);
    }
    settle();
  };

  const schedule = () => {
    if (!stopped && frame == null) frame = raf(tick);
  };

  return {
    push(chunk) {
      if (stopped || typeof chunk !== 'string' || !chunk) return;
      target += chunk;
      schedule();
    },
    finish(authoritative, onFinished) {
      if (stopped) return;
      const finalText = typeof authoritative === 'string' ? authoritative : target;
      if (finalText.startsWith(shown)) target = finalText;
      else {
        shown = finalText;
        target = finalText;
        onText(shown);
      }
      terminal = typeof onFinished === 'function' ? onFinished : null;
      schedule();
    },
    whenIdle() {
      if (stopped || (frame == null && shown.length >= target.length && !terminal)) {
        return Promise.resolve();
      }
      return new Promise((resolve) => waiters.push(resolve));
    },
    cancel() {
      stopped = true;
      if (frame != null) caf(frame);
      frame = null;
      terminal = null;
      settle();
    },
  };
}
