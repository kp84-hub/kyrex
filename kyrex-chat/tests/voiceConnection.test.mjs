import assert from 'node:assert/strict';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import VoiceConnection from '../src/components/VoiceConnection.jsx';

const div = document.body.appendChild(document.createElement('div'));
const root = createRoot(div);
const calls = [];
let copied = '';
let paired = false;
Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async value => { copied = value; } } });
globalThis.fetch = async (url, options) => {
  calls.push([url, options.method]);
  if (options.body) { const body = JSON.parse(options.body); assert.deepEqual(Object.keys(body).sort(), ['name', 'platform']); }
  return { ok: true, json: async () => url.endsWith('/connect')
    ? { pairing_code: 'voice-test-code', expires_at: Date.now() / 1000 + 600 } : { paired } };
};
const button = text => [...div.querySelectorAll('button')].find(item => item.textContent === text);
await act(async () => root.render(React.createElement(VoiceConnection)));
assert.deepEqual(calls, [['/api/connections/voice', 'GET']], 'opening setup never requests microphone, pairs or sends');
await act(async () => button('Pair voice phone').click());
assert.equal(calls.at(-1)[0], '/api/connections/voice/connect');
assert.match(div.textContent, /10 minutes/);
await act(async () => button('Copy voice pairing code').click());
assert.equal(copied, 'voice-test-code');
paired = true;
await act(async () => window.dispatchEvent(new Event('focus')));
assert.match(div.querySelector('summary').textContent, /paired/, 'returning from the phone refreshes pairing status');
await act(async () => button('Disconnect all voice devices').click());
assert.equal(calls.at(-2)[0], '/api/connections/voice/disconnect');
assert.equal(button('Copy voice pairing code'), undefined);
await act(async () => root.unmount());
console.log('voice pairing is explicit, copyable, owner-scoped and revocable: passed');

const settingsDiv = document.body.appendChild(document.createElement('div'));
const settingsRoot = createRoot(settingsDiv);
const options = { voices: { google: ['en-US-Chirp3-HD-Leda', 'en-US-Chirp3-HD-Puck'], openai: ['cedar', 'marin'] },
  transcription_models: { groq: ['whisper-large-v3', 'whisper-large-v3-turbo'], openai: ['gpt-4o-mini-transcribe'] } };
let settings = { saved: true, configured: true, engine: 'openai', speech_provider: 'openai', transcription_provider: 'openai',
  voice: 'marin', transcription_model: 'gpt-4o-mini-transcribe', speaking_rate: 1,
  speech_configured: true, transcription_configured: true, credentials: { openai: true, google: false, groq: false }, options };
const settingsCalls = [];
globalThis.fetch = async (url, init) => {
  settingsCalls.push([url, init]);
  if (url.endsWith('/preview')) return { ok: true, blob: async () => new Blob(['test-audio'], { type: 'audio/wav' }) };
  if (url.endsWith('/settings')) {
    const body = JSON.parse(init.body);
    const present = { ...settings.credentials };
    for (const [provider, key] of Object.entries(body.credentials)) if (key) present[provider] = true;
    for (const provider of body.remove_credentials) present[provider] = false;
    const { credentials: inputs, remove_credentials, ...preferences } = body;
    settings = { ...settings, ...preferences, credentials: present };
    return { ok: true, json: async () => settings };
  }
  return { ok: true, json: async () => ({ paired: false, devices: [], settings }) };
};
const settingsButton = text => [...settingsDiv.querySelectorAll('button')].find(item => item.textContent === text);
const control = label => [...settingsDiv.querySelectorAll('label')].find(item => item.firstChild.textContent === label)?.querySelector('select,input,textarea');
const select = async (label, value) => act(async () => {
  const element = control(label); element.value = value; element.dispatchEvent(new Event('change', { bubbles: true }));
});
const input = async (label, value) => act(async () => {
  const element = control(label);
  Object.getOwnPropertyDescriptor(element.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype, 'value').set.call(element, value);
  element.dispatchEvent(new Event('input', { bubbles: true }));
});
await act(async () => settingsRoot.render(React.createElement(VoiceConnection)));
assert.equal(control('Reply voice provider').value, 'openai', 'existing users retain their provider');
assert.equal(control('Reply voice').value, 'marin');
assert.equal(control('OpenAI API key').value, '', 'saved credentials are never put back in inputs');
assert.deepEqual(settingsCalls.map(([url]) => url), ['/api/connections/voice']);
await select('Reply voice provider', 'google');
await select('PC transcription provider', 'groq');
await select('Reply voice', 'en-US-Chirp3-HD-Puck');
await select('Speaking speed', '0.75');
await select('Follow-up listening', '15');
await select('Transcription model', 'whisper-large-v3-turbo');
await input('Google Cloud service account JSON', '{"type":"service_account","test_only":true}');
await input('Groq API key', 'test-only-groq-api-key-1234567890');
await act(async () => window.dispatchEvent(new Event('focus')));
assert.equal(control('Reply voice provider').value, 'google', 'focus refresh does not erase unsaved choices');
assert.equal(control('Groq API key').value, 'test-only-groq-api-key-1234567890');
assert.equal(settingsButton('Preview saved voice').disabled, true, 'save first so preview matches the persisted voice');
await act(async () => settingsButton('Save voice settings').click());
const savedBody = JSON.parse(settingsCalls.find(([url]) => url.endsWith('/settings'))[1].body);
assert.equal(savedBody.speech_provider, 'google');
assert.equal(savedBody.transcription_provider, 'groq');
assert.equal(savedBody.voice, 'en-US-Chirp3-HD-Puck');
assert.equal(savedBody.transcription_model, 'whisper-large-v3-turbo');
assert.equal(savedBody.speaking_rate, 0.75);
assert.equal(savedBody.follow_up_seconds, 15);
assert.equal(savedBody.credentials.groq, 'test-only-groq-api-key-1234567890');
assert.equal(control('Groq API key').value, '');
assert.equal(control('Google Cloud service account JSON').value, '');
assert.match(settingsDiv.textContent, /Restart listening/);
let played = 0, paused = 0, revoked = 0;
globalThis.Audio = class { play = async () => { played++; }; pause = () => { paused++; }; };
URL.createObjectURL = () => 'blob:test-voice-preview'; URL.revokeObjectURL = () => { revoked++; };
await act(async () => settingsButton('Preview saved voice').click());
assert.equal(played, 1);
assert.equal(settingsCalls.at(-1)[0], '/api/connections/voice/preview');
await act(async () => settingsButton('Remove OpenAI credentials').click());
assert.deepEqual(JSON.parse(settingsCalls.at(-1)[1].body).remove_credentials, ['openai']);
assert.equal(control('Reply voice provider').value, 'google', 'removing an old key keeps the selected provider');
assert.equal(settings.credentials.groq, true);
assert.equal(settings.credentials.openai, false);
assert.equal(control('Follow-up listening').value, '15', 'credential changes preserve follow-up preferences');
assert.equal(paused, 1); assert.equal(revoked, 1);
await select('Reply voice provider', 'device');
assert.equal(control('Reply voice'), undefined);
assert.equal(control('PC transcription provider').value, 'groq', 'device speech does not change PC transcription');
await act(async () => settingsRoot.unmount());
console.log('speech providers, models, voices, encrypted credential inputs and preview cleanup: passed');
