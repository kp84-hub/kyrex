import React, { useEffect, useRef, useState } from 'react';
import { checkBotModel } from '../lib/api.js';

export default function BotModelCheck({ botId, profile, model }) {
  const [busy, setBusy] = useState(false), [result, setResult] = useState(null), [error, setError] = useState('');
  const sequence = useRef(0);
  useEffect(() => {
    sequence.current += 1;
    setBusy(false); setResult(null); setError('');
    return () => { sequence.current += 1; };
  }, [botId, profile, model]);
  const supported = /^https:\/\/opencode\.ai(?:\/|$)/i.test(profile?.base_url || '');
  if (!supported) return null;
  const check = async () => {
    if (busy) return;
    const token = ++sequence.current;
    setBusy(true); setResult(null); setError('');
    try {
      const value = await checkBotModel(botId, { provider_profile_id: profile.id, model });
      if (token === sequence.current) setResult(value);
    } catch (e) {
      if (token === sequence.current) setError(e.message);
    } finally {
      if (token === sequence.current) setBusy(false);
    }
  };
  return <div className="bot-model-check">
    <button type="button" className="bot-configure-btn" disabled={busy || !model?.trim()} onClick={check}>
      {busy ? 'Checking connection…' : 'Check OpenCode connection'}
    </button>
    <p className="bot-config-hint">Uses your saved key for one short test. Your provider’s usage allowance applies.</p>
    {result && <div role={result.ok ? 'status' : 'alert'}>
      <p>{result.message}</p>
      <p>{result.workspace?.available
        ? result.workspace.model_listed ? 'OpenCode lists this model for your workspace.' : 'OpenCode did not list this model for your workspace. Check model access in OpenCode Console.'
        : `The workspace model list could not be checked${result.workspace?.http_status ? ` (HTTP ${result.workspace.http_status})` : ''}.`}</p>
      {result.ok && <p>If a Chat request still fails, its tools or conversation history need a separate check.</p>}
    </div>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
