import React, { useEffect, useState } from 'react';
import { calendarBriefingRequest, fetchGoogleCalendars } from '../lib/api.js';

export default function CalendarBriefingSettings() {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState(null);
  const [form, setForm] = useState(null);
  const [choices, setChoices] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [calendarError, setCalendarError] = useState('');
  const [saved, setSaved] = useState('');

  useEffect(() => {
    if (!open) return;
    let active = true;
    setError('');
    setCalendarError('');
    calendarBriefingRequest().then(value => {
      if (!active) return;
      if (!value.settings || !Array.isArray(value.settings.calendars)) throw new Error('Could not load Calendar Briefing settings.');
      setView(value);
      setForm(value.settings);
    }).catch(e => { if (active) setError(e.message); });
    fetchGoogleCalendars().then(value => {
      if (active) setChoices(value.calendars);
    }).catch(() => { if (active) setCalendarError('Connect Google Calendar in Connections to choose calendars.'); });
    return () => { active = false; };
  }, [open]);

  function change(key, value) {
    setForm(previous => ({ ...previous, [key]: value }));
    setSaved('');
  }

  async function save(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    setSaved('');
    try {
      const value = await calendarBriefingRequest('PUT', form);
      setView(value);
      setForm(value.settings);
      setSaved(form.enabled ? 'Calendar Briefing enabled.' : 'Calendar Briefing paused.');
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return <details className="email-automation-settings" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><strong>Calendar Briefing</strong>{view && <span className="email-automation-status">{view.settings.enabled ? 'Enabled' : 'Paused'}</span>}</summary>
    {open && <>
      <p>Get a morning agenda in The Overwatcher and alerts for changes in the next 48 hours.</p>
      {!form && !error && <p role="status">Loading Calendar Briefing…</p>}
      {form && <form className="email-automation-form" onSubmit={save}>
        <label><span><input type="checkbox" checked={form.enabled} disabled={busy}
          onChange={e => {
            const enabled = e.target.checked;
            const preferred = choices?.find(row => row.preferred) || choices?.find(row => row.primary);
            setSaved('');
            setForm({ ...form, enabled, calendars: enabled && !form.calendars.length && preferred
              ? [preferred.id] : form.calendars });
          }} /> Enable Calendar Briefing</span></label>
        {form.enabled && <fieldset>
          <legend>Calendars to watch (up to five)</legend>
          {choices?.map(row => <label key={row.id}><span><input type="checkbox"
            checked={form.calendars.includes(row.id)}
            disabled={busy || (!form.calendars.includes(row.id) && form.calendars.length >= 5)}
            onChange={e => change('calendars', e.target.checked
              ? [...form.calendars, row.id] : form.calendars.filter(id => id !== row.id))} /> {row.summary}</span></label>)}
          {calendarError && <p role="status">{calendarError}</p>}
        </fieldset>}
        {[['briefing_at', 'Morning briefing'], ['quiet_from', 'Quiet hours start'], ['quiet_until', 'Quiet hours end']]
          .map(([key, label]) => <label key={key}>{label} <input type="time" required disabled={busy}
            value={form[key]} onChange={e => change(key, e.target.value)} /></label>)}
        <p className="email-automation-note">Times are Eastern. Kyrex checks every 15 minutes while Chat is closed. Set matching quiet times to turn quiet hours off.</p>
        <button type="submit" className="send-btn" disabled={busy || (form.enabled && (!form.calendars.length || !choices?.length))}>
          {busy ? 'Saving…' : 'Save Calendar Briefing'}
        </button>
      </form>}
      {view?.last_scan && <p>Last successful check {new Date(view.last_scan * 1000).toLocaleString()}</p>}
      {view?.detail && <p role="status">{view.detail}</p>}
      {saved && <p role="status">{saved}</p>}
      {error && <p role="alert">{error}</p>}
    </>}
  </details>;
}
