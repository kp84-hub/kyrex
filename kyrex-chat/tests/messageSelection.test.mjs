import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import MessageList from '../src/components/MessageList.jsx';

const host = document.createElement('div');
document.body.append(host);
const composer = document.createElement('textarea');
composer.value = 'An unsent draft stays editable';
document.body.append(composer);
const root = createRoot(host);
const messages = [
  { id: 'earlier', role: 'assistant', content: 'Earlier answer must stay out.' },
  { id: 'prompt', role: 'user', content: 'My newest prompt' },
  { id: 'reply', role: 'assistant', content: 'Newest **reply** with a [link](https://example.com).\n\nSecond paragraph.',
    task: { status: 'done' }, events: [{ kind: 'progress', payload: { stage: 'Checked the workspace' } }] },
];
await act(async () => root.render(React.createElement(MessageList, { messages })));
const scopes = () => [...host.querySelectorAll('[data-message-selection]')];
const selection = document.getSelection();
const announce = async () => act(async () => document.dispatchEvent(new Event('selectionchange')));
const start = scope => act(() => scope.dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true })));
const select = (node, begin, end) => {
  const range = document.createRange();
  if (begin == null) range.selectNodeContents(node);
  else { range.setStart(node, begin); range.setEnd(node, end); }
  selection.removeAllRanges(); selection.addRange(range);
};
const shortcut = (target, options = {}) => {
  const event = new window.KeyboardEvent('keydown', { key: 'a', ctrlKey: true,
    bubbles: true, cancelable: true, ...options });
  act(() => target.dispatchEvent(event));
  return event;
};
const [earlier, prompt, reply] = scopes();

// Mobile native Select all is a document-wide range. The long-pressed reply
// supplies its boundary, even if selectstart subsequently targets the body.
start(reply);
document.body.dispatchEvent(new Event('selectstart', { bubbles: true }));
select(document.body);
await announce();
assert.equal(selection.toString(), 'Newest reply with a link.\nSecond paragraph.');
assert.equal(reply.contains(selection.anchorNode), true);
assert.equal(reply.contains(selection.focusNode), true);
assert.equal(host.querySelector('.jump-latest')?.textContent.includes('Latest'), true,
  'selection stops automatic stream scrolling');
assert.equal(selection.toString().includes('Earlier'), false);
assert.equal(selection.toString().includes('Task done'), false);

// Normal word selection stays a word; desktop shortcuts select this reply.
const firstText = reply.querySelector('p').firstChild;
select(firstText, 0, 6);
await announce();
assert.equal(selection.toString(), 'Newest');
assert.equal(shortcut(document.body).defaultPrevented, true);
assert.equal(selection.toString(), reply.textContent);
assert.equal(shortcut(document.body, { ctrlKey: false, metaKey: true }).defaultPrevented, true);
assert.equal(selection.toString(), reply.textContent);

// A backward drag into earlier messages is clipped without flipping its
// direction or pulling task/activity controls into the highlighted reply.
selection.setBaseAndExtent(firstText, 6, earlier.querySelector('p').firstChild, 0);
await announce();
assert.equal(selection.toString(), 'Newest');
assert.equal(selection.anchorNode, firstText);
assert.equal(selection.anchorOffset, 6);
assert.equal(selection.focusNode, reply);
assert.equal(selection.focusOffset, 0);

// Starting on a different prompt/reply changes the active boundary.
start(prompt);
select(document.body);
await announce();
assert.equal(selection.toString(), 'My newest prompt');
start(earlier);
assert.equal(shortcut(document.body).defaultPrevented, true);
assert.equal(selection.toString(), 'Earlier answer must stay out.');

// Controls and outside clicks must release scope, preserving native Ctrl+A
// and input selection. Extra modifiers are not hijacked either.
composer.focus();
composer.setSelectionRange(3, 9);
assert.equal(shortcut(composer).defaultPrevented, false);
await announce();
assert.equal(composer.selectionStart, 3);
assert.equal(composer.selectionEnd, 9);
composer.blur();
document.body.dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true }));
selection.removeAllRanges();
assert.equal(shortcut(document.body).defaultPrevented, false);
start(reply);
assert.equal(shortcut(document.body, { altKey: true }).defaultPrevented, false);

// A long press can start while the composer still has focus on mobile.
composer.focus();
start(reply);
select(document.body);
await announce();
assert.equal(selection.toString(), reply.textContent);
composer.blur();

// Streaming/finished render changes keep the same message boundary. Switching
// conversations removes it; stale selection state must not clamp the new chat.
await act(async () => root.render(React.createElement(MessageList, { messages: messages.map(m => m.id === 'reply'
  ? { ...m, content: 'Updated streaming reply', streaming: true } : m) })));
select(document.body);
await announce();
assert.equal(selection.toString(), 'Updated streaming reply');
await act(async () => root.render(React.createElement(MessageList, { messages: [
  { id: 'new-chat', role: 'assistant', content: 'A different conversation' },
] })));
const newReply = scopes()[0];
select(newReply);
await announce();
assert.equal(shortcut(document.body).defaultPrevented, true);
assert.equal(selection.toString(), 'A different conversation');

// Unmount removes global listeners; page-wide selection works elsewhere.
await act(async () => root.unmount());
select(document.body);
await announce();
assert.equal(shortcut(document.body).defaultPrevented, false);
composer.remove(); host.remove(); selection.removeAllRanges();
console.log('Native Select all, partial/backward selection, prompt/reply boundaries, composer editing and cleanup: passed');
