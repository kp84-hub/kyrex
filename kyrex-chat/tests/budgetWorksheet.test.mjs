import assert from 'node:assert/strict';
import test from 'node:test';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import BudgetWorksheet from '../src/components/BudgetWorksheet.jsx';

test('the worksheet renders the projection and saves a row through the existing budget API', async () => {
  const calls = [];
  const today = '2026-11-20';
  const row = { id: 'row1', title: 'Internet', amount_minor: 8000, kind: 'expense',
    date: today, repeat: 'monthly', next_due: today, active: true, last_settled: null };
  let view = { revision: 'abc', currency: 'USD', today, source: 'manual', current_minor: 50000,
    balance: { date: today, amount_minor: 50000 }, before_income_minor: null, next_income: null,
    low_minor: 42000, low_date: today, rows: [row], chart: [
      { date: today, income: 0, expense: 8000, balance_minor: 42000 },
      { date: '2026-11-21', income: 0, expense: 0, balance_minor: 42000 },
    ] };
  globalThis.fetch = async (url, options) => {
    calls.push({ url, method: options.method, body: options.body && JSON.parse(options.body) });
    if (options.method === 'PATCH') {
      view = { ...view, revision: 'def', rows: [{ ...row, amount_minor: 9000 }] };
    }
    return { ok: true, json: async () => view };
  };
  const node = document.body.appendChild(document.createElement('div'));
  const root = createRoot(node);
  const button = label => [...node.querySelectorAll('button')].find(el => (el.getAttribute('aria-label') || el.textContent.trim()) === label);
  try {
    await act(async () => { root.render(React.createElement(BudgetWorksheet, { refreshing: false })); });
    assert.ok(node.querySelector('svg[role="img"] polyline'));
    assert.match(node.textContent, /\$420\.00/);
    await act(async () => { button('Edit Internet').click(); });
    const amount = node.querySelector('input[type="number"]');
    await act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(amount, '90.00');
      amount.dispatchEvent(new window.Event('input', { bubbles: true }));
    });
    await act(async () => { button('Save row').click(); });
    assert.deepEqual(calls.at(-1), { url: '/api/automations/budget/worksheet', method: 'PATCH',
      body: { op: 'save', id: 'row1', title: 'Internet', amount: '90.00', kind: 'expense',
        date: today, repeat: 'monthly', revision: 'abc' } });
    assert.match(node.textContent, /\$90\.00/);
    await act(async () => { root.render(React.createElement(BudgetWorksheet, { refreshing: true })); });
    await act(async () => { root.render(React.createElement(BudgetWorksheet, { refreshing: false })); });
    assert.equal(calls.at(-1).method, 'GET');
  } finally {
    await act(async () => { root.unmount(); });
    node.remove();
  }
});
