import React, { useEffect, useState } from 'react';
import { onboardingRequest } from '../lib/api.js';
import '../Onboarding.css';

export default function Onboarding({ state, onState, onComplete, onAdvanced }) {
  const [provider, setProvider] = useState(state.providers?.[0]?.id || 'opencode');
  const [savedProfile, setSavedProfile] = useState('');
  const [key, setKey] = useState('');
  const [model, setModel] = useState(state.model || '');
  const [models, setModels] = useState([]), [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [editingProvider, setEditingProvider] = useState(false);
  const step = editingProvider ? 'provider' : state.step;
  useEffect(() => {
    let alive = true;
    if (step === 'model') {
      setModels([]); setNotice('');
      onboardingRequest('models').then(result => { if (alive) { setModels(result.models); setNotice(result.notice || ''); } })
        .catch(e => { if (alive) setNotice('Model discovery is unavailable. Enter the exact model ID from your provider.'); });
    }
    return () => { alive = false; };
  }, [step, state.profile_id]);
  const act = async (action, body) => {
    if (busy) return;
    setBusy(true); setError('');
    try {
      const next = await onboardingRequest(action, body);
      setKey(''); setEditingProvider(false); onState(next);
      if (action === 'provider') setModel('');
      if (action === 'complete') await onComplete(next);
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  };
  const selected = state.profiles?.find(profile => profile.id === state.profile_id);
  return <section className="onboarding" aria-label="Kyrex setup">
    <p className="onboarding-kicker">Welcome to Kyrex</p>
    <h1>{step === 'provider' ? 'Connect your AI provider' : step === 'model' ? 'Choose and test your model' : 'Your connection is ready'}</h1>
    <p>Bring your own provider and model. This setup works before your assistant is connected.</p>
    <ol className="onboarding-steps" aria-label="Setup progress">
      <li aria-current={step === 'provider' ? 'step' : undefined}>Provider</li>
      <li aria-current={step === 'model' ? 'step' : undefined}>Model test</li>
      <li aria-current={step === 'finish' ? 'step' : undefined}>Open Chat</li>
    </ol>
    {step === 'provider' && <form onSubmit={event => { event.preventDefault(); act('provider', savedProfile ? { profile_id: savedProfile } : { provider, api_key: key }); }}>
      {state.profiles?.length > 0 && <label>Saved provider<select value={savedProfile} disabled={busy} onChange={event => { setSavedProfile(event.target.value); setKey(''); }}>
        <option value="">Connect a provider</option>{state.profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
      </select></label>}
      {!savedProfile && <>
        <label>Provider<select value={provider} disabled={busy} onChange={event => { setProvider(event.target.value); setKey(''); }}>
          {state.providers.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}
        </select></label>
        <label>Provider API key<input required type="password" autoComplete="off" spellCheck={false} maxLength={512} value={key} disabled={busy}
          onChange={event => setKey(event.target.value)} placeholder="Enter your own provider key" /></label>
      </>}
      <p>Your credentials are encrypted in your account. Enter them here, never in Chat. Your provider’s subscription or API billing applies.</p>
      <button className="send-btn" disabled={busy || (!savedProfile && !key.trim())}>{busy ? 'Saving…' : 'Save and choose model'}</button>
    </form>}
    {step === 'model' && <form onSubmit={event => { event.preventDefault(); act('test', { model }); }}>
      <p>Using <strong>{selected?.name || 'your saved provider'}</strong>. Your key is saved.</p>
      <label>Model ID<input required list="onboarding-models" maxLength={200} value={model} disabled={busy} autoComplete="off"
        onChange={event => setModel(event.target.value)} placeholder="Select or enter an exact model ID" /></label>
      <datalist id="onboarding-models">{models.map(id => <option key={id} value={id} />)}</datalist>
      {notice && <p role="status">{notice}</p>}
      <p>Testing sends one short request to this model. It may use your provider’s credits or included allowance. It runs no tools and sends no personal Chat history.</p>
      <button className="send-btn" disabled={busy || !model.trim()}>{busy ? 'Testing connection…' : 'Test connection'}</button>
      <button type="button" className="onboarding-secondary" disabled={busy} onClick={() => { setEditingProvider(true); setError(''); }}>Change provider</button>
    </form>}
    {step === 'finish' && <div>
      <p role="status">Connection verified: <strong>{selected?.name}</strong> · {state.model}</p>
      <p>We’ll create your Overwatcher with this provider and model, then open a welcome conversation. You can connect services and set up voice afterward.</p>
      <button className="send-btn" disabled={busy} onClick={() => act('complete')}>{busy ? 'Preparing your assistant…' : 'Create Overwatcher and open Chat'}</button>
      <button type="button" className="onboarding-secondary" disabled={busy} onClick={() => { onState({ ...state, step: 'model', verified: false }); setError(''); }}>Change model</button>
    </div>}
    {error && <p className="message-error" role="alert">{error}</p>}
    <button className="onboarding-secondary" disabled={busy} onClick={onAdvanced}>Open existing settings</button>
  </section>;
}
