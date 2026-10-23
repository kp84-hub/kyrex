import assert from 'node:assert/strict';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import { render } from '../src/ui.js';

function fixture(mode) {
  const dom = new JSDOM('<div id="app"></div>');
  const root = dom.window.document.querySelector('#app'), calls = [], handlers = new Map();
  let options = { account: 'Signed in', unread: 4, pinned: false, autostart: false, alerts: false };
  const dispose = render(root, { mode,
    invoke: async (name, data) => { calls.push([name, data]); return options; },
    listen: async (name, fn) => { handlers.set(name, fn); return () => handlers.delete(name); },
  });
  return { dom, root, calls, handlers, dispose, setOptions: value => { options = value; } };
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
