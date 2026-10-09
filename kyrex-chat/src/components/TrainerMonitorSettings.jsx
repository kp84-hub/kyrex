import React, { useEffect, useState } from 'react';
import { getTrainerMonitor, saveTrainerMonitor, getTrainerAlerts, resetTrainerBaseline,
  resendTrainerAlert, newRequestId } from '../lib/api.js';

const initial = { enabled: false, delivery: 'chat', bot_id: '', interval_seconds: 3600, horizon_days: 14 };
const stamp = seconds => seconds ? new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium', timeStyle: 'short', timeZone: 'America/New_York',
}).format(new Date(seconds * 1000)) + ' ET' : 'Not checked yet';

export default function TrainerMonitorSettings() {
  const [form, setForm] = useState(initial);
  const [status, setStatus] = useState(null);
  const [alerts, setAlerts] = useState([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [notice, setNotice] = useState('');
  const refresh = async (replaceForm = false) => {
    const [view, history] = await Promise.all([getTrainerMonitor(), getTrainerAlerts()]);
    setStatus(view);
    setAlerts(history.alerts || []);
    if (replaceForm) setForm({ ...initial, ...view.settings,
      enabled: Boolean(view.settings.enabled), bot_id: view.settings.bot_id || view.bots?.[0]?.id || '' });
    setLoaded(true);
  };
  useEffect(() => {
    let active = true;
    refresh(true).catch(err => { if (active) setError(err.message); });
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') refresh().catch(() => {});
    }, 15000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  const action = async (work, message) => {
    setBusy(true); setError(''); setNotice('');
    try { await work(); await refresh(); setNotice(message); }
    catch (err) { setError(err.message || 'Could not update trainer monitoring.'); }
    finally { setBusy(false); }
  };
  return <section className="email-automation-settings" aria-label="L6 trainer alerts">
    <div className="email-automation-heading"><div>
      <h3>L6 trainer alerts</h3>
      <p>Check your 8:30 class on Glofox and confirm trainer changes before notifying you.</p>
    </div><span className={`email-automation-status ${status?.service_ready ? 'ready' : ''}`}>
      {status?.service_ready ? 'Monitor available' : 'Monitor not configured'}
    </span></div>
    {!status?.service_ready && <p className="email-automation-note">
      Save your preferences here. The worker must be configured before checks can run.
    </p>}
    <form className="email-automation-form" onSubmit={event => {
      event.preventDefault();
      const { enabled, delivery, bot_id, interval_seconds, horizon_days } = form;
      action(() => saveTrainerMonitor({ enabled, delivery, bot_id, interval_seconds, horizon_days }), 'Preferences saved.');
    }}>
      <label><span>Monitor trainer changes</span><input type="checkbox" checked={form.enabled}
        onChange={event => setForm({ ...form, enabled: event.target.checked })} /></label>
      <label>Calendar Bot<select value={form.bot_id} onChange={event => setForm({ ...form, bot_id: event.target.value })}>
        {!status?.bots?.length && <option value="">Choose a running Calendar Bot</option>}
        {status?.bots?.map(bot => <option value={bot.id} key={bot.id}>{bot.name}</option>)}
      </select></label>
      <label>Notify me in<select value={form.delivery} onChange={event => setForm({ ...form, delivery: event.target.value })}>
        <option value="chat">Kyrex Chat only</option><option value="group">L6 group chat</option>
      </select></label>
      <label>Check every (hours)<input type="number" min="0.25" max="24" step="0.25"
        value={form.interval_seconds / 3600} onChange={event => setForm({ ...form, interval_seconds: Number(event.target.value) * 3600 })} /></label>
      <label>Upcoming days<select value={form.horizon_days} onChange={event => setForm({ ...form, horizon_days: Number(event.target.value) })}>
        <option value="14">14 days</option><option value="7">7 days</option>
      </select></label>
      <button type="submit" className="send-btn" disabled={busy || !loaded || (form.enabled && !form.bot_id)}>Save trainer alerts</button>
    </form>
    {form.delivery === 'group' && <p className="email-automation-note">
      {status?.group_note || 'Uses your existing pinned L6 group. Verify its conversation and Google Messages pairing on the Browser Host.'}
      {' '}An in-flight send may finish after you pause monitoring.
    </p>}
    <p className="email-automation-note">Last check: {stamp(status?.settings?.last_read)}</p>
    {status?.settings?.last_error && <p role="status">{status.settings.last_error}</p>}
    <button type="button" className="bot-configure-btn" disabled={busy || !loaded}
      onClick={() => action(resetTrainerBaseline, 'The next valid read will seed the baseline silently.')}>
      Reset baseline silently
    </button>
    {notice && <p role="status">{notice}</p>}
    {error && <div className="message-error" role="alert">{error}</div>}
    <div className="email-automation-rules" aria-label="Trainer alert history">
      {alerts.length === 0 && <p>No trainer changes recorded yet.</p>}
      {alerts.map(alert => <article className="email-automation-rule" key={alert.id}>
        <div><strong>{alert.message}</strong><span>{alert.day} · Change {alert.version} · {alert.state}</span>
          {alert.detail && <span>{alert.detail}</span>}
          {alert.state === 'unknown' && <span>Check the group before resending; the first send may have arrived.</span>}
        </div>
        {form.delivery === 'group' && ['sent', 'failed', 'unknown'].includes(alert.state)
          && alert.starts_at * 1000 > Date.now() && <button type="button"
            className="bot-configure-btn" disabled={busy} onClick={() => {
              const requestId = newRequestId();
              action(() => resendTrainerAlert(alert.id, requestId), 'Resend queued for a fresh schedule check.');
            }}>Resend (may duplicate)</button>}
      </article>)}
    </div>
  </section>;
}
