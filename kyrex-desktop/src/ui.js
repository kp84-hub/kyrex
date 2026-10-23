// All native authority lives in Rust. This document has only companion controls.
export function render(root, { mode, invoke, listen }) {
  const doc = root.ownerDocument;
  doc.body.className = mode;
  let disposed = false;
  const unlisten = [];
  const subscribe = async (event, fn) => {
    const stop = await listen(event, ({ payload }) => { if (!disposed) fn(payload); });
    if (disposed) stop(); else unlisten.push(stop);
  };
  const report = failure => {
    const status = root.querySelector('[role="status"]');
    if (status) { status.textContent = String(failure); status.classList.add('error'); }
    else invoke('show_settings').catch(() => {});
  };
  if (mode === 'launcher') {
    root.innerHTML = `<button class="orb" aria-label="Talk to Kbot" aria-description="Click to chat. Hold to read notifications. Drag to move. Press N for notifications." aria-keyshortcuts="N" title="Kbot · click to chat, hold for notifications, drag to move, right-click for settings"><span class="mascot" aria-hidden="true">
      <svg class="mascot-clips" width="0" height="0"><defs><clipPath id="mascot-body-clip" clipPathUnits="objectBoundingBox"><path clip-rule="evenodd" d="M0 0H1V1H0Z M.218 .652L.265 .667L.286 .743L.188 .866L.071 .858L.076 .745L.161 .667Z M.746 .652L.794 .639L.865 .671L.946 .765L.948 .864L.884 .865L.797 .812L.735 .721Z M.337 .829L.433 .842L.475 1L.270 1L.295 .867Z M.598 .850L.684 .840L.767 1L.535 1L.548 .906Z"/></clipPath></defs></svg>
      <img class="mascot-body" src="/companion.png" alt="" draggable="false">
      <span class="mascot-jet jet-hand-left"></span>
      <span class="mascot-jet jet-hand-right"></span>
      <span class="mascot-jet jet-foot-left"></span>
      <span class="mascot-jet jet-foot-right"></span>
      <span class="sleep-eye eye-left"></span><span class="sleep-eye eye-right"></span>
      <span class="mascot-mouth"></span>
      <span class="sleep-marks">z<span>z</span></span>
      <span class="bonk-stars">✦ <span>✧</span> ✦</span>
    </span><span class="badge" hidden></span></button>`;
    const orb = root.querySelector('.orb');
    subscribe('companion-phase', phase => {
      if (['idle', 'listening', 'thinking', 'replying', 'speaking'].includes(phase)) orb.dataset.phase = phase;
    }).catch(report);
    const motion = value => {
      if (['idle', 'flying-left', 'flying-right', 'boost', 'bonk', 'sleeping', 'waking'].includes(value)) orb.dataset.motion = value;
    };
    const reduced = doc.defaultView.matchMedia?.('(prefers-reduced-motion: reduce)');
    let hovered = false, focused = false;
    const activity = () => disposed ? Promise.resolve() : invoke('launcher_activity', { hovered, focused, reducedMotion: reduced?.matches ?? false }).catch(report);
    const enter = () => { hovered = true; activity(); };
    const leave = () => { hovered = false; activity(); };
    const focus = () => { focused = true; activity(); };
    const blur = () => { focused = false; hovered = orb.matches(':hover'); activity(); };
    orb.addEventListener('pointerenter', enter);
    orb.addEventListener('pointerleave', leave);
    orb.addEventListener('focus', focus);
    orb.addEventListener('blur', blur);
    doc.defaultView.addEventListener('blur', blur);
    reduced?.addEventListener('change', activity);
    subscribe('companion-motion', motion).then(activity).catch(report);
    unlisten.push(() => {
      orb.removeEventListener('pointerenter', enter); orb.removeEventListener('pointerleave', leave);
      orb.removeEventListener('focus', focus); orb.removeEventListener('blur', blur);
      doc.defaultView.removeEventListener('blur', blur); reduced?.removeEventListener('change', activity);
    });
    let origin, holdTimer, dragged = false, held = false;
    const cancelHold = () => { clearTimeout(holdTimer); holdTimer = undefined; };
    const endPointer = () => { cancelHold(); origin = null; };
    const notifications = () => invoke('open_chat', { view: 'notifications' }).catch(report);
    orb.addEventListener('pointerdown', e => {
      if (e.button !== 0 || e.isPrimary === false) return;
      cancelHold(); origin = { x: e.screenX, y: e.screenY }; dragged = false; held = false;
      holdTimer = setTimeout(() => {
        holdTimer = undefined;
        if (disposed || !origin || dragged) return;
        held = true; origin = null; notifications();
      }, 600);
    });
    orb.addEventListener('pointermove', e => {
      if (origin && !dragged && Math.hypot(e.screenX - origin.x, e.screenY - origin.y) > 5) {
        cancelHold(); dragged = true; origin = null; invoke('drag_launcher').catch(report);
      }
    });
    orb.addEventListener('pointerup', endPointer);
    orb.addEventListener('pointerleave', endPointer);
    orb.addEventListener('pointercancel', () => { endPointer(); dragged = true; });
    doc.defaultView.addEventListener('blur', endPointer);
    unlisten.push(() => { endPointer(); doc.defaultView.removeEventListener('blur', endPointer); });
    orb.addEventListener('click', () => { if (!dragged && !held) invoke('toggle_chat').catch(report); dragged = false; held = false; });
    orb.addEventListener('contextmenu', e => { e.preventDefault(); if (!held) invoke('show_settings').catch(report); });
    orb.addEventListener('keydown', e => { if (e.key.toLowerCase() === 'n' && !e.repeat && !e.ctrlKey && !e.altKey && !e.metaKey) { e.preventDefault(); notifications(); } });
    const badge = count => {
      const n = Math.max(0, Math.min(9999, Number(count) || 0));
      const element = root.querySelector('.badge');
      element.hidden = !n; element.textContent = n > 99 ? '99+' : String(n);
      orb.setAttribute('aria-label', n ? `Talk to Kbot, ${n} unread notifications` : 'Talk to Kbot');
    };
    subscribe('companion-unread', badge).catch(report);
    invoke('companion_status').then(value => { if (!disposed) badge(value.unread); }).catch(report);
  } else {
    root.innerHTML = `<main class="settings-panel">
      <header class="brand"><img src="/companion.png" alt=""><div><p class="eyebrow">YOUR DESKTOP COMPANION</p><h1>Kbot, close at hand.</h1></div></header>
      <p>Click Kbot to chat. Hold him briefly to read notifications, or focus him and press N. Drag him to park for 30 seconds. Hover to wake him or catch him mid-flight.</p>
      <p class="status" id="account">Checking connection…</p>
      <div class="actions"><button class="primary" id="chat">Open Chat</button><button id="notifications">Notifications</button><button id="signin">Sign in</button></div>
      <label class="option"><input type="checkbox" id="roaming"><span>Fly around<small>Flights, sleepy breaks and the occasional rocket mishap. He stays put during chats and while you hover. Windows reduced motion keeps him still.</small></span></label>
      <label class="option"><input type="checkbox" id="pinned"><span>Keep Chat on top<small>The floating icon always stays on top.</small></span></label>
      <label class="option"><input type="checkbox" id="autostart"><span>Start with Windows<small>Off until you choose to enable it.</small></span></label>
      <label class="option"><input type="checkbox" id="alerts"><span>Desktop alerts<small>Show a generic alert when unread activity increases. Message text stays inside Chat.</small></span></label>
      <div class="shortcuts"><span>Open or hide Chat</span><kbd>Ctrl + Alt + Space</kbd><span>Talk to Kbot</span><kbd>Ctrl + Alt + K</kbd></div>
      <p class="status" role="status"></p>
      <p><small>Drag the icon to move it. Right-click the tray icon to hide it or quit. Closing Chat hides its panel; accepted tasks keep running. Voice stops when the panel is hidden.</small></p>
    </main>`;
    const status = root.querySelector('[role="status"]');
    const refresh = value => {
      root.querySelector('#account').textContent = value.account;
      root.querySelector('#pinned').checked = value.pinned;
      root.querySelector('#autostart').checked = value.autostart;
      root.querySelector('#alerts').checked = value.alerts;
      root.querySelector('#roaming').checked = value.roaming;
      if (value.shortcut_error) report(value.shortcut_error);
    };
    root.querySelector('#chat').onclick = () => invoke('open_chat').catch(report);
    root.querySelector('#notifications').onclick = () => invoke('open_chat', { view: 'notifications' }).catch(report);
    root.querySelector('#signin').onclick = async e => {
      e.target.disabled = true; status.classList.remove('error'); status.textContent = 'Finish signing in in your browser, then return here.';
      try { await invoke('start_login'); } catch (failure) { report(failure); }
      finally { e.target.disabled = false; }
    };
    for (const id of ['pinned', 'autostart', 'alerts', 'roaming']) root.querySelector('#' + id).onchange = async e => {
      e.target.disabled = true;
      try { refresh(await invoke('set_option', { name: id, enabled: e.target.checked })); status.classList.remove('error'); status.textContent = 'Saved.'; }
      catch (failure) { report(failure); invoke('companion_status').then(refresh).catch(() => {}); }
      finally { e.target.disabled = false; }
    };
    subscribe('companion-status', refresh).catch(report);
    subscribe('companion-error', report).catch(report);
    invoke('companion_status').then(value => { if (!disposed) refresh(value); }).catch(report);
  }
  return () => { disposed = true; unlisten.forEach(stop => stop()); };
}
