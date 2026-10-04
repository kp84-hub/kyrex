import React, { useState } from 'react';
import { connectGitHub } from '../lib/api.js';

export default function GitHubConnect({ onConnected }) {
  const [token, setToken] = useState('');
  const [repositories, setRepositories] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const submit = async (event) => {
    event.preventDefault();
    setBusy(true); setError('');
    const credential = token;
    setToken('');
    try {
      await connectGitHub(credential, repositories.split(/[\s,]+/).filter(Boolean));
      await onConnected();
    } catch (e) {
      setError(e.message || 'GitHub connection failed.');
    } finally { setBusy(false); }
  };
  return <form onSubmit={submit} aria-label="Connect GitHub repositories" className="github-connect-form">
    <p>Create a <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener noreferrer">fine-grained GitHub token</a>, select the repositories you want Kyrex to read, and set Contents to Read-only. Leave other permissions unset.</p>
    <label>Fine-grained token
      <input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} required disabled={busy} />
    </label>
    <label>Repositories (owner/name, separated by commas)
      <input type="text" placeholder="kp84-hub/kyrex" value={repositories} onChange={e => setRepositories(e.target.value)} required disabled={busy} />
    </label>
    <p>The token is encrypted on Kyrex’s server and is never sent to the model. Kyrex can read only the repositories you enter here.</p>
    {error ? <p role="alert">{error}</p> : null}
    <button type="submit" className="connection-btn" disabled={busy}>{busy ? 'Connecting…' : 'Connect GitHub'}</button>
  </form>;
}
