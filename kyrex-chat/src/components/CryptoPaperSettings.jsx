import React, { useEffect, useState } from 'react';
import { accountRequest, createBot, updateBotStatus } from '../lib/api.js';

const endpoint = '/api/automations/crypto-paper';
const request = (path = '', method = 'GET', body) => accountRequest(endpoint + path, method, body);
const money = value => {
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD' }).format(number) : 'Unavailable';
};
const stamp = value => value == null ? 'No completed checks yet' : new Date(value * 1000).toLocaleString();
function checked(value) {
  if (value?.mode !== 'paper' || typeof value.settings?.enabled !== 'boolean' || !Array.isArray(value.bots)
    || !Array.isArray(value.ticks) || !value.metrics || !value.bot_template
    || ![900, 3600, 14400].includes(value.settings.interval_seconds)
    || !Array.isArray(value.available_symbols) || !value.available_symbols.length
    || value.available_symbols.some(s => typeof s !== 'string')
    || !Array.isArray(value.settings.symbols) || !value.settings.symbols.length
    || new Set(value.settings.symbols).size !== value.settings.symbols.length
    || value.settings.symbols.some(s => !value.available_symbols.includes(s))
    || ['cash_usd', 'equity_usd', 'pnl_usd', 'benchmark_equity_usd', 'fees_usd', 'max_drawdown_usd'].some(k =>
      typeof value.metrics[k] !== 'string' || !Number.isFinite(Number(value.metrics[k])))
    || typeof value.settings.strategy !== 'string' || typeof value.settings.time_zone !== 'string'
    || !value.metrics.positions || typeof value.metrics.positions !== 'object'
    || Object.values(value.metrics.positions).some(p => !p || !Number.isFinite(Number(p.quantity)) || !Number.isFinite(Number(p.basis)))
    || !Number.isInteger(value.tick_count) || (value.next_offset != null && !Number.isInteger(value.next_offset))
    || value.ticks.some(t => !t || typeof t.task_id !== 'string' || !Number.isFinite(t.created)
      || typeof t.outcome !== 'string' || typeof t.decision?.reason !== 'string'
      || typeof t.model?.model !== 'string' || !Number.isFinite(t.execution_quotes?.retrieved_at)
      || !['buy', 'sell', 'hold'].includes(t.decision.action)
      || (t.fill != null && ['notional_usd','fee_usd','price_usd','quantity'].some(k => !Number.isFinite(Number(t.fill[k])))))) {
    throw new Error('Paper experiment could not be loaded. Refresh to check its saved status.');
  }
  return value;
}

