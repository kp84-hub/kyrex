import React, { useEffect, useState } from 'react';
import { budgetRequest, createBot, updateBotStatus } from '../lib/api.js';

const initial = { enabled: false, bot_id: '', interval_seconds: 3600, dollar_currency: 'USD', time_zone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' };
const kinds = ['purchase', 'subscription_charge', 'renewal_notice', 'refund', 'cancellation', 'trial'];
const categories = ['shopping', 'groceries', 'dining', 'transport', 'home', 'health', 'entertainment', 'software', 'education', 'travel', 'other'];
const label = text => text.replaceAll('_', ' ');
const amount = (minor, currency) => minor == null ? 'Amount needs review' : currency
  ? new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(minor / 100) : `${(minor / 100).toFixed(2)} · currency needed`;
function checkedLedger(value) {
  const validCurrency = c => ['USD', 'CAD', 'AUD', 'EUR', 'GBP'].includes(c);
  const validEntry = row => row && typeof row.id === 'string' && typeof row.revision === 'string'
    && typeof row.merchant === 'string' && kinds.includes(row.kind) && categories.includes(row.category)
    && typeof row.date === 'string' && (row.amount_minor === null || Number.isSafeInteger(row.amount_minor))
    && (row.currency === '' || validCurrency(row.currency)) && typeof row.cadence === 'string'
    && typeof row.renewal_date === 'string' && ['recorded', 'review', 'excluded'].includes(row.state)
    && Array.isArray(row.needs) && row.needs.every(n => typeof n === 'string')
    && typeof row.source?.label === 'string' && typeof row.source?.received === 'string' && typeof row.evidence === 'string';
  if (!value || !value.totals_minor || typeof value.totals_minor !== 'object' || Array.isArray(value.totals_minor)
    || Object.entries(value.totals_minor).some(([currency, total]) => !validCurrency(currency) || !Number.isSafeInteger(total))
    || !Array.isArray(value.entries) || !value.entries.every(validEntry)
    || !Array.isArray(value.subscriptions) || !value.subscriptions.every(validEntry)
    || !Array.isArray(value.categories) || value.categories.some(c => !c || typeof c.category !== 'string' || !validCurrency(c.currency) || !Number.isSafeInteger(c.amount_minor))
    || !Number.isInteger(value.review_count) || typeof value.limitation !== 'string') {
    throw new Error('Budget ledger could not be loaded. Refresh budget to try again.');
  }
  return value;
}

function Entry({ entry, refresh }) {
  const [form, setForm] = useState(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [deleting, setDeleting] = useState(false);
  const start = () => setForm({ merchant: entry.merchant, kind: entry.kind, category: entry.category,
    date: entry.date, amount: entry.amount_minor == null ? '' : (entry.amount_minor / 100).toFixed(2),
    currency: entry.currency, cadence: entry.cadence, renewal_date: entry.renewal_date, state: entry.state === 'excluded' ? 'excluded' : 'recorded' });
  const field = (key, value) => setForm({ ...form, [key]: value });
  const save = async event => {
    event.preventDefault(); if (busy) return;
    setBusy(true); setError('');
    try { await budgetRequest(`/entries/${encodeURIComponent(entry.id)}`, 'PATCH', { revision: entry.revision, fields: form }); await refresh(); setForm(null); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (busy) return;
    setBusy(true); setError('');
    try { await budgetRequest(`/entries/${encodeURIComponent(entry.id)}`, 'DELETE', { revision: entry.revision }); await refresh(); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  const startSelected = async () => {
    if (busy || !form.bot_id) return;
    setBusy(true); setError('');
    const id = form.bot_id;
    try { await updateBotStatus(id, 'running'); accept(await budgetRequest()); setForm(f => ({ ...f, bot_id: id })); onSaved?.(); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  return <article className="budget-entry">
    <strong>{entry.merchant}</strong><span>{entry.kind === 'refund' ? 'Refund · ' : ''}{amount(entry.amount_minor, entry.currency)}</span>
    <p>{entry.date} · {label(entry.kind)} · {entry.category} · {entry.state === 'review' ? 'Needs review — excluded from totals' : entry.state}</p>
    {entry.needs.length > 0 && <p>{entry.needs.join(', ')}</p>}
    <details><summary>Source and evidence</summary><p>{entry.source.label} · {entry.source.received}</p>
      <p>Date based on: {entry.date_basis}. {entry.reference && `Reference: ${entry.reference}`}</p><blockquote>{entry.evidence}</blockquote></details>
    {!form && <button type="button" onClick={start}>{entry.state === 'review' ? 'Review entry' : 'Edit entry'}</button>}
    {!form && !deleting && <button type="button" onClick={() => setDeleting(true)}>Delete record</button>}
    {deleting && <div><p>Delete this saved record and its quoted evidence? Your original email stays in your inbox.</p>
      <button type="button" disabled={busy} onClick={remove}>Delete saved record</button><button type="button" disabled={busy} onClick={() => setDeleting(false)}>Keep record</button></div>}
    {form && <form className="email-automation-form" onSubmit={save}>
      <label>Merchant<input required disabled={busy} maxLength={200} value={form.merchant} onChange={e => field('merchant', e.target.value)} /></label>
      <label>Type<select disabled={busy} value={form.kind} onChange={e => field('kind', e.target.value)}>{kinds.map(k => <option key={k} value={k}>{label(k)}</option>)}</select></label>
      <label>Category<select disabled={busy} value={form.category} onChange={e => field('category', e.target.value)}>{categories.map(k => <option key={k}>{k}</option>)}</select></label>
      <label>Date<input type="date" required disabled={busy} value={form.date} onChange={e => field('date', e.target.value)} /></label>
      <label>Total paid or refunded<input inputMode="decimal" disabled={busy} value={form.amount} onChange={e => field('amount', e.target.value)} /></label>
      <label>Currency<select disabled={busy} value={form.currency} onChange={e => field('currency', e.target.value)}><option value="">Choose currency</option>{['USD', 'CAD', 'AUD', 'EUR', 'GBP'].map(k => <option key={k}>{k}</option>)}</select></label>
      <label>Billing interval<select disabled={busy} value={form.cadence} onChange={e => field('cadence', e.target.value)}><option value="">Unknown / not recurring</option>{['weekly', 'monthly', 'quarterly', 'yearly'].map(k => <option key={k}>{k}</option>)}</select></label>
      <label>Next renewal, if known<input type="date" disabled={busy} value={form.renewal_date} onChange={e => field('renewal_date', e.target.value)} /></label>
      <label>Ledger status<select disabled={busy} value={form.state} onChange={e => field('state', e.target.value)}><option value="recorded">Record entry</option><option value="excluded">Exclude from totals</option></select></label>
      <button type="submit" className="send-btn" disabled={busy}>Save entry</button><button type="button" disabled={busy} onClick={() => setForm(null)}>Cancel</button>
    </form>}{error && <p role="alert">{error}</p>}
  </article>;
}

export default function BudgetSettings({ profiles = [], onOpenConversation, onSaved }) {
  const [view, setView] = useState(null), [form, setForm] = useState(initial), [ledger, setLedger] = useState(null);
  const [month, setMonth] = useState(() => { const now = new Date(); return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`; });
  const [profile, setProfile] = useState(''), [model, setModel] = useState(''), [created, setCreated] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [reload, setReload] = useState(0);
  const accept = value => {
    if (!value?.settings || typeof value.settings.enabled !== 'boolean' || !Array.isArray(value.bots)
      || value.bots.some(b => !b || typeof b.id !== 'string' || typeof b.name !== 'string' || typeof b.model !== 'string')
      || ![3600, 7200, 21600, 86400].includes(value.settings.interval_seconds)
      || Object.keys(initial).some(k => typeof value.settings[k] !== typeof initial[k])) throw new Error('Budget settings could not be loaded.');
    setView(value); setForm({ ...Object.fromEntries(Object.keys(initial).map(k => [k, value.settings[k]])),
      ...(!value.configured ? { time_zone: initial.time_zone } : {}) });
  };
  useEffect(() => {
    let alive = true;
    budgetRequest().then(v => { if (alive) accept(v); }).catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [reload]);
  useEffect(() => {
    let alive = true;
    setLedger(null);
    budgetRequest(`/ledger?month=${encodeURIComponent(month)}`).then(v => { const valid = checkedLedger(v); if (alive) setLedger(valid); }).catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [month, reload]);
  const refresh = async () => setLedger(checkedLedger(await budgetRequest(`/ledger?month=${encodeURIComponent(month)}`)));
  const create = async event => {
    event.preventDefault(); if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const bot = created ? { id: created } : await createBot({ ...view.bot_template, providerProfileId: profile, model });
      setCreated(bot.id);
      await updateBotStatus(bot.id, 'running');
      const next = await budgetRequest(); accept(next); setForm(f => ({ ...f, bot_id: bot.id }));
      setNotice('Budget Bot created. Enable tracking and save below to start watching new emails.'); onSaved?.();
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  const save = async event => {
    event.preventDefault(); if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { accept(await budgetRequest('', 'PUT', form)); await refresh(); setNotice(form.enabled ? 'Budget tracking saved. New emails will be checked automatically.' : 'Budget tracking paused. Your saved ledger is still available.'); }
    catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  const chosenProfile = profiles.find(p => p.id === profile);
  const selectedBot = view?.bots.find(b => b.id === form.bot_id);
  return <section className="email-automation-settings budget-settings" aria-label="Budget Bot">
    <div className="email-automation-heading"><div><h3>Budget Bot</h3><p>Track purchases and subscriptions from new email receipts.</p></div>
      <span className="email-automation-status">{view ? view.settings.enabled ? 'Enabled' : 'Paused' : 'Loading…'}</span></div>
    {view && <>
      {!view.bots.length && <form className="email-automation-form" onSubmit={create}>
        <p>Create a dedicated Bot using one of your saved models.</p>
        <label>Provider<select required value={profile} disabled={busy} onChange={e => { setProfile(e.target.value); setModel(''); }}><option value="">Choose provider</option>{profiles.map(p => <option key={p.id} value={p.id}>{p.name || p.id}</option>)}</select></label>
        <label>Model<select required value={model} disabled={busy} onChange={e => setModel(e.target.value)}><option value="">Choose model</option>{(chosenProfile?.models || []).map(m => <option key={m}>{m}</option>)}</select></label>
        <button type="submit" className="send-btn" disabled={busy}>{created ? 'Start created Budget Bot' : 'Create Budget Bot'}</button>
      </form>}
      <form className="email-automation-form" onSubmit={save}>
        <label><span><input type="checkbox" checked={form.enabled} disabled={busy} onChange={e => setForm({ ...form, enabled: e.target.checked })} /> Enable purchase and subscription tracking</span></label>
        <label>Budget Bot<select value={form.bot_id} disabled={busy} onChange={e => setForm({ ...form, bot_id: e.target.value })}><option value="">Choose a Budget Bot</option>{view.bots.map(b => <option key={b.id} value={b.id}>{b.name} · {b.model}{b.status && b.status !== 'running' ? ` · ${b.status}` : ''}</option>)}</select></label>
        {selectedBot?.status && selectedBot.status !== 'running' && <button type="button" disabled={busy} onClick={startSelected}>Start selected Budget Bot</button>}
        <label>Check frequency<select disabled={busy} value={form.interval_seconds} onChange={e => setForm({ ...form, interval_seconds: Number(e.target.value) })}><option value={3600}>Hourly</option><option value={7200}>Every 2 hours</option><option value={21600}>Every 6 hours</option><option value={86400}>Daily</option></select></label>
        <label>Unqualified $ amounts mean<select disabled={busy} value={form.dollar_currency} onChange={e => setForm({ ...form, dollar_currency: e.target.value })}>{['USD', 'CAD', 'AUD'].map(k => <option key={k}>{k}</option>)}</select></label>
        <label>Timezone<input required maxLength={64} disabled={busy} value={form.time_zone} onChange={e => setForm({ ...form, time_zone: e.target.value })} /></label>
        <p className="email-automation-note">Uses your selected email accounts. Enabling, resuming or changing inboxes starts from that moment; no backlog is imported. Multiple inboxes rotate between checks.</p>
        <p className="email-automation-note">Receipt-related email text is sent to {selectedBot ? `${selectedBot.name}’s model (${selectedBot.model})` : 'your selected Bot’s model'}. Kyrex saves quoted evidence and ledger fields in encrypted storage. This is email-based tracking, so purchases without matching receipts may be missing.</p>
        <button type="submit" className="send-btn" disabled={busy}>Save budget tracking</button>
      </form>
      {view.configured && view.conversation_ready !== false && onOpenConversation && <button type="button" disabled={busy} onClick={() => onOpenConversation(view.conversation_id)}>Open Budget Bot</button>}
      {view.settings.last_scan && <p>Last check: {new Date(view.settings.last_scan * 1000).toLocaleString()}</p>}
      {view.settings.detail && <p role="status">{view.settings.detail}</p>}
    </>}
    <details className="budget-ledger"><summary>Spending and subscriptions</summary>
      <label>Month<input aria-label="Budget month" type="month" value={month} onChange={e => { if (e.target.value) setMonth(e.target.value); }} /></label>
      <button type="button" disabled={busy} onClick={() => { setError(''); setReload(n => n + 1); }}>Refresh budget</button>
      {ledger && <>
        <p>Recorded spending minus refunds: {Object.entries(ledger.totals_minor).map(([currency, value]) => amount(value, currency)).join(' · ') || 'No recorded charges for this month.'}</p>
        <p>{ledger.review_count} entry/entries need review. Upcoming renewals and trials are excluded from spending.</p>
        {ledger.categories.length > 0 && <ul>{ledger.categories.map(c => <li key={`${c.currency}:${c.category}`}>{c.category}: {amount(c.amount_minor, c.currency)}</li>)}</ul>}
        <h4>Observed subscriptions</h4>
        {ledger.subscriptions.length ? <ul>{ledger.subscriptions.map(s => <li key={s.id}>{s.merchant} · {label(s.kind)} · {amount(s.amount_minor, s.currency)}{s.cadence && ` · ${s.cadence}`}{s.renewal_date && ` · next renewal ${s.renewal_date}`}</li>)}</ul> : <p>No subscriptions recorded yet.</p>}
        <h4>Email records</h4>
        {ledger.entries.map(e => <Entry key={`${e.id}:${e.revision}`} entry={e} refresh={refresh} />)}
        {(ledger.entries_truncated || ledger.subscriptions_truncated) && <p>Only the first 200 records are shown. Monthly totals include all recorded entries.</p>}
        <p className="email-automation-note">{ledger.limitation}</p>
      </>}
    </details>
    {notice && <p role="status">{notice}</p>}{error && <p role="alert" className="message-error">{error}</p>}
  </section>;
}
