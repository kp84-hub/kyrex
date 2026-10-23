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
    root.innerHTML = `<button class="orb" aria-label="Open Kyrex Chat" title="Kyrex · click to chat, drag to move, right-click for settings"><span class="mascot" aria-hidden="true">
      <svg class="mascot-clips" width="0" height="0"><defs><clipPath id="mascot-body-clip" clipPathUnits="objectBoundingBox"><path clip-rule="evenodd" d="M0 0H1V1H0Z M.218 .652L.265 .667L.286 .743L.188 .866L.071 .858L.076 .745L.161 .667Z M.746 .652L.794 .639L.865 .671L.946 .765L.948 .864L.884 .865L.797 .812L.735 .721Z M.337 .829L.433 .842L.475 1L.270 1L.295 .867Z M.598 .850L.684 .840L.767 1L.535 1L.548 .906Z"/></clipPath></defs></svg>
      <img class="mascot-body" src="/companion.png" alt="" draggable="false">
      <span class="mascot-jet jet-hand-left"></span>
      <span class="mascot-jet jet-hand-right"></span>
      <span class="mascot-jet jet-foot-left"></span>
      <span class="mascot-jet jet-foot-right"></span>
    </span><span class="badge" hidden></span></button>`;
    const orb = root.querySelector('.orb');
    let origin, dragged = false;
    orb.addEventListener('pointerdown', e => { if (e.button === 0) { origin = { x: e.screenX, y: e.screenY }; dragged = false; } });
    orb.addEventListener('pointermove', e => {
      if (origin && !dragged && Math.hypot(e.screenX - origin.x, e.screenY - origin.y) > 5) {
        dragged = true; origin = null; invoke('drag_launcher').catch(report);
      }
    });
    orb.addEventListener('pointerup', () => { origin = null; });
    orb.addEventListener('pointercancel', () => { origin = null; });
    orb.addEventListener('click', () => { if (!dragged) invoke('toggle_chat').catch(report); dragged = false; });
    orb.addEventListener('contextmenu', e => { e.preventDefault(); invoke('show_settings').catch(report); });
    const badge = count => {
      const n = Math.max(0, Math.min(9999, Number(count) || 0));
      const element = root.querySelector('.badge');
      element.hidden = !n; element.textContent = n > 99 ? '99+' : String(n);
      orb.setAttribute('aria-label', n ? `Open Kyrex Chat, ${n} unread notifications` : 'Open Kyrex Chat');
    };
    subscribe('companion-unread', badge).catch(report);
    invoke('companion_status').then(value => { if (!disposed) badge(value.unread); }).catch(report);
  } else {
    root.innerHTML = `<main class="settings-panel">
      <header class="brand"><img src="/companion.png" alt=""><div><p class="eyebrow">YOUR DESKTOP COMPANION</p><h1>Kyrex, close at hand.</h1></div></header>
      <p>Click the floating icon whenever you need Kyrex. Your existing chats, bots, and connections are here.</p>
      <p class="status" id="account">Checking connection…</p>
      <div class="actions"><button class="primary" id="chat">Open Chat</button><button id="signin">Sign in</button></div>
      <label class="option"><input type="checkbox" id="pinned"><span>Keep Chat on top<small>The floating icon always stays on top.</small></span></label>
      <label class="option"><input type="checkbox" id="autostart"><span>Start with Windows<small>Off until you choose to enable it.</small></span></label>
      <label class="option"><input type="checkbox" id="alerts"><span>Desktop alerts<small>Show a generic alert when unread activity increases. Message text stays inside Chat.</small></span></label>
      <div class="shortcuts"><span>Open or hide Chat</span><kbd>Ctrl + Alt + Space</kbd><span>Talk to Kyrex</span><kbd>Ctrl + Alt + K</kbd></div>
      <p class="status" role="status"></p>
      <p><small>Drag the icon to move it. Right-click the tray icon to hide it or quit. Closing Chat hides its panel; accepted tasks keep running. Voice stops when the panel is hidden.</small></p>
    </main>`;
    const status = root.querySelector('[role="status"]');
    const refresh = value => {
      root.querySelector('#account').textContent = value.account;
      root.querySelector('#pinned').checked = value.pinned;
      root.querySelector('#autostart').checked = value.autostart;
      root.querySelector('#alerts').checked = value.alerts;
      if (value.shortcut_error) report(value.shortcut_error);
    };
    root.querySelector('#chat').onclick = () => invoke('open_chat').catch(report);
    root.querySelector('#signin').onclick = async e => {
      e.target.disabled = true; status.classList.remove('error'); status.textContent = 'Finish signing in in your browser, then return here.';
      try { await invoke('start_login'); } catch (failure) { report(failure); }
      finally { e.target.disabled = false; }
    };
    for (const id of ['pinned', 'autostart', 'alerts']) root.querySelector('#' + id).onchange = async e => {
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
