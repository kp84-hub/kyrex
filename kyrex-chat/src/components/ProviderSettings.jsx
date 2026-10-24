import React, { useEffect, useRef, useState } from 'react';
import InstallApp from './InstallApp.jsx';
import EmailAutomationSettings from './EmailAutomationSettings.jsx';
import TrainerMonitorSettings from './TrainerMonitorSettings.jsx';
import PrivacySettings from './PrivacySettings.jsx';
import { consolidateProviderProfiles, deleteProviderProfile, listProviderProfiles, saveProviderProfile } from '../lib/api.js';

const empty = { id: '', name: '', provider: 'openai', base_url: '', api_key: '', models: '', headers: '' };

// Provider option presets. OpenCode is the recommended OpenCode gateway
// endpoint; OpenRouter and OpenAI-compatible are plain OpenAI-style base
// URLs. Selecting one pre-fills the endpoint (the user can still edit it).
const PROVIDER_OPTIONS = [
  { value: 'groq', label: 'Groq', base_url: 'https://api.groq.com/openai/v1' },
  { value: 'opencode', label: 'OpenCode Go', base_url: 'https://opencode.ai/inference/go/v1' },
  { value: 'openrouter', label: 'OpenRouter', base_url: 'https://openrouter.ai/api/v1' },
  { value: 'openai', label: 'OpenAI-compatible', base_url: 'https://api.openai.com/v1' },
  { value: 'anthropic', label: 'Anthropic', base_url: 'https://api.anthropic.com' },
];

// Parse a "Header-Name: value" block (one per line) into an object. Header
// VALUES are secrets: they are sent to the server once and never rendered
// back — the API returns only the header NAMES.
function parseHeaders(text) {
  const out = {};
  for (const line of String(text || '').split('\n')) {
    const idx = line.indexOf(':');
    if (idx === -1) continue;
    const name = line.slice(0, idx).trim();
    const value = line.slice(idx + 1).trim();
    if (name) out[name] = value;
  }
  return out;
}

