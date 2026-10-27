import React, { useEffect, useState } from 'react';
import { eventReviewRequest } from '../lib/api.js';

const initial = { enabled: false, emails: true, messages: false, bot_id: '', interval_seconds: 3600 };
export default function EventReviewSettings({ onOpenConversation }) {
  const [view, setView] = useState(null), [form, setForm] = useState(initial);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [reload, setReload] = useState(0);
  const accept = value => {
    const settings = value?.settings;
    if (!settings || Object.keys(initial).some(key => typeof settings[key] !== typeof initial[key])
      || ![3600, 7200, 21600, 86400].includes(settings.interval_seconds)
      || !Array.isArray(value.bots) || value.bots.some(bot => !bot || typeof bot.id !== 'string' || typeof bot.name !== 'string')) {
      throw new Error('Event review settings could not be loaded. Try again.');
    }
    // Validate before storing either state value: a failed response must not
    // leave an invalid view that crashes the surrounding provider settings.
    const fields = Object.fromEntries(Object.keys(initial).map(key => [key, settings[key]]));
    setView(value);
    setForm(fields);
  };
  useEffect(() => {
    let alive = true;
    eventReviewRequest().then(value => { if (alive) accept(value); }).catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [reload]);
  const save = async event => {
    event.preventDefault(); if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { accept(await eventReviewRequest('', 'PUT', form)); setNotice(form.enabled ? 'Event review enabled. The first check will start shortly.' : 'Event review paused.'); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  return <section className="email-automation-settings" aria-label="Calendar event review">
    <div className="email-automation-heading"><div><h3>Calendar event review</h3>
      <p>Find events in selected inboxes and synced phone messages, then review them before adding to Calendar.</p>
    </div><span className="email-automation-status">{view ? (view.settings.enabled ? 'Enabled' : 'Paused') : error ? 'Unavailable' : 'Loading…'}</span></div>
    {view && <form className="email-automation-form" onSubmit={save}>
      <label><span><input type="checkbox" checked={form.enabled} disabled={busy} onChange={e => setForm({ ...form, enabled: e.target.checked })} /> Enable event review</span></label>
      <label><span><input type="checkbox" checked={form.emails} disabled={busy} onChange={e => setForm({ ...form, emails: e.target.checked })} /> Selected email accounts</span></label>
      <label><span><input type="checkbox" checked={form.messages} disabled={busy} onChange={e => setForm({ ...form, messages: e.target.checked })} /> Synced phone messages</span></label>
      <label>Review with<select value={form.bot_id} disabled={busy} onChange={e => setForm({ ...form, bot_id: e.target.value })}>
        <option value="">Choose a running Bot</option>{view.bots.map(bot => <option key={bot.id} value={bot.id}>{bot.name}</option>)}
      </select></label>
      <label>Check frequency<select value={form.interval_seconds} disabled={busy} onChange={e => setForm({ ...form, interval_seconds: Number(e.target.value) })}>
        <option value={3600}>Hourly</option><option value={7200}>Every 2 hours</option><option value={21600}>Every 6 hours</option><option value={86400}>Daily</option>
      </select></label>
      <p className="email-automation-note">The first scan checks messages received in the previous 24 hours. Later checks use recent content, without repeating dismissed suggestions. Scans use your selected Bot’s model.</p>
      <p className="email-automation-note">Phone coverage is limited to synced text: up to 100 messages from 10 recent conversations. Attachments and browser-only Messages connections are excluded. Keep your phone synced.</p>
      <p className="email-automation-note">Times use {view.timezone}. Missing times need your input. Source or Calendar account changes require saving these settings again.</p>
      <button type="submit" className="send-btn" disabled={busy}>{busy ? 'Saving…' : 'Save event review'}</button>
    </form>}
    {view?.settings.last_scan && <p>Last check: {new Date(view.settings.last_scan * 1000).toLocaleString()}</p>}
    {view?.settings.detail && <p role="status">{view.settings.detail}</p>}
    {view?.configured && onOpenConversation && <button type="button" disabled={busy} onClick={() => onOpenConversation(view.conversation_id)}>Open Calendar suggestions</button>}
    {notice && <p role="status">{notice}</p>}{error && <div role="alert" className="message-error"><p>{error}</p>
      <button type="button" disabled={busy} onClick={() => { setError(''); setReload(value => value + 1); }}>Retry event review</button></div>}
  </section>;
}
