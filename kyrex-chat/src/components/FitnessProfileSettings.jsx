import React, { useEffect, useState } from 'react';
import { getFitnessProfile, saveFitnessProfile, clearFitnessProfile } from '../lib/api.js';

const rounded = (value) => Math.round(value * 100) / 100;
export function profileToForm(profile, units = 'us') {
  const inches = profile.height_cm == null ? null : rounded(profile.height_cm / 2.54);
  return {
    units, age: profile.age ?? '', goal: profile.goal ?? '', usual_activity: profile.usual_activity ?? '',
    feet: inches == null ? '' : Math.floor(inches / 12),
    inches: inches == null ? '' : rounded(inches % 12),
    height: profile.height_cm == null ? '' : rounded(profile.height_cm),
    weight: profile.weight_kg == null ? '' : rounded(profile.weight_kg * (units === 'us' ? 2.2046226218 : 1)),
  };
}

export function formToProfile(form) {
  const profile = {};
  if (form.age !== '') profile.age = Number(form.age);
  if (form.units === 'us') {
    if (form.feet !== '' || form.inches !== '') {
      profile.height_cm = rounded((Number(form.feet || 0) * 12 + Number(form.inches || 0)) * 2.54);
    }
  } else if (form.height !== '') profile.height_cm = Number(form.height);
  if (form.weight !== '') profile.weight_kg = rounded(Number(form.weight) / (form.units === 'us' ? 2.2046226218 : 1));
  if (form.goal) profile.goal = form.goal;
  if (form.usual_activity) profile.usual_activity = form.usual_activity;
  return profile;
}

export default function FitnessProfileSettings() {
  const [form, setForm] = useState(profileToForm({}));
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('');
  useEffect(() => {
    let active = true;
    getFitnessProfile().then((profile) => {
      if (active) { setForm(profileToForm(profile)); setLoaded(true); }
    }).catch((err) => { if (active) setError(err.message); });
    return () => { active = false; };
  }, []);
  const retry = async () => {
    setBusy(true); setError('');
    try { setForm(profileToForm(await getFitnessProfile())); setLoaded(true); }
    catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  const change = (key) => (event) => {
    setForm({ ...form, [key]: event.target.value }); setStatus('');
  };
  const save = async (event) => {
    event.preventDefault(); setBusy(true); setError(''); setStatus('');
    try {
      setForm(profileToForm(await saveFitnessProfile(formToProfile(form)), form.units));
      setStatus('Profile saved. Ask your Workout Bot to review your next workout.');
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  const clear = async () => {
    setBusy(true); setError(''); setStatus('');
    try {
      await clearFitnessProfile(); setForm(profileToForm({}, form.units));
      setStatus('Profile cleared. New fitness reads will use general feedback. Earlier chat messages remain.');
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };
  return (
    <section className="fitness-profile" aria-label="Fitness profile">
      <h3>Fitness profile</h3>
      <p>Help your Workout Bot explain what went well and how to improve toward your goal. Fields are optional; age is for adults 18 and over.</p>
      <p>This profile is shared with your selected model when a fitness tool reads workouts. You can update or clear it anytime.</p>
      <form onSubmit={save}>
        <fieldset disabled={!loaded || busy}>
          <div className="fitness-profile-fields">
            <label>Units<select aria-label="Fitness units" value={form.units} onChange={(event) => {
              setForm(profileToForm(formToProfile(form), event.target.value)); setStatus('');
            }}><option value="us">Feet / inches / pounds</option><option value="metric">Centimeters / kilograms</option></select></label>
            <label>Age (years)<input aria-label="Age (years)" type="number" min="18" max="120" step="1" value={form.age} onChange={change('age')} /></label>
            {form.units === 'us' ? <>
              <label>Height (feet)<input aria-label="Height (feet)" type="number" min="0" max="8" step="1" value={form.feet} onChange={change('feet')} /></label>
              <label>Height (inches)<input aria-label="Height (inches)" type="number" min="0" max="11.99" step="0.01" value={form.inches} onChange={change('inches')} /></label>
            </> : <label>Height (cm)<input aria-label="Height (cm)" type="number" min="80" max="260" step="0.01" value={form.height} onChange={change('height')} /></label>}
            <label>Weight ({form.units === 'us' ? 'lb' : 'kg'})<input aria-label="Weight" type="number" min={form.units === 'us' ? '44.09' : '20'} max={form.units === 'us' ? '1102.32' : '500'} step="0.01" value={form.weight} onChange={change('weight')} /></label>
            <label>Goal<select aria-label="Fitness goal" value={form.goal} onChange={change('goal')}>
              <option value="">Choose a goal (optional)</option><option value="general_fitness">General fitness</option>
              <option value="endurance">Endurance</option><option value="strength">Strength / muscle</option>
              <option value="weight_management">Weight management</option>
            </select></label>
            <label>Usual workout<select aria-label="Usual workout" value={form.usual_activity} onChange={change('usual_activity')}>
              <option value="">Choose an activity (optional)</option><option value="hiit">HIIT / circuits</option>
              <option value="strength">Strength training</option><option value="running">Running</option>
              <option value="cycling">Cycling</option><option value="walking">Walking</option><option value="other">Other</option>
            </select></label>
          </div>
          <div className="fitness-profile-actions">
            <button className="send-btn" type="submit">{busy ? 'Saving…' : 'Save fitness profile'}</button>
            <button className="settings-close" type="button" onClick={clear}>Clear profile</button>
          </div>
        </fieldset>
      </form>
      {!loaded && !error && <p role="status">Loading profile…</p>}
      {error && <div role="alert" className="message-error">{error}</div>}
      {!loaded && error && <button type="button" className="settings-close" disabled={busy} onClick={retry}>Retry profile load</button>}
      {status && <p role="status">{status}</p>}
    </section>
  );
}
