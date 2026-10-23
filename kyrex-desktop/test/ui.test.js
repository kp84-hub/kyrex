import assert from 'node:assert/strict';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import { render } from '../src/ui.js';

function fixture(mode) {
  const dom = new JSDOM('<div id="app"></div>');
  const root = dom.window.document.querySelector('#app'), calls = [], handlers = new Map();
  let options = { account: 'Signed in', unread: 4, pinned: false, autostart: false, alerts: false, roaming: true };
  const media = new dom.window.EventTarget(); media.matches = false;
  dom.window.matchMedia = () => media;
  const dispose = render(root, { mode,
    invoke: async (name, data) => { calls.push([name, data]); return options; },
    listen: async (name, fn) => { handlers.set(name, fn); return () => handlers.delete(name); },
  });
  return { dom, root, calls, handlers, dispose, media, setOptions: value => { options = value; } };
}
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

test('launcher opens once; dragging never opens Chat; keyboard activation works', async () => {
  const f = fixture('launcher'); await tick();
  const orb = f.root.querySelector('button');
  orb.click(); assert.equal(f.calls.at(-1)[0], 'toggle_chat');
  orb.dispatchEvent(new f.dom.window.MouseEvent('pointerdown', { button: 0, screenX: 20, screenY: 20 }));
  orb.dispatchEvent(new f.dom.window.MouseEvent('pointermove', { screenX: 40, screenY: 25 }));
  assert.equal(f.calls.at(-1)[0], 'drag_launcher');
  const before = f.calls.length; orb.click(); assert.equal(f.calls.length, before);
  orb.click(); assert.equal(f.calls.at(-1)[0], 'toggle_chat');
  orb.dispatchEvent(new f.dom.window.MouseEvent('contextmenu', { bubbles: true, cancelable: true }));
  assert.equal(f.calls.at(-1)[0], 'show_settings'); f.dispose();
});

test('badge is bounded, accessible, and removed on sign-out without remote HTML', async () => {
  const f = fixture('launcher'); await tick();
  assert.match(f.root.querySelector('button').getAttribute('aria-label'), /4 unread/);
  f.handlers.get('companion-unread')({ payload: 150 });
  assert.equal(f.root.querySelector('.badge').textContent, '99+');
  f.handlers.get('companion-unread')({ payload: '<img onerror=alert(1)>' });
  assert.equal(f.root.querySelector('.badge').hidden, true);
  assert.equal(f.root.querySelectorAll('img').length, 1);
  f.dispose(); assert.equal(f.handlers.size, 0);
});

test('settings leave startup and toasts off until explicit user choice', async () => {
  const f = fixture('settings'); await tick();
  assert.equal(f.root.querySelector('#autostart').checked, false);
  assert.equal(f.root.querySelector('#alerts').checked, false);
  assert.deepEqual(f.calls.map(([name]) => name), ['companion_status']);
  const input = f.root.querySelector('#autostart'); input.checked = true;
  input.dispatchEvent(new f.dom.window.Event('change')); await tick();
  assert.deepEqual(f.calls.at(-1), ['set_option', { name: 'autostart', enabled: true }]);
  f.root.querySelector('#signin').click(); await tick();
  assert.equal(f.calls.at(-1)[0], 'start_login');
  assert.match(f.root.querySelector('[role=status]').textContent, /browser/);
  f.dispose();
});


test('Kbot accepts only fixed animation phases and retains one accessible launcher', async () => {
  const f = fixture('launcher'); await tick();
  const orb = f.root.querySelector('button');
  assert.match(orb.getAttribute('aria-label'), /Kbot/);
  for (const phase of ['listening', 'thinking', 'replying', 'speaking', 'idle']) {
    f.handlers.get('companion-phase')({ payload: phase });
    assert.equal(orb.dataset.phase, phase);
  }
  f.handlers.get('companion-phase')({ payload: '<img onerror=alert(1)>' });
  assert.equal(orb.dataset.phase, 'idle');
  assert.equal(f.root.querySelectorAll('button').length, 1);
  assert.equal(f.root.querySelectorAll('.mascot-jet').length, 4);
  assert.equal(f.root.querySelector('.mascot-mouth').closest('[aria-hidden]').getAttribute('aria-hidden'), 'true');
  f.dispose();
});

