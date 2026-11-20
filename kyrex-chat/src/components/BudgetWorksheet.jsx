import React, { useEffect, useRef, useState } from 'react';
import { budgetRequest } from '../lib/api.js';
import './BudgetWorksheet.css';

const request = (method, body) => budgetRequest('/worksheet', method, body);
const frequencies = { once: 'Once', weekly: 'Weekly', biweekly: 'Every two weeks', monthly: 'Monthly' };
const decimal = cents => (cents / 100).toFixed(2);
const shortDate = value => value
  ? new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(new Date(value + 'T00:00:00Z'))
  : '—';

function BalanceChart({ view, money }) {
  const [day, setDay] = useState(0);
  if (view.current_minor === null) return null;
  const days = view.chart;
  const balances = days.map(item => item.balance_minor);
  const min = Math.min(0, view.low_minor, ...balances);
  const max = Math.max(min + 100, view.current_minor, ...balances);
  const x = index => 14 + index / (days.length - 1) * 572;
  const y = value => 126 - (value - min) / (max - min) * 106;
  const points = balances.map((value, i) => `${x(i)},${y(value)}`).join(' ');
  const selected = days[day] || days[0];

  return <section className="worksheet-chart" aria-label="Balance forecast">
    <div className="worksheet-chart-heading">
      <h3>30-day forecast</h3>
      <span>{shortDate(selected.date)} · {money(selected.balance_minor)}</span>
    </div>
    <svg viewBox="0 0 600 140" role="img" aria-label="Projected end-of-day balance over the next 30 days">
      {[0, 0.5, 1].map(ratio => {
        const line = y(min + (max - min) * ratio);
        return <line key={ratio} x1="14" x2="586" y1={line} y2={line} className="worksheet-grid-line" />;
      })}
      {min < 0 && <line x1="14" x2="586" y1={y(0)} y2={y(0)} className="worksheet-zero-line" />}
      <polygon points={`14,126 ${points} 586,126`} className="worksheet-chart-fill" />
      <polyline points={points} className="worksheet-chart-line" />
      <circle cx={x(day)} cy={y(selected.balance_minor)} r="4" className="worksheet-chart-dot" />
    </svg>
    <input type="range" min={0} max={days.length - 1} value={day} onChange={e => setDay(Number(e.target.value))}
      aria-label="Forecast date" aria-valuetext={`${shortDate(selected.date)}, ${money(selected.balance_minor)}`} />
    <div className="worksheet-chart-dates"><span>{shortDate(days[0].date)}</span><span>{shortDate(days.at(-1).date)}</span></div>
    {(selected.expense > 0 || selected.income > 0) && <p className="worksheet-hint">
      {selected.expense > 0 && `Bills ${money(selected.expense)}`}{selected.expense > 0 && selected.income > 0 && ' · '}
      {selected.income > 0 && `Income ${money(selected.income)}`}
    </p>}
  </section>;
}

