import React, { useEffect, useState } from 'react';
import { deleteProviderProfile, listProviderProfiles, saveProviderProfile } from '../lib/api.js';

const empty = { id: '', name: '', provider: 'openai', base_url: '', api_key: '', models: '' };

export default function ProviderSettings({ onClose, onSaved }) {
  const [profiles, setProfiles] = useState([]);
  const [form, setForm] = useState(empty);
  const [error, setError] = useState('');

  const refresh = async () => {
    try { setProfiles(await listProviderProfiles()); } catch (e) { setError(e.message); }
  };
  useEffect(() => { refresh(); }, []);

  const save = async (e) => {
    e.preventDefault();
    setError('');
    try {
      await saveProviderProfile({ ...form, models: form.models.split(',').map((m) => m.trim()).filter(Boolean) });
      setForm(empty);
      await refresh();
      onSaved?.();
    } catch (err) { setError(err.message); }
  };
  const remove = async (id) => {
    try { await deleteProviderProfile(id); await refresh(); onSaved?.(); }
    catch (err) { setError(err.message); }
  };

  return (
    <section className="provider-settings" aria-label="Provider settings">
      <div className="settings-heading">
        <div><h2>Provider settings</h2><p>Add a provider once, then switch models from the composer.</p></div>
        <button type="button" className="settings-close" onClick={onClose}>Close</button>
      </div>
      <form className="provider-form" onSubmit={save}>
        <input required placeholder="Profile ID, e.g. openrouter" value={form.id} onChange={(e) => setForm({ ...form, id: e.target.value.toLowerCase() })} />
        <input required placeholder="Display name, e.g. OpenRouter" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        <select value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value })}>
          <option value="openai">OpenAI-compatible</option>
          <option value="anthropic">Anthropic</option>
        </select>
        <input required type="url" placeholder="API URL" value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
        <input required type="password" placeholder="API key" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} />
        <input required placeholder="Models, comma separated" value={form.models} onChange={(e) => setForm({ ...form, models: e.target.value })} />
        <button type="submit" className="send-btn">Save provider</button>
      </form>
      {error && <div className="message-error">{error}</div>}
      <div className="provider-list">
        {profiles.map((p) => <div key={p.id} className="provider-row">
          <div><strong>{p.name}</strong><span>{p.base_url}</span><span>{p.models.join(', ')}</span></div>
          <button type="button" className="conversation-delete visible" onClick={() => remove(p.id)}>Remove</button>
        </div>)}
      </div>
    </section>
  );
}