export default function ProviderSettings({ onClose, onSaved, installState }) {
  const [profiles, setProfiles] = useState([]);
  const [form, setForm] = useState(empty);
  const isGo = p => /^https:\/\/opencode\.ai\/(?:zen\/go|inference\/go(?:\/openai)?)\/v1(?:\/|$)/.test(p?.base_url || "");
  const goProfiles = profiles.filter(isGo);
  const primaryGo = goProfiles.find(p => p.id === "opencode") || goProfiles[0];
  const [error, setError] = useState('');
  const [editingId, setEditingId] = useState(null);
  const [headerMode, setHeaderMode] = useState('replace');
  const [savedHeaders, setSavedHeaders] = useState([]);
  const [busy, setBusy] = useState(false);
  const formRef = useRef(null);

  const reset = () => {
    setForm(empty); setEditingId(null); setHeaderMode('replace'); setSavedHeaders([]);
  };
  const edit = (profile) => {
    setEditingId(profile.id); setHeaderMode('keep'); setSavedHeaders(profile.header_names || []);
    setForm({ id: profile.id, name: profile.name, provider: profile.provider,
      base_url: profile.base_url, api_key: '', models: profile.models.join(', '), headers: '' });
    setError('');
    formRef.current?.scrollIntoView?.({ block: 'start', behavior: 'smooth' });
  };

  const refresh = async () => {
    try { setProfiles(await listProviderProfiles()); } catch (e) { setError(e.message); }
  };
  useEffect(() => { refresh(); }, []);

  const save = async (e) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const { headers, ...fields } = form;
      await saveProviderProfile({
        ...fields,
        models: form.models.split(',').map((m) => m.trim()).filter(Boolean),
        ...(headerMode === 'keep' ? {} : { headers: headerMode === 'clear' ? {} : parseHeaders(headers) }),
      });
      reset();
      await refresh();
      onSaved?.();
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  const consolidate = async (id) => {
    if (busy || !primaryGo) return;
    setBusy(true); setError('');
    try {
      await consolidateProviderProfiles(id, primaryGo.id);
      reset(); await refresh(); onSaved?.();
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  const remove = async (id) => {
    if (busy) return;
    setBusy(true);
    try { await deleteProviderProfile(id); if (editingId === id) reset(); await refresh(); onSaved?.(); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };

  return (
    <section className="provider-settings" aria-label="Provider settings">
      <div className="settings-heading">
        <div><h2>Provider settings</h2><p>Add a provider once, then switch models from the composer.</p></div>
        <button type="button" className="settings-close" onClick={onClose}>Close</button>
      </div>
      <InstallApp state={installState} />
      <PrivacySettings />
      <EmailAutomationSettings />
      <TrainerMonitorSettings />
      <form ref={formRef} className="provider-form" onSubmit={save}>
        {isGo(form) && <p>One OpenCode Go profile works across Go models. Choose each Bot’s model in Bots.</p>}
        {editingId && <p role="status">Editing {form.name}. Leave the API key blank to keep your saved key.</p>}
        <input required disabled={busy || !!editingId} placeholder="Profile ID, e.g. openrouter" value={form.id} onChange={(e) => setForm({ ...form, id: e.target.value.toLowerCase() })} />
        <input required disabled={busy} placeholder="Display name, e.g. OpenRouter" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <select disabled={busy} value={form.provider} onChange={(e) => {
          const preset = PROVIDER_OPTIONS.find((p) => p.value === e.target.value);
          // Pre-fill the endpoint for a preset selection (OpenCode, OpenRouter,
          // etc.) — an explicit endpoint is required and a wrong one fails at
          // turn time, so a correct default avoids a trivial misconfiguration.
          setForm({ ...form, provider: e.target.value, base_url: preset?.base_url ?? form.base_url,
            ...(e.target.value === 'groq' ? { models: 'qwen/qwen3.8-27b',
              id: form.id || 'groq', name: form.name || 'Groq' } : {}) });
        }}>
          {PROVIDER_OPTIONS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
        </select>
        <input required disabled={busy} type="url" placeholder="API URL" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
        <input required={!editingId} disabled={busy} type="password" autoComplete="off" placeholder={editingId ? 'API key — leave blank to keep saved key' : 'API key'} value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} />
        <input required disabled={busy} placeholder="Models, comma separated" value={form.models} onChange={(e) => setForm({ ...form, models: e.target.value })} />
        {editingId && <label>Extra headers
          <select disabled={busy} aria-label="Extra headers action" value={headerMode} onChange={(e) => setHeaderMode(e.target.value)}>
            <option value="keep">Keep saved headers</option>
            <option value="replace">Replace extra headers</option>
            <option value="clear">Clear extra headers</option>
          </select>
          {savedHeaders.length > 0 && <span>Saved header names: {savedHeaders.join(', ')}</span>}
        </label>}
        {headerMode === 'replace' && <textarea
          disabled={busy}
          placeholder={'Extra headers, one per line (values are stored as secrets)\ne.g. HTTP-Referer: https://kyrex.dev'}
          value={form.headers}
          onChange={(e) => setForm({ ...form, headers: e.target.value })}
          rows={2}
        />}
        <button type="submit" disabled={busy} className="send-btn">{busy ? 'Saving…' : editingId ? 'Save changes' : 'Save provider'}</button>
        {editingId && <button type="button" disabled={busy} onClick={reset}>Cancel edit</button>}
      </form>
      {error && <div className="message-error">{error}</div>}
      <div className="provider-list">
        {profiles.map((p) => <div key={p.id} className="provider-row">
          <div>
            <strong>{p.name}</strong>
            <span>{p.base_url}</span>
            <span>{p.models.join(', ')}</span>
            <span className="provider-secret">
              {p.has_api_key ? `key ••••${p.api_key_last4 || ''}` : 'no key'}
              {p.header_names && p.header_names.length > 0
                ? ` · headers: ${p.header_names.join(', ')}`
                : ''}
            </span>
          </div>
          <div className="provider-actions">
          {isGo(p) && p.id !== primaryGo?.id && <button type="button" disabled={busy} className="bot-configure-btn" onClick={() => consolidate(p.id)}>Use {primaryGo.name} instead</button>}
          <button type="button" disabled={busy} className="bot-configure-btn" onClick={() => edit(p)}>Edit</button>
          <button type="button" disabled={busy} className="conversation-delete visible" onClick={() => remove(p.id)}>Remove</button>
          </div>
        </div>)}
      </div>
    </section>
  );
}