export default function BudgetWorksheet({ refreshing }) {
  const [view, setView] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [row, setRow] = useState(null);
  const [starting, setStarting] = useState(null);
  const edits = useRef(0);
  useEffect(() => {
    if (refreshing) return;
    let active = true;
    const edit = edits.current;
    request().then(value => { if (active && edit === edits.current) { setView(value); setError(''); } })
      .catch(e => { if (active && edit === edits.current) setError(e.message); });
    return () => { active = false; };
  }, [refreshing]);

  async function apply(action, done) {
    if (busy) return;
    edits.current += 1;
    setBusy(true);
    setError('');
    try {
      const value = await request('PATCH', { ...action, revision: view.revision });
      edits.current += 1;
      setView(value);
      done?.();
    } catch (e) {
      edits.current += 1;
      setError(e.message);
      try { setView(await request()); } catch { /* Keep the original error. */ }
    } finally {
      setBusy(false);
    }
  }

  if (!view) return <section className="budget-worksheet" aria-label="Budget worksheet">
    {error ? <p role="alert">{error}</p> : <p role="status">Loading worksheet…</p>}
  </section>;
  const format = new Intl.NumberFormat(undefined, { style: 'currency', currency: view.currency });
  const money = value => value === null ? '—' : format.format(value / 100);
  const rows = [...view.rows].sort((a, b) => (a.next_due || '9999').localeCompare(b.next_due || '9999')
    || a.title.localeCompare(b.title));
  const openRow = item => setRow(item
    ? { id: item.id, title: item.title, amount: decimal(item.amount_minor), kind: item.kind, date: item.date, repeat: item.repeat }
    : { title: '', amount: '', kind: 'expense', date: view.today, repeat: 'once' });
  const openBalance = () => setStarting(view.balance
    ? { amount: decimal(view.balance.amount_minor), date: view.balance.date }
    : { amount: '', date: view.today });

  return <section className="budget-worksheet" aria-label="Budget worksheet">
    <div className="worksheet-top">
      <div><h2>Budget worksheet</h2><p className="worksheet-hint">{view.currency} · {shortDate(view.today)}</p></div>
      <button type="button" className="connection-btn" onClick={openBalance}>Starting balance</button>
    </div>
    {error && <p role="alert" className="message-error">{error}</p>}
    <div className="worksheet-metrics">
      {[
        [view.source === 'bank' ? 'Estimated cash' : 'Current balance', view.current_minor,
          view.source === 'bank' ? (view.bank_environment === 'sandbox' ? 'Plaid test data' : 'Saved bank totals') : 'Manual cash tracking'],
        ['Before next income', view.before_income_minor, view.next_income ? shortDate(view.next_income) : 'No income scheduled in 30 days'],
        ['Lowest projected balance', view.low_minor, view.low_date ? shortDate(view.low_date) : 'Next 30 days'],
      ].map(([label, value, detail]) => <div key={label} className="worksheet-metric">
        <span>{label}</span><strong className={value < 0 ? 'worksheet-negative' : ''}>{money(value)}</strong><small>{detail}</small>
      </div>)}
    </div>
    {view.source === 'none' && <p className="worksheet-hint">Enter a starting balance to see your cash forecast.</p>}
    {view.source === 'manual' && <p className="worksheet-hint">
      This balance uses your manual starting amount and worksheet settlements since {shortDate(view.balance.date)}.
    </p>}
    {view.source === 'bank' && <p className="worksheet-hint">
      {view.bank_last_scan && `Bank totals saved ${new Date(view.bank_last_scan * 1000).toLocaleString()}. `}
      {view.bank_stale && 'Bank totals may be stale or incomplete. '}
      Mark a row settled after it appears in the posted bank totals.
    </p>}
    {starting && <form className="worksheet-form" onSubmit={e => {
      e.preventDefault(); apply({ op: 'balance', ...starting }, () => setStarting(null));
    }}>
      <label>Cash at start of day<input required type="number" step="0.01" inputMode="decimal" value={starting.amount}
        onChange={e => setStarting({ ...starting, amount: e.target.value })} /></label>
      <label>As of<input required type="date" max={view.today} value={starting.date}
        onChange={e => setStarting({ ...starting, date: e.target.value })} /></label>
      <div className="worksheet-actions">
        <button className="send-btn" disabled={busy}>Save balance</button>
        <button type="button" className="connection-btn" onClick={() => setStarting(null)}>Cancel</button>
        {view.balance && <button type="button" className="connection-btn" disabled={busy}
          onClick={() => apply({ op: 'clear_balance' }, () => setStarting(null))}>Clear manual balance</button>}
      </div>
    </form>}
    <BalanceChart view={view} money={money} />
    <div className="worksheet-top">
      <h3>Bills and income</h3>
      <button type="button" className="connection-btn" onClick={() => openRow()}>Add row</button>
    </div>
    {row && <form className="worksheet-form" onSubmit={e => {
      e.preventDefault(); apply({ op: 'save', ...row }, () => setRow(null));
    }}>
      <label>Item<input required maxLength="80" value={row.title}
        onChange={e => setRow({ ...row, title: e.target.value })} /></label>
      <label>Amount<input required type="number" min="0.01" step="0.01" inputMode="decimal" value={row.amount}
        onChange={e => setRow({ ...row, amount: e.target.value })} /></label>
      <label>Type<select value={row.kind} onChange={e => setRow({ ...row, kind: e.target.value })}>
        <option value="expense">Bill</option><option value="income">Income</option>
      </select></label>
      <label>First due date<input required type="date" value={row.date}
        onChange={e => setRow({ ...row, date: e.target.value })} /></label>
      <label>Repeats<select value={row.repeat} onChange={e => setRow({ ...row, repeat: e.target.value })}>
        {Object.entries(frequencies).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <div className="worksheet-actions">
        <button className="send-btn" disabled={busy}>Save row</button>
        <button type="button" className="connection-btn" onClick={() => setRow(null)}>Cancel</button>
      </div>
    </form>}
    {!rows.length && <p className="worksheet-hint">Add your first bill or payday.</p>}
    {rows.length > 0 && <div className="worksheet-table-wrap">
      <table className="worksheet-table">
        <thead><tr><th scope="col">Item</th><th scope="col">Next due</th><th scope="col">Amount</th>
          <th scope="col">Projected balance</th><th scope="col">Actions</th></tr></thead>
        <tbody>{rows.map(item => {
          const overdue = item.next_due && item.next_due < view.today;
          const projectedDate = overdue && item.kind === 'expense' ? view.today : item.next_due;
          const day = !overdue || item.kind === 'expense'
            ? view.chart.find(day => day.date === projectedDate) : null;
          return <tr key={item.id}>
            <th scope="row">{item.title}<small>{frequencies[item.repeat]}</small></th>
            <td>{item.next_due ? shortDate(item.next_due) : item.active ? 'Settled' : 'Paused'}
              {overdue && <small className="worksheet-negative">Overdue</small>}</td>
            <td className={item.kind === 'income' ? 'worksheet-income' : ''}>
              {item.kind === 'income' ? '+' : '−'}{money(item.amount_minor)}</td>
            <td>{money(day?.balance_minor ?? null)}</td>
            <td><div className="worksheet-actions">
              <button type="button" onClick={() => openRow(item)} aria-label={`Edit ${item.title}`}>Edit</button>
              {item.next_due && <button type="button" disabled={busy}
                onClick={() => apply({ op: 'settle', id: item.id, date: item.next_due })}>
                {item.kind === 'income' ? 'Received' : 'Paid'}
              </button>}
              {item.last_settled && <button type="button" disabled={busy}
                onClick={() => apply({ op: 'undo', id: item.id, date: item.last_settled })}>Undo settlement</button>}
              <button type="button" disabled={busy} onClick={() => apply({ op: item.active ? 'stop' : 'resume', id: item.id })}>
                {item.active ? 'Remove from forecast' : 'Restore'}</button>
            </div></td>
          </tr>;
        })}</tbody>
      </table>
    </div>}
    <p className="worksheet-hint">Overdue bills are included today. Overdue income is excluded until recorded or rescheduled. Same-day bills are applied before income.</p>
  </section>;
}
