import React, { useEffect, useState } from 'react';
import { budgetRequest, createBot, updateBotStatus } from '../lib/api.js';

const request = (path = '', method = 'GET', data) => budgetRequest(`/bank${path}`, method, data);
const money = (value, currency) => new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(value / 100);
const localDay = () => {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
};
const fields = view => ({ enabled: view.enabled, bot_id: view.bot_id, selected: view.selected,
  start_date: view.start_date || localDay(), starting_balance: view.starting_balance,
  currency: view.currency, time_zone: view.configured ? view.time_zone : Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  interval_seconds: view.interval_seconds });
function checked(value) {
  if (!value || typeof value.available !== 'boolean' || typeof value.enabled !== 'boolean'
      || typeof value.private_mode !== 'boolean' || typeof value.state !== 'string'
      || !Array.isArray(value.accounts) || !Array.isArray(value.selected) || !Array.isArray(value.bots)
      || value.accounts.some(a => !a || !/^[a-f0-9]{64}$/.test(a.ref) || typeof a.label !== 'string')
      || typeof value.starting_balance !== 'string') throw new Error('Bank settings could not be loaded.');
  return value;
}

export default function BankBudgetSettings({ profiles = [], onOpenConversation, onSaved, onMode }) {
  const [view, setView] = useState(null), [form, setForm] = useState(null);
  const [profile, setProfile] = useState(''), [model, setModel] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [totals, setTotals] = useState(null), [disconnecting, setDisconnecting] = useState(false);
  const [month, setMonth] = useState(localDay().slice(0, 7));
  const accept = data => {
    const value = checked(data);
    setView(value); setForm(fields(value)); onMode?.(value.private_mode);
    return value;
  };
  useEffect(() => {
    let alive = true;
    request().then(value => { if (alive) accept(value); }).catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, []);
  const perform = async fn => {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { await fn(); } catch (e) {
      setError(e.message);
      // A failed exchange/revocation can still change saved connection state.
      try { accept(await request()); } catch { /* Keep the original error. */ }
    }
    finally { setBusy(false); }
  };
  const finish = async flow => {
    accept(await request('/finish', 'POST', { flow }));
    const url = new URL(window.location.href);
    url.searchParams.delete('bank_link');
    window.history.replaceState(null, '', url.pathname + url.search + url.hash);
    setNotice('Bank connected. Choose accounts, a starting date and Budget Bot, then save to begin.');
  };
  useEffect(() => {
    const flow = new URLSearchParams(window.location.search).get('bank_link');
    if (!view || !flow || view.flow !== flow || view.state !== 'linking') return;
    perform(() => finish(flow));
  }, [view?.flow]);
  const loadTotals = async () => {
    const value = await request(`/totals?month=${encodeURIComponent(month)}`);
    if (value?.privacy !== 'category_totals_only' || !Array.isArray(value.categories)
        || !value.categories.every(c => Number.isSafeInteger(c.amount_minor) && typeof c.currency === 'string')) {
      throw new Error('Private budget totals could not be loaded.');
    }
    setTotals(value);
  };
  const save = event => {
    event.preventDefault();
    perform(async () => {
      accept(await request('', 'PUT', { ...form, revision: view.revision }));
      setTotals(null);
      setNotice(form.enabled ? 'Private bank tracking saved. The worker will sync automatically.' : 'Bank tracking paused. Saved totals remain available.');
      onSaved?.();
    });
  };
  const create = event => {
    event.preventDefault();
    perform(async () => {
      const bot = await createBot({ ...view.bot_template, providerProfileId: profile, model });
      // Creating a Bot does not enable bank collection or connect a bank.
      await updateBotStatus(bot.id, 'running');
      accept(await request());
      setForm(value => ({ ...value, bot_id: bot.id }));
      setNotice('Budget Bot created. Save private bank tracking below when ready.'); onSaved?.();
    });
  };
  const change = (key, value) => setForm(old => ({ ...old, [key]: value }));
  const selectedBot = view?.bots.find(b => b.id === form?.bot_id);
  return <section className="email-automation-settings budget-settings" aria-label="Private bank budget">
    <div className="email-automation-heading"><div><h3>Private bank budget</h3>
      <p>Plaid transactions, filtered by Kyrex before reaching Budget Bot.</p></div>
      <span className="email-automation-status">{view ? view.enabled ? 'Tracking' : view.state === 'connected' ? 'Paused' : view.state.replaceAll('_', ' ') : 'Loading…'}</span></div>
    <p>Budget Bot receives category and daily totals plus your optional starting balance estimate. Merchant names, locations, account numbers and bank-provided balances are excluded from its model input.</p>
    <p>Plaid and Kyrex’s backend still handle financial data. This connection cannot pay or transfer money. Bank updates may arrive only a few times a day.</p>
    {view && <>
      {!view.available && <p role="status">Bank connection setup is needed: configure Plaid credentials and the HTTPS Kyrex home URL in deployment settings. Never paste bank passwords or API secrets into chat.</p>}
      {view.environment === 'sandbox' && <p role="status">Sandbox mode uses test banks. It does not connect your real bank.</p>}
      {view.state === 'disconnected' && <button type="button" disabled={busy || !view.available} onClick={() => perform(async () => {
        const result = await request('/link', 'POST');
        const url = new URL(result.url);
        if (url.protocol !== 'https:' || url.host !== 'secure.plaid.com' || !url.pathname.startsWith('/hl/')) throw new Error('Bank sign-in link could not be verified.');
        window.location.assign(url.href);
      })}>Connect bank with Plaid</button>}
      {['linking', 'exchanged'].includes(view.state) && <button type="button" disabled={busy} onClick={() => perform(() => finish(view.flow))}>Check completed bank sign-in</button>}
      {view.state === 'linking' && <button type="button" disabled={busy || !view.available} onClick={() => perform(async () => {
        const result = await request('/link', 'POST');
        const url = new URL(result.url);
        if (url.protocol !== 'https:' || url.host !== 'secure.plaid.com' || !url.pathname.startsWith('/hl/')) throw new Error('Bank sign-in link could not be verified.');
        window.location.assign(url.href);
      })}>Continue bank sign-in</button>}
      {view.enabled && <button type="button" disabled={busy} onClick={() => perform(async () => {
        accept(await request('/pause', 'POST', { revision: view.revision })); setTotals(null);
        setNotice('Bank tracking paused.');
      })}>Pause bank tracking</button>}
      {view.state !== 'disconnected' && !disconnecting && <button type="button" disabled={busy} onClick={() => setDisconnecting(true)}>Disconnect bank</button>}
      {disconnecting && <div role="group" aria-label="Confirm bank disconnect"><p>Stop tracking, clear local bank records and revoke Kyrex’s Plaid connection? Existing chat replies and backups are separate.</p>
        {view.state === 'exchange_unknown' && <p>First revoke Kyrex in the Plaid Portal: the sign-in exchange outcome could not be verified.</p>}
        <button type="button" disabled={busy} onClick={() => perform(async () => {
          accept(await request('/disconnect', 'POST', { revision: view.revision }));
          setTotals(null); setDisconnecting(false); onSaved?.();
        })}>Disconnect and clear bank records</button>
        <button type="button" disabled={busy} onClick={() => setDisconnecting(false)}>Keep connection</button></div>}
      {view.state === 'connected' && form && <>
        {!view.bots.length && <form className="email-automation-form" onSubmit={create}>
          <p>Create your Budget Bot using a saved provider. Choose a model/provider whose financial-data retention terms you accept.</p>
          <label>Budget provider<select required disabled={busy} value={profile} onChange={e => { setProfile(e.target.value); setModel(''); }}><option value="">Choose provider</option>{profiles.map(p => <option key={p.id} value={p.id}>{p.name || p.id}</option>)}</select></label>
          <label>Budget model<select required disabled={busy} value={model} onChange={e => setModel(e.target.value)}><option value="">Choose model</option>{(profiles.find(p => p.id === profile)?.models || []).map(m => <option key={m}>{m}</option>)}</select></label>
          <button type="submit" disabled={busy}>Create private Budget Bot</button>
        </form>}
        <form className="email-automation-form" onSubmit={save}>
          <label>Private Budget Bot<select required disabled={busy} value={form.bot_id} onChange={e => change('bot_id', e.target.value)}><option value="">Choose your Budget Bot</option>{view.bots.map(b => <option key={b.id} value={b.id}>{b.name} · {b.model} · {b.status}</option>)}</select></label>
          {selectedBot && selectedBot.status !== 'running' && <button type="button" disabled={busy} onClick={() => perform(async () => { await updateBotStatus(selectedBot.id, 'running'); accept(await request()); onSaved?.(); })}>Start private Budget Bot</button>}
          <fieldset disabled={busy}><legend>Accounts to include</legend>{view.accounts.map(account => <label key={account.ref}><span><input type="checkbox" checked={form.selected.includes(account.ref)} onChange={e => change('selected', e.target.checked ? [...form.selected, account.ref] : form.selected.filter(ref => ref !== account.ref))} /> {account.label}</span></label>)}</fieldset>
          <label>Start date<input type="date" required disabled={busy} min={view.earliest_start_date || undefined} max={localDay()} value={form.start_date} onChange={e => change('start_date', e.target.value)} /></label>
          <label>Starting cash balance (optional)<input inputMode="decimal" disabled={busy} maxLength={32} value={form.starting_balance} onChange={e => change('starting_balance', e.target.value)} /></label>
          <p>Enter the combined balance of selected checking/savings accounts at the beginning of the start date. Card balances are excluded. For first setup, choose a date within the past 30 days; available history varies by bank.</p>
          <label>Balance currency<select disabled={busy} value={form.currency} onChange={e => change('currency', e.target.value)}>{['USD', 'CAD', 'EUR', 'GBP', 'AUD'].map(c => <option key={c}>{c}</option>)}</select></label>
          <label>Budget timezone<input required disabled={busy} value={form.time_zone} onChange={e => change('time_zone', e.target.value)} /></label>
          <label>Bank check frequency<select disabled={busy} value={form.interval_seconds} onChange={e => change('interval_seconds', Number(e.target.value))}>{[[3600, 'Hourly'], [7200, 'Every 2 hours'], [21600, 'Every 6 hours'], [86400, 'Daily']].map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></label>
          <label><span><input type="checkbox" disabled={busy} checked={form.enabled} onChange={e => change('enabled', e.target.checked)} /> Enable private bank tracking</span></label>
          <p>Saving switches the selected Bot to budget totals only and pauses email receipt scanning. Previous receipt replies remain visible, but are excluded from private Budget Bot model requests. Data already sent to a provider cannot be recalled.</p>
          <button type="submit" disabled={busy}>Save private bank tracking</button>
        </form>
      </>}
      {view.configured && onOpenConversation && <button type="button" disabled={busy} onClick={() => onOpenConversation(view.conversation_id)}>Open private Budget Bot</button>}
      {view.last_scan && <p>Last bank sync: {new Date(view.last_scan * 1000).toLocaleString()}</p>}
      {view.detail && <p role="status">{view.detail}</p>}
      {view.configured && <details><summary>Private budget totals</summary>
        <label>Bank budget month<input type="month" value={month} onChange={e => { setMonth(e.target.value); setTotals(null); }} /></label>
        <button type="button" disabled={busy} onClick={() => perform(loadTotals)}>Refresh private totals</button>
        {totals && <>
          {!totals.history_loaded && <p role="status">Bank history is still loading. These totals may be incomplete.</p>}
          {!totals.latest_attempt_ok && <p role="status">The latest check has not completed successfully. Saved totals may be stale.</p>}
          {!totals.enabled && <p>Tracking is paused. Totals reflect the last saved sync.</p>}
          <ul>{totals.categories.map(c => <li key={`${c.currency}:${c.category}`}>{c.category.replaceAll('_', ' ')}: {money(c.category === 'income' ? -c.amount_minor : c.amount_minor, c.currency)}</li>)}</ul>
          <p>{totals.pending_count} pending transaction(s) excluded.</p>
          {totals.uncategorized_count > 0 && <p>{totals.uncategorized_count} posted transaction(s) use “other” across the tracking period. Category errors can affect these estimates.</p>}
          {totals.cash_estimate && <p>Estimated cash remaining: {money(totals.cash_estimate.estimated_minor, totals.cash_estimate.currency)}</p>}
          <p>{totals.limitation}</p>
        </>}
      </details>}
    </>}
    {notice && <p role="status">{notice}</p>}{error && <p role="alert">{error}</p>}
  </section>;
}