test('holding Kbot opens the inbox once; release never toggles it closed', async t => {
  const f = fixture('launcher'); await tick();
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const orb = f.root.querySelector('button');
  orb.dispatchEvent(new f.dom.window.MouseEvent('pointerdown', { button: 0, screenX: 20, screenY: 20 }));
  t.mock.timers.tick(599);
  assert.deepEqual(f.calls.map(([name]) => name), ['companion_status', 'launcher_activity']);
  t.mock.timers.tick(1);
  assert.deepEqual(f.calls.at(-1), ['open_chat', { view: 'notifications' }]);
  const count = f.calls.length;
  t.mock.timers.tick(1000);
  orb.dispatchEvent(new f.dom.window.MouseEvent('pointerup'));
  orb.click();
  assert.equal(f.calls.length, count);
  orb.dispatchEvent(new f.dom.window.MouseEvent('pointerdown', { button: 0 }));
  orb.dispatchEvent(new f.dom.window.MouseEvent('pointerup'));
  orb.click();
  assert.equal(f.calls.at(-1)[0], 'toggle_chat', 'A fresh short click still chats');
  orb.dispatchEvent(new f.dom.window.KeyboardEvent('keydown', { key: 'n' }));
  assert.deepEqual(f.calls.at(-1), ['open_chat', { view: 'notifications' }]);
  f.dispose();
});

test('hover, focus and reduced motion pause flight; fixed poses and cleanup stay local', async () => {
  const f = fixture('launcher'); await tick();
  const orb = f.root.querySelector('button');
  const snapshot = (hovered, focused, reducedMotion) => ['launcher_activity', { hovered, focused, reducedMotion }];
  assert.deepEqual(f.calls.at(-1), snapshot(false, false, false));
  f.handlers.get('companion-motion')({ payload: 'sleeping' });
  assert.equal(orb.dataset.motion, 'sleeping');
  orb.dispatchEvent(new f.dom.window.Event('pointerenter'));
  assert.deepEqual(f.calls.at(-1), snapshot(true, false, false));
  f.handlers.get('companion-motion')({ payload: 'waking' });
  assert.equal(orb.dataset.motion, 'waking');
  orb.focus();
  assert.deepEqual(f.calls.at(-1), snapshot(true, true, false));
  orb.dispatchEvent(new f.dom.window.Event('pointerleave'));
  assert.deepEqual(f.calls.at(-1), snapshot(false, true, false));
  f.dom.window.dispatchEvent(new f.dom.window.Event('blur'));
  assert.deepEqual(f.calls.at(-1), snapshot(false, false, false));
  f.media.matches = true; f.media.dispatchEvent(new f.dom.window.Event('change'));
  assert.deepEqual(f.calls.at(-1), snapshot(false, false, true));
  for (const mood of ['idle', 'flying-left', 'flying-right', 'boost', 'bonk']) {
    f.handlers.get('companion-motion')({ payload: mood }); assert.equal(orb.dataset.motion, mood);
  }
  f.handlers.get('companion-motion')({ payload: 'javascript:alert(1)' });
  assert.equal(orb.dataset.motion, 'bonk');
  const count = f.calls.length;
  f.dispose();
  orb.dispatchEvent(new f.dom.window.Event('pointerenter'));
  f.media.dispatchEvent(new f.dom.window.Event('change'));
  assert.equal(f.calls.length, count);
  assert.equal(f.handlers.size, 0);
});

test('Fly around is visible and can be switched off without a new header control', async () => {
  const f = fixture('settings'); await tick();
  const input = f.root.querySelector('#roaming');
  assert.equal(input.checked, true);
  input.checked = false;
  input.dispatchEvent(new f.dom.window.Event('change')); await tick();
  assert.deepEqual(f.calls.at(-1), ['set_option', { name: 'roaming', enabled: false }]);
  f.dispose();
});

test('dragging, cancelling, leaving, losing focus and disposal cancel the hold', async t => {
  const fixtures = Array.from({ length: 5 }, () => fixture('launcher')); await tick();
  t.mock.timers.enable({ apis: ['setTimeout'] });
  fixtures.forEach((f, i) => {
    const orb = f.root.querySelector('button');
    orb.dispatchEvent(new f.dom.window.MouseEvent('pointerdown', { button: 0, screenX: 20, screenY: 20 }));
    if (i === 0) orb.dispatchEvent(new f.dom.window.MouseEvent('pointermove', { screenX: 40, screenY: 20 }));
    if (i === 1) orb.dispatchEvent(new f.dom.window.MouseEvent('pointercancel'));
    if (i === 2) orb.dispatchEvent(new f.dom.window.MouseEvent('pointerleave'));
    if (i === 3) f.dom.window.dispatchEvent(new f.dom.window.Event('blur'));
    if (i === 4) f.dispose();
    t.mock.timers.tick(1000);
    assert.ok(!f.calls.some(([name]) => name === 'open_chat'));
    if (i === 0) { orb.click(); assert.equal(f.calls.at(-1)[0], 'drag_launcher'); }
    f.dispose();
  });
});