export default function CryptoPaperSettings({ profiles = [], onOpenConversation, onSaved }) {
  const [view, setView] = useState(null), [form, setForm] = useState(null);
  const [profile, setProfile] = useState(''), [model, setModel] = useState(''), [created, setCreated] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const accept = value => {
    checked(value); setView(value);
    const settings = value.settings;
    setForm({ bot_id: settings.bot_id, interval_seconds: settings.interval_seconds, strategy: settings.strategy, symbols: settings.symbols,
      time_zone: value.configured ? settings.time_zone : Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' });
  };
  useEffect(() => {
    let alive = true;
    request().then(value => { if (alive) accept(value); }).catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, []);
  const action = async callback => {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { await callback(); } catch (e) { setError(e.message); } finally { setBusy(false); }
  };
  const refresh = () => action(async () => accept(await request()));
  const create = event => {
    event.preventDefault();
    action(async () => {
      const bot = created ? { id: created } : await createBot({ ...view.bot_template, providerProfileId: profile, model });
      setCreated(bot.id);
      await updateBotStatus(bot.id, 'running');
      accept(await request()); setForm(f => ({ ...f, bot_id: bot.id }));
      setNotice('Crypto Bot created. Start the paper experiment below when ready.'); onSaved?.();
    });
  };
  const save = event => {
    event.preventDefault();
    action(async () => { accept(await request('', 'PUT', form)); setNotice('Paper experiment saved. Simulated checks run in the background.'); onSaved?.(); });
  };
  const pause = () => action(async () => { accept(await request('/pause', 'POST', {})); setNotice('Paper experiment paused. Saved results are retained.'); });
  const selected = view?.bots.find(b => b.id === form?.bot_id);
  return <section className="email-automation-settings crypto-paper-settings" aria-label="Crypto paper trading">
    <div className="email-automation-heading"><div><h3>Crypto paper trading</h3>
      <p>Live public prices. Simulated money. No exchange account or real orders.</p></div>
      <span className="email-automation-status">{view ? view.settings.enabled ? 'Running · Paper' : 'Paused · Paper' : 'Loading…'}</span></div>
    <p>Starts with a simulated $1,000. Choose up to eight coins to watch. Your selected model compares them and chooses one buy, sell or hold per check.</p>
    {view && form && <>
      {!view.bots.length && <form className="email-automation-form" onSubmit={create}>
        <label>Provider<select aria-label="Crypto provider" required disabled={busy} value={profile} onChange={e => { setProfile(e.target.value); setModel(''); }}>
          <option value="">Choose provider</option>{profiles.map(p => <option key={p.id} value={p.id}>{p.name || p.id}</option>)}</select></label>
        <label>Model<select aria-label="Crypto model" required disabled={busy} value={model} onChange={e => setModel(e.target.value)}>
          <option value="">Choose model</option>{(profiles.find(p => p.id === profile)?.models || []).map(m => <option key={m}>{m}</option>)}</select></label>
        <button type="submit" className="send-btn" disabled={busy || !profile || !model}>{created ? 'Start created Crypto Bot' : 'Create Crypto Bot'}</button>
      </form>}
      <form className="email-automation-form" onSubmit={save}>
        <fieldset className="crypto-paper-coins" disabled={busy}>
          <legend>Coins to watch</legend>
          <div>{view.available_symbols.map(symbol => <label key={symbol}>
            <input type="checkbox" checked={form.symbols.includes(symbol)} onChange={e => setForm(f => ({ ...f,
              symbols: e.target.checked ? [...f.symbols, symbol] : f.symbols.filter(s => s !== symbol) }))} />{symbol}
          </label>)}</div>
          <button type="button" onClick={() => setForm(f => ({ ...f, symbols: [...view.available_symbols] }))}>Watch all eight coins</button>
          <p>Save to apply your selection. Removed coins with holdings remain tracked and sellable. The fixed BTC/ETH benchmark stays the same.</p>
          <p>One AI decision per check across the watchlist. More coins add market data to the prompt, which may increase token costs.</p>
        </fieldset>
        <label>Crypto Bot<select required disabled={busy} value={form.bot_id} onChange={e => setForm({ ...form, bot_id: e.target.value })}>
          <option value="">Choose your Crypto Bot</option>{view.bots.map(b => <option key={b.id} value={b.id}>{b.name} · {b.model} · {b.status}</option>)}</select></label>
        {selected && selected.status !== 'running' && <button type="button" disabled={busy} onClick={() => action(async () => {
          const id = form.bot_id; await updateBotStatus(id, 'running'); accept(await request()); setForm(f => ({ ...f, bot_id: id })); onSaved?.();
        })}>Start selected Crypto Bot</button>}
        <label>Check frequency<select disabled={busy} value={form.interval_seconds} onChange={e => setForm({ ...form, interval_seconds: Number(e.target.value) })}>
          <option value={900}>Every 15 minutes · up to 96 model calls/day</option><option value={3600}>Hourly · up to 24 model calls/day</option><option value={14400}>Every 4 hours · up to 6 model calls/day</option></select></label>
        <label>Experiment strategy<textarea required disabled={busy} maxLength={2000} value={form.strategy} onChange={e => setForm({ ...form, strategy: e.target.value })} /></label>
        <label>Daily summary timezone<input required disabled={busy} maxLength={64} value={form.time_zone} onChange={e => setForm({ ...form, time_zone: e.target.value })} /></label>
        <p>Paper limits: $25 per order before costs, $100 per position, $200 daily turnover. A sampled $50 loss pauses new checks; it is not a guaranteed loss cap.</p>
        <p>Assumes 0.4% fees and 0.1% slippage per fill, plus observed spread. Inference charges use your provider plan and are excluded from paper P/L.</p>
        <button type="submit" className="send-btn" disabled={busy || !form.bot_id || !form.symbols.length || selected?.status !== 'running' || view.metrics.auto_halted}>
          {view.settings.enabled ? 'Save paper settings' : 'Start paper experiment'}</button>
      </form>
      <div className="crypto-paper-metrics">
        <p><strong>Simulated equity</strong><span>{money(view.metrics.equity_usd)}</span></p>
        <p><strong>P/L since start</strong><span>{money(view.metrics.pnl_usd)}</span></p>
        <p><strong>Buy-and-hold benchmark</strong><span>{money(view.metrics.benchmark_equity_usd)}</span></p>
        <p><strong>Largest observed decline</strong><span>{money(view.metrics.max_drawdown_usd)}</span></p>
      </div>
      <p>Simulated cash: {money(view.metrics.cash_usd)} · Simulated fees: {money(view.metrics.fees_usd)}</p>
      {Object.entries(view.metrics.positions).map(([symbol, position]) => <p key={symbol}>
        {symbol}: {position.quantity} simulated units · Cost basis including fees: {money(position.basis)}</p>)}
      <p>Last completed check: {stamp(view.metrics.last_tick)}. {view.settings.detail}</p>
      <p>{view.benchmark} {view.limitation}</p>
      {view.settings.conversation_id && <button type="button" disabled={busy} onClick={() => onOpenConversation?.(view.settings.conversation_id)}>Open Crypto Bot chat</button>}
      <p>Daily snapshots arrive in that same Bot chat and your notification inbox after each local date change. Failed checks leave the last completed snapshot visible.</p>
      <h4>Decisions and simulated trades</h4>
      {!view.ticks.length && <p>No decisions recorded yet. Start the paper experiment to begin.</p>}
      {view.history_truncated && <p>Showing {view.ticks.length} of {view.tick_count} saved checks. Older decisions are retained.</p>}
      {view.ticks.map(tick => <article className="budget-entry" key={tick.task_id}>
        <strong>{tick.decision.action.toUpperCase()} {tick.decision.symbol} · {tick.fill ? `${money(tick.fill.notional_usd)} simulated` : 'No fill'}</strong>
        <p>{stamp(tick.created)} · {tick.outcome.replaceAll('_', ' ')}</p><p>{tick.decision.reason}</p>
        <details><summary>Decision details</summary><p>Model: {tick.model.model}. Quotes retrieved: {stamp(tick.execution_quotes.retrieved_at)}.</p>
          {tick.fill && <p>Quantity: {tick.fill.quantity} · Simulated price: {money(tick.fill.price_usd)} · Fee: {money(tick.fill.fee_usd)}</p>}</details>
      </article>)}
      {view.next_offset != null && <button type="button" disabled={busy} onClick={() => action(async () => {
        const page = checked(await request('?offset=' + view.next_offset));
        setView(old => { const seen = new Set(old.ticks.map(t => t.task_id));
          return { ...page, ticks: [...old.ticks, ...page.ticks.filter(t => !seen.has(t.task_id))] }; });
      })}>Earlier decisions</button>}
    </>}
    <div className="crypto-paper-controls"><button type="button" disabled={busy} onClick={refresh}>Refresh paper results</button>
      <button type="button" disabled={busy} onClick={pause}>Pause paper experiment</button></div>
    {notice && <p role="status">{notice}</p>}{error && <p role="alert">{error}</p>}
  </section>;
}
